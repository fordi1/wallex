from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from ..ai.providers import AIReviewService
from ..config import AppConfig
from ..data.market_data import WallexMarketDataSource, resample_completed
from ..errors import (
    ConfigurationError,
    ExchangeError,
    ReconciliationError,
    StaleMarketDataError,
)
from ..execution.brokers import LiveBroker, ShadowBroker, client_order_id, quantize_price
from ..features.indicators import feature_snapshots
from ..lifecycle import flatten_plan
from ..models import (
    WALLEX_FEES,
    DecisionRecord,
    EquitySnapshot,
    MarketSpec,
    OrderBookSnapshot,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    RegimeDecision,
    ReviewDecision,
    Side,
    SignalAction,
)
from ..protocols import Broker, ExchangeClient
from ..risk.manager import PortfolioRiskManager
from ..storage.database import SqlEventStore
from ..strategy.hybrid import HybridStrategy, trail_position
from .alerts import AlertManager
from .health import HealthState


class BookFeed:
    def __init__(self, client: ExchangeClient, symbols: list[str]) -> None:
        self.client = client
        self.symbols = symbols
        self.books: dict[str, OrderBookSnapshot] = {}
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="wallex-book-feed")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def get(self, symbol: str) -> OrderBookSnapshot:
        book = self.books.get(symbol)
        if book is None or datetime.now(tz=UTC) - book.captured_at > timedelta(seconds=15):
            book = await self.client.get_order_book(symbol)
            self.books[symbol] = book
        return book

    async def _run(self) -> None:
        async for book in self.client.stream_books(self.symbols):
            self.books[book.symbol] = book


TRANSIENT_ERRORS = (httpx.TransportError, ExchangeError, StaleMarketDataError)


class TradingRuntime:
    def __init__(
        self,
        mode: str,
        config: AppConfig,
        client: ExchangeClient,
        market_data: WallexMarketDataSource,
        store: SqlEventStore,
        ai: AIReviewService,
        alerts: AlertManager,
        health: HealthState,
    ) -> None:
        self.mode = mode
        self.config = config
        self.client = client
        self.market_data = market_data
        self.store = store
        self.ai = ai
        self.alerts = alerts
        self.health = health
        self.strategy = HybridStrategy(config.strategy)
        self.risk = PortfolioRiskManager(
            config.risk,
            config.execution,
            config.challenge.initial_capital_tmn,
            config.app.timezone,
        )
        self.broker: Broker = (
            LiveBroker(client, store) if mode == "live" else ShadowBroker(store, WALLEX_FEES.taker)
        )
        self._shadow_cash = config.challenge.initial_capital_tmn
        self.run_id = f"{mode}-{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        self._last_decision: dict[str, datetime] = {}
        self._last_reconcile = datetime.min.replace(tzinfo=UTC)
        self._transient_failures = 0
        self._regimes: dict[str, tuple[datetime, RegimeDecision]] = {}
        self._markets: dict[str, MarketSpec] = {}
        self._feed: BookFeed | None = None

    async def run_forever(self) -> None:
        self.config.require_challenge_ready(self.mode)
        await self.store.initialize()
        if not await self.store.acquire_leader_lock():
            raise ConfigurationError("another engine replica owns the PostgreSQL leader lock")
        if await self.store.kill_switch_active():
            self.risk.state.kill_switch_active = True
            raise ConfigurationError(
                "persisted kill switch is active; explicit restart acknowledgement is required"
            )
        self._markets = await self._validated_markets()
        self._feed = BookFeed(self.client, list(self._markets))
        await self._feed.start()
        self.health.mode = self.mode
        self.health.ready = True
        self.health.detail = "سرویس آماده است"
        await self.alerts.send("process_started", "موتور معاملاتی راه‌اندازی شد.", mode=self.mode)
        try:
            while True:
                await asyncio.sleep(await self._guarded_cycle())
        finally:
            self.health.ready = False
            if self._feed:
                await self._feed.close()

    async def _guarded_cycle(self) -> float:
        """Run one cycle; a transient exchange/network fault pauses instead of crashing.

        Reconciliation drift and configuration errors still stop the process (fail closed).
        Returns the delay before the next cycle.
        """
        try:
            await self.run_cycle()
        except TRANSIENT_ERRORS as exc:
            self._transient_failures += 1
            self.health.ready = False
            self.health.detail = "اختلال موقت در اتصال به والکس؛ تلاش دوباره…"
            if self._transient_failures in (1, 12):
                await self.alerts.send(
                    "exchange_degraded",
                    f"اختلال موقت در اتصال به والکس ({type(exc).__name__}).",
                    failures=self._transient_failures,
                )
            return min(60.0, 5.0 * 2 ** min(self._transient_failures, 4))
        if self._transient_failures:
            self.health.ready = True
            self.health.detail = "سرویس آماده است"
            self._transient_failures = 0
        return 5.0

    async def run_cycle(self) -> None:
        now = datetime.now(tz=UTC)
        plan = flatten_plan(now, self.config.challenge.ends_at)  # type: ignore[arg-type]
        if now - self._last_reconcile >= timedelta(
            seconds=self.config.execution.reconcile_interval_seconds
        ):
            try:
                await self.broker.reconcile()
            except ReconciliationError:
                self.health.ready = False
                self.health.detail = "مغایرت تطبیق وضعیت"
                await self.alerts.send("reconciliation_drift", "مغایرت تطبیق شناسایی شد.")
                return
            self._last_reconcile = now
            self.health.last_reconciliation_at = now
            self.health.ready = True
            await self.store.set_service_state("reconciliation_status", "ok")
        if plan.cancel_pending_buys:
            await self._cancel_pending_buys()
        positions = await self.store.load_positions()
        if plan.stage != "normal":
            await self._flatten_positions(
                positions,
                plan.use_market_exits,
                plan.seconds_remaining,
            )
            await self.store.set_service_state("flatten_stage", plan.stage)
            await self.store.set_service_state(
                "flatten_seconds_remaining", str(plan.seconds_remaining)
            )
            return
        await self.store.set_service_state("flatten_stage", "normal")
        await self.store.set_service_state(
            "flatten_seconds_remaining", str(plan.seconds_remaining)
        )
        if self.mode == "live":
            markets = self.config.challenge.allowed_markets
            schedules = [await self.client.get_fees(symbol) for symbol in markets]
            fee_rate = max(schedule.maker for schedule in schedules)
        else:
            fee_rate = WALLEX_FEES.maker
        cash = await self._cash_tmn()
        equity, position_value = await self._equity(cash, positions)
        transitions = self.risk.update_equity(equity, now)
        if "kill_switch" in transitions:
            await self.store.set_kill_switch(True, "peak drawdown reached 10%", now)
            await self._cancel_pending_buys()
            await self._flatten_positions(positions, True, 0)
            await self.alerts.send("kill_switch", "توقف اضطراری فعال شد و خروج آغاز شد.")
            return
        await self.store.append_equity(
            EquitySnapshot(
                run_id=self.run_id,
                captured_at=now,
                cash_tmn=cash,
                positions_tmn=position_value,
                equity_tmn=equity,
                peak_equity_tmn=self.risk.state.peak_equity,
                drawdown=(self.risk.state.peak_equity - equity) / self.risk.state.peak_equity,
                daily_pnl=(equity - self.risk.state.day_start_equity)
                / self.risk.state.day_start_equity,
            )
        )
        await self._consider_markets(equity, fee_rate)

    async def _consider_markets(self, equity: Decimal, fee_rate: Decimal) -> None:
        """Evaluate each market against fresh positions and cash.

        An entry filled for one symbol must count against the canary/portfolio limits
        of the next symbol in the same cycle.
        """
        for symbol, market in self._markets.items():
            positions = await self.store.load_positions()
            cash = await self._cash_tmn()
            try:
                await self._consider_symbol(symbol, market, positions, equity, cash, fee_rate)
            except StaleMarketDataError:
                await self.alerts.send(
                    "stale_market_data",
                    "داده بازار تازه نیست؛ ورود این بازار متوقف شد.",
                    symbol=symbol,
                )

    async def _consider_symbol(
        self,
        symbol: str,
        market: MarketSpec,
        positions: list[Position],
        equity: Decimal,
        cash: Decimal,
        fee_rate: Decimal,
    ) -> None:
        assert self._feed is not None
        end = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        minute = await self.market_data.history(symbol, 1, end - timedelta(days=3), end)
        five = resample_completed(minute, self.config.strategy.signal_timeframe_minutes)
        hourly = await self.market_data.history(symbol, 60, end - timedelta(days=5), end)
        book = await self._feed.get(symbol)
        self.health.last_market_data_at = book.captured_at
        snapshots = feature_snapshots(
            five,
            hourly,
            book,
            fee_rate,
            self.config.execution.modeled_slippage_bps,
        )
        if not snapshots:
            return
        snapshot = snapshots[-1]
        if self._last_decision.get(symbol) == snapshot.observed_at:
            return
        self._last_decision[symbol] = snapshot.observed_at
        existing = next((item for item in positions if item.symbol == symbol), None)
        if existing:
            candle = next((c for c in five if c.opened_at == snapshot.observed_at), five[-1])
            await self._manage_position(existing, snapshot, candle, book)
            return
        signal = self.strategy.evaluate(snapshot)
        cached_regime = self._regimes.get(symbol)
        if cached_regime and snapshot.observed_at - cached_regime[0] < timedelta(minutes=30):
            regime = cached_regime[1]
        else:
            regime = await self.ai.regime(snapshot)
            self._regimes[symbol] = (snapshot.observed_at, regime)
        review = (
            await self.ai.review(snapshot, signal)
            if signal.action == SignalAction.ENTER_LONG
            else ReviewDecision(
                approve=False,
                size_adjustment=Decimal("0"),
                reason_fa="سیگنال ورود صادر نشده است.",
            )
        )
        self.risk.state.ai_entries_paused = self.ai.entries_paused
        risk = self.risk.assess_entry(
            signal, market, equity, cash, positions, regime, review, snapshot.observed_at
        )
        outcome = "skipped"
        if signal.action == SignalAction.ENTER_LONG and risk.approved:
            outcome = await self._execute_entry(signal, risk.quantity, market, book)
        await self.store.append_decision(
            DecisionRecord(
                decision_id=signal.decision_id,
                run_id=self.run_id,
                created_at=snapshot.observed_at,
                mode=self.mode,
                symbol=symbol,
                features=snapshot,
                signal=signal,
                regime=regime,
                review=review,
                risk=risk,
                outcome=outcome,
                reason_fa=risk.reason_fa
                if signal.action == SignalAction.ENTER_LONG
                else signal.reason_fa,
            )
        )

    async def _manage_position(
        self, position: Position, snapshot: Any, candle: Any, book: Any
    ) -> None:
        """One new signal bar for an open position: count it, exit if due, else trail.

        Mirrors the backtest: bars_held drives the time stop and the trailing stop starts
        once price has moved one initial risk in our favour.
        """
        position = position.model_copy(update={"bars_held": position.bars_held + 1})
        await self.store.upsert_position(position)
        signal = self.strategy.evaluate_exit(snapshot, position)
        if signal.action == SignalAction.EXIT:
            too_early = position.opened_at is not None and (
                snapshot.observed_at - position.opened_at
                < timedelta(minutes=self.config.risk.min_hold_minutes)
            )
            if too_early and not signal.urgent:
                return
            await self._exit_position(signal.decision_id, position, book, signal.urgent)
            return
        trailed = trail_position(
            position, candle.high, candle.close, snapshot.atr_14, self.config.strategy.stop_atr
        )
        if trailed != position:
            await self.store.upsert_position(trailed)

    async def _execute_entry(
        self, signal: Any, quantity: Decimal, market: MarketSpec, initial_book: OrderBookSnapshot
    ) -> str:
        assert self._feed is not None
        book = initial_book
        for attempt in range(self.config.execution.max_reprice_attempts + 1):
            price = quantize_price(book.best_bid, market.tick_size)
            intent = OrderIntent(
                decision_id=signal.decision_id,
                client_id=client_order_id(signal.decision_id, attempt),
                attempt=attempt,
                symbol=market.symbol,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                price=price,
                created_at=datetime.now(tz=UTC),
            )
            order = await self.broker.submit(intent)
            if order.status == OrderStatus.FILLED:
                await self._position_from_order(signal, order, market)
                return "filled"
            if self.mode == "shadow":
                return "filled"
            deadline = datetime.now(tz=UTC) + timedelta(
                seconds=self.config.execution.entry_timeout_seconds
            )
            while datetime.now(tz=UTC) < deadline:
                await asyncio.sleep(2)
                updated = await self.client.get_order(client_id=intent.client_id)
                if updated and updated.status == OrderStatus.FILLED:
                    await self.store.persist_order(updated)
                    await self._position_from_order(signal, updated, market)
                    return "filled"
                if updated and updated.status == OrderStatus.PARTIAL:
                    await self.broker.cancel(updated)
                    await self._position_from_order(signal, updated, market)
                    return "partial_protected"
            await self.broker.cancel(order)
            book = await self._feed.get(market.symbol)
        return "expired_without_market_conversion"

    async def _position_from_order(self, signal: Any, order: Any, market: MarketSpec) -> None:
        quantity = order.filled_quantity
        if quantity <= 0:
            return
        position = Position(
            symbol=market.symbol,
            quantity=quantity,
            average_entry=order.average_price or signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            opened_at=order.updated_at,
        )
        await self.store.upsert_position(position)
        if self.mode == "shadow":
            self._shadow_cash -= position.quantity * position.average_entry * Decimal("1.002")
        if self.mode == "live":
            await self.broker.install_protection(signal.decision_id, position, market)  # type: ignore[attr-defined]

    async def _cancel_pending_buys(self) -> None:
        if self.mode != "live":
            return
        for order in await self.client.get_open_orders():
            if order.side == Side.BUY:
                await self.broker.cancel(order)

    async def _flatten_positions(
        self,
        positions: list[Position],
        market: bool,
        seconds_remaining: int,
    ) -> None:
        assert self._feed is not None
        # The limit-exit window lasts 150 minutes. A stable five-minute bucket
        # prevents order churn while still making each replacement more
        # aggressive as the deadline approaches.
        attempt = 999 if market else max(0, (3 * 60 * 60 - seconds_remaining) // 300)
        for position in positions:
            book = await self._feed.get(position.symbol)
            await self._exit_position(
                f"flatten-{self.run_id}-{position.symbol}",
                position,
                book,
                market,
                attempt=attempt,
            )

    async def _exit_position(
        self,
        decision_id: str,
        position: Position,
        book: OrderBookSnapshot,
        use_market: bool,
        attempt: int = 0,
    ) -> None:
        desired_client_id = client_order_id(decision_id, attempt, "exit")
        if self.mode == "live":
            # Cancel exchange-side protection and any prior exit before placing
            # the full-quantity replacement. Keeping the current order avoids
            # cancel/recreate churn within the same flatten bucket.
            open_sells = [
                order
                for order in await self.client.get_open_orders(position.symbol)
                if order.side == Side.SELL
            ]
            if any(order.client_id == desired_client_id for order in open_sells):
                return
            for order in open_sells:
                await self.broker.cancel(order)
        price = None
        if not use_market:
            price = flatten_limit_price(book, attempt)
        intent = OrderIntent(
            decision_id=decision_id,
            client_id=desired_client_id,
            attempt=attempt,
            symbol=position.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET if use_market else OrderType.LIMIT,
            quantity=position.quantity,
            price=price,
            reduce_only=True,
            created_at=datetime.now(tz=UTC),
        )
        order = await self.broker.submit(intent)
        if order.status == OrderStatus.FILLED:
            if self.mode == "shadow" and order.average_price is not None:
                self._shadow_cash += order.filled_quantity * order.average_price * Decimal("0.998")
            await self.store.delete_position(position.symbol)
    async def _cash_tmn(self) -> Decimal:
        if self.mode != "live":
            return max(Decimal(0), self._shadow_cash)
        balances = await self.client.get_balances()
        tmn = next((item for item in balances if item.asset == "TMN"), None)
        return tmn.available if tmn else Decimal(0)

    async def _equity(self, cash: Decimal, positions: list[Position]) -> tuple[Decimal, Decimal]:
        assert self._feed is not None
        value = Decimal(0)
        for position in positions:
            book = await self._feed.get(position.symbol)
            value += position.quantity * book.best_bid
        return cash + value, value

    async def _validated_markets(self) -> dict[str, MarketSpec]:
        available = await self.client.get_markets()
        validated: dict[str, MarketSpec] = {}
        errors: list[str] = []
        for symbol in self.config.challenge.allowed_markets:
            normalized = symbol.upper()
            spec = available.get(normalized)
            if spec is None:
                errors.append(f"{normalized}: metadata missing")
            elif not spec.active:
                errors.append(f"{normalized}: inactive")
            elif spec.quote_asset != "TMN":
                errors.append(f"{normalized}: quote is not TMN")
            else:
                validated[normalized] = spec
        if errors or not validated:
            raise ConfigurationError("market allowlist validation failed: " + "; ".join(errors))
        return validated


def flatten_limit_price(book: OrderBookSnapshot, attempt: int) -> Decimal:
    """Move from best ask toward best bid over thirty five-minute buckets."""

    progress = min(max(Decimal(attempt), Decimal(0)) / Decimal(30), Decimal(1))
    return book.best_ask - (book.best_ask - book.best_bid) * progress
