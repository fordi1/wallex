from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pandas as pd

from ..ai.providers import AIReviewService, DeterministicAIProvider
from ..config import AppConfig
from ..execution.brokers import BacktestBroker, client_order_id, quantize_price
from ..features.indicators import feature_snapshots
from ..models import (
    BookLevel,
    Candle,
    DecisionRecord,
    EquitySnapshot,
    FeeSchedule,
    MarketSpec,
    OrderBookSnapshot,
    OrderIntent,
    OrderType,
    Position,
    ReviewDecision,
    Side,
    SignalAction,
)
from ..risk.manager import PortfolioRiskManager
from ..storage.database import SqlEventStore
from ..strategy.hybrid import HybridStrategy


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    equity: pd.DataFrame
    trades: pd.DataFrame
    decisions: int
    rejects: int
    fallbacks: int


class BacktestEngine:
    def __init__(
        self,
        config: AppConfig,
        store: SqlEventStore,
        ai_service: AIReviewService | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.strategy = HybridStrategy(config.strategy)
        self.risk = PortfolioRiskManager(
            config.risk,
            config.execution,
            config.challenge.initial_capital_tmn,
            config.app.timezone,
        )
        self.ai = ai_service or AIReviewService(DeterministicAIProvider())
        self.broker = BacktestBroker(store)

    async def run(
        self,
        run_id: str,
        market: MarketSpec,
        candles_5m: list[Candle],
        candles_1h: list[Candle],
        fees: FeeSchedule,
    ) -> BacktestResult:
        books = {
            c.opened_at: synthetic_book(c, self.config.modeled_spread_bps(market.symbol) / 2)
            for c in candles_5m
        }
        snapshots = feature_snapshots(
            candles_5m,
            candles_1h,
            books,
            fees.maker,
            self.config.execution.modeled_slippage_bps,
        )
        candle_by_time = {c.opened_at: c for c in candles_5m}
        times = sorted(candle_by_time)
        next_time = {left: right for left, right in zip(times, times[1:], strict=False)}
        cash = self.config.challenge.initial_capital_tmn
        peak = cash
        position: Position | None = None
        entry_fee = Decimal(0)
        initial_risk_per_unit = Decimal(0)
        trades: list[dict[str, object]] = []
        equity_rows: list[dict[str, object]] = []
        rejects = 0
        fallbacks = 0
        cached_regime = None
        cached_regime_at = None

        for snapshot in snapshots:
            candle = candle_by_time[snapshot.observed_at]
            if position is not None:
                position = position.model_copy(update={"bars_held": position.bars_held + 1})
                exit_price, reason = exit_price_for_bar(
                    position, candle, self.config.strategy.time_stop_bars
                )
                if exit_price is not None:
                    exit_price *= Decimal(
                        "1"
                    ) - self.config.execution.modeled_slippage_bps / Decimal("10000")
                    gross = position.quantity * exit_price
                    exit_fee = gross * fees.taker
                    cash += gross - exit_fee
                    pnl = (
                        (exit_price - position.average_entry) * position.quantity
                        - entry_fee
                        - exit_fee
                    )
                    trades.append(
                        {
                            "symbol": position.symbol,
                            "opened_at": position.opened_at,
                            "closed_at": snapshot.observed_at,
                            "entry": position.average_entry,
                            "exit": exit_price,
                            "quantity": position.quantity,
                            "pnl_tmn": pnl,
                            "reason": reason,
                        }
                    )
                    self.risk.record_exit(position.symbol, pnl, snapshot.observed_at)
                    await self.store.delete_position(position.symbol)
                    position = None
                elif (
                    initial_risk_per_unit > 0
                    and candle.high >= position.average_entry + initial_risk_per_unit
                ):
                    trailing = max(
                        position.trailing_stop or Decimal(0),
                        candle.close - self.config.strategy.stop_atr * snapshot.atr_14,
                    )
                    position = position.model_copy(update={"trailing_stop": trailing})
                    await self.store.upsert_position(position)

            market_value = position.quantity * candle.close if position else Decimal(0)
            equity = cash + market_value
            peak = max(peak, equity)
            self.risk.update_equity(equity, snapshot.observed_at)
            equity_snapshot = EquitySnapshot(
                run_id=run_id,
                captured_at=snapshot.observed_at,
                cash_tmn=cash,
                positions_tmn=market_value,
                equity_tmn=equity,
                peak_equity_tmn=peak,
                drawdown=(peak - equity) / peak if peak else Decimal(0),
                daily_pnl=(equity - self.risk.state.day_start_equity)
                / self.risk.state.day_start_equity,
            )
            await self.store.append_equity(equity_snapshot)
            equity_rows.append(equity_snapshot.model_dump(mode="python"))

            signal = self.strategy.evaluate(snapshot)
            if (
                cached_regime is not None
                and cached_regime_at is not None
                and snapshot.observed_at - cached_regime_at < timedelta(minutes=30)
            ):
                regime = cached_regime
            else:
                regime = await self.ai.regime(snapshot)
                cached_regime = regime
                cached_regime_at = snapshot.observed_at
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
            if (
                "fallback" in regime.regime
                or review.size_adjustment == self.config.ai.fallback_size_multiplier
            ):
                fallbacks += 1
            risk = self.risk.assess_entry(
                signal,
                market,
                equity,
                cash,
                [position] if position else [],
                regime,
                review,
                snapshot.observed_at,
            )
            outcome = "skipped"
            reason = (
                signal.reason_fa if signal.action != SignalAction.ENTER_LONG else risk.reason_fa
            )
            if signal.action == SignalAction.ENTER_LONG and risk.approved and position is None:
                execution_time = next_time.get(snapshot.observed_at)
                if execution_time is None:
                    outcome = "skipped_no_next_bar"
                    reason = "کندل بعدی برای اجرای بدون نگاه به آینده وجود ندارد."
                else:
                    next_bar = candle_by_time[execution_time]
                    execution_price = next_bar.open * (
                        Decimal("1") + self.config.execution.modeled_slippage_bps / Decimal("10000")
                    )
                    execution_price = quantize_price(execution_price, market.tick_size)
                    quantity = risk.quantity
                    notional = quantity * execution_price
                    entry_fee = notional * fees.maker
                    if cash >= notional + entry_fee:
                        intent = OrderIntent(
                            decision_id=signal.decision_id,
                            client_id=client_order_id(signal.decision_id, 0),
                            attempt=0,
                            symbol=market.symbol,
                            side=Side.BUY,
                            order_type=OrderType.LIMIT,
                            quantity=quantity,
                            price=execution_price,
                            created_at=execution_time,
                        )
                        await self.broker.submit(intent)
                        cash -= notional + entry_fee
                        position = Position(
                            symbol=market.symbol,
                            quantity=quantity,
                            average_entry=execution_price,
                            stop_price=signal.stop_price,
                            target_price=signal.target_price,
                            opened_at=execution_time,
                        )
                        initial_risk_per_unit = execution_price - (
                            signal.stop_price or execution_price
                        )
                        await self.store.upsert_position(position)
                        outcome = "filled"
                        reason = "سفارش در قیمت بازشدن کندل بعدی شبیه‌سازی شد."
                    else:
                        outcome = "rejected_cash"
                        reason = "موجودی نقد برای سفارش و کارمزد کافی نیست."
                        rejects += 1
            elif signal.action == SignalAction.ENTER_LONG and not risk.approved:
                rejects += 1
            await self.store.append_decision(
                DecisionRecord(
                    decision_id=signal.decision_id,
                    run_id=run_id,
                    created_at=snapshot.observed_at,
                    mode="backtest",
                    symbol=market.symbol,
                    features=snapshot,
                    signal=signal,
                    regime=regime,
                    review=review,
                    risk=risk,
                    outcome=outcome,
                    reason_fa=reason,
                )
            )

        # A backtest must not hide unrealized inventory at the end of its
        # interval. Liquidate at the last executable close, with taker fees and
        # modeled sell slippage, then publish one final all-cash equity point.
        if position is not None and candles_5m:
            last = candles_5m[-1]
            exit_price = last.close * (
                Decimal("1") - self.config.execution.modeled_slippage_bps / Decimal("10000")
            )
            gross = position.quantity * exit_price
            exit_fee = gross * fees.taker
            cash += gross - exit_fee
            pnl = (
                (exit_price - position.average_entry) * position.quantity - entry_fee - exit_fee
            )
            final_at = last.opened_at + timedelta(minutes=last.timeframe_minutes)
            trades.append(
                {
                    "symbol": position.symbol,
                    "opened_at": position.opened_at,
                    "closed_at": final_at,
                    "entry": position.average_entry,
                    "exit": exit_price,
                    "quantity": position.quantity,
                    "pnl_tmn": pnl,
                    "reason": "backtest_end",
                }
            )
            self.risk.record_exit(position.symbol, pnl, final_at)
            await self.store.delete_position(position.symbol)
            peak = max(peak, cash)
            self.risk.update_equity(cash, final_at)
            final_equity = EquitySnapshot(
                run_id=run_id,
                captured_at=final_at,
                cash_tmn=cash,
                positions_tmn=Decimal(0),
                equity_tmn=cash,
                peak_equity_tmn=peak,
                drawdown=(peak - cash) / peak if peak else Decimal(0),
                daily_pnl=(cash - self.risk.state.day_start_equity)
                / self.risk.state.day_start_equity,
            )
            await self.store.append_equity(final_equity)
            equity_rows.append(final_equity.model_dump(mode="python"))

        return BacktestResult(
            run_id=run_id,
            equity=pd.DataFrame(equity_rows),
            trades=pd.DataFrame(trades),
            decisions=len(snapshots),
            rejects=rejects,
            fallbacks=fallbacks,
        )


def synthetic_book(candle: Candle, half_spread_bps: Decimal) -> OrderBookSnapshot:
    offset = candle.close * half_spread_bps / Decimal("10000")
    return OrderBookSnapshot(
        symbol=candle.symbol,
        captured_at=candle.opened_at,
        bids=(BookLevel(price=candle.close - offset, quantity=Decimal("1000000")),),
        asks=(BookLevel(price=candle.close + offset, quantity=Decimal("1000000")),),
    )


def exit_price_for_bar(
    position: Position, candle: Candle, time_stop_bars: int = 12
) -> tuple[Decimal | None, str | None]:
    stop = (
        max(value for value in (position.stop_price, position.trailing_stop) if value is not None)
        if (position.stop_price is not None or position.trailing_stop is not None)
        else None
    )
    if stop is not None and candle.low <= stop:
        return stop, "stop"
    if position.target_price is not None and candle.high >= position.target_price:
        return position.target_price, "target"
    if position.bars_held >= time_stop_bars:
        return candle.close, "time"
    return None, None
