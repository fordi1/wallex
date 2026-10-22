from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wallex_engine.lifecycle import flatten_plan
from wallex_engine.models import BookLevel, OrderBookSnapshot
from wallex_engine.runtime.engine import flatten_limit_price


def test_flatten_stages() -> None:
    end = datetime(2026, 10, 22, 20, tzinfo=UTC)
    assert flatten_plan(end - timedelta(hours=4), end).stage == "normal"
    limit = flatten_plan(end - timedelta(hours=2), end)
    assert limit.stage == "limit_exit" and limit.stop_new_entries
    market = flatten_plan(end - timedelta(minutes=20), end)
    assert market.stage == "market_exit" and market.use_market_exits
    assert flatten_plan(end + timedelta(seconds=1), end).stage == "ended"


def test_flatten_limit_price_becomes_more_aggressive() -> None:
    book = OrderBookSnapshot(
        symbol="BTCTMN",
        captured_at=datetime.now(tz=UTC),
        bids=(BookLevel(price=Decimal("99"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("1")),),
    )
    assert flatten_limit_price(book, 0) == Decimal("101")
    assert flatten_limit_price(book, 15) == Decimal("100")
    assert flatten_limit_price(book, 30) == Decimal("99")
    assert flatten_limit_price(book, 100) == Decimal("99")


@pytest.mark.asyncio
async def test_transient_exchange_error_pauses_the_cycle_instead_of_crashing() -> None:
    import httpx

    from wallex_engine.errors import ReconciliationError
    from wallex_engine.runtime.engine import TradingRuntime
    from wallex_engine.runtime.health import HealthState

    class Alerts:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, event, message, **_):
            self.sent.append(event)

    runtime = object.__new__(TradingRuntime)
    runtime.health, runtime.alerts, runtime._transient_failures = HealthState(), Alerts(), 0
    runtime.health.ready = True

    async def disconnect() -> None:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    runtime.run_cycle = disconnect
    delay = await runtime._guarded_cycle()
    assert delay > 5 and runtime.health.ready is False
    assert runtime.alerts.sent == ["exchange_degraded"]

    async def healthy() -> None:
        return None

    runtime.run_cycle = healthy
    assert await runtime._guarded_cycle() == 5
    assert runtime.health.ready is True and runtime._transient_failures == 0

    async def drift() -> None:
        raise ReconciliationError("balances differ")

    runtime.run_cycle = drift
    with pytest.raises(ReconciliationError):
        await runtime._guarded_cycle()


class _Store:
    def __init__(self) -> None:
        self.saved = {}

    async def upsert_position(self, position) -> None:
        self.saved[position.symbol] = position


def _runtime_for_positions(config):
    from wallex_engine.runtime.engine import TradingRuntime
    from wallex_engine.strategy.hybrid import HybridStrategy

    runtime = object.__new__(TradingRuntime)
    runtime.config, runtime.store = config, _Store()
    runtime.strategy = HybridStrategy(config.strategy)
    runtime.exits = []

    async def exit_position(decision_id, position, book, use_market, attempt=0):
        runtime.exits.append((position.bars_held, use_market))

    runtime._exit_position = exit_position
    return runtime


def _open_position(**update):
    from wallex_engine.models import Position

    base = Position(symbol="BTCTMN", quantity=Decimal("1"), average_entry=Decimal("100"),
                    stop_price=Decimal("90"), target_price=Decimal("130"),
                    opened_at=datetime(2026, 10, 21, tzinfo=UTC))
    return base.model_copy(update=update)


def _candle(high: str, close: str):
    from wallex_engine.models import Candle

    return Candle(symbol="BTCTMN", timeframe_minutes=15,
                  opened_at=datetime(2026, 10, 22, tzinfo=UTC),
                  open=Decimal(close), high=Decimal(high), low=Decimal(close),
                  close=Decimal(close), volume=Decimal("1"), completed=True)


@pytest.mark.asyncio
async def test_open_position_counts_bars_and_hits_the_time_stop(config, feature) -> None:
    runtime = _runtime_for_positions(config)
    snapshot = feature.model_copy(update={"close": Decimal("101")})
    position = _open_position(bars_held=config.strategy.time_stop_bars - 2)
    await runtime._manage_position(position, snapshot, _candle("101", "101"), None)
    assert runtime.store.saved["BTCTMN"].bars_held == config.strategy.time_stop_bars - 1
    assert runtime.exits == []
    await runtime._manage_position(runtime.store.saved["BTCTMN"], snapshot,
                                   _candle("101", "101"), None)
    assert runtime.exits == [(config.strategy.time_stop_bars, False)]


@pytest.mark.asyncio
async def test_open_position_trails_after_one_r(config, feature) -> None:
    runtime = _runtime_for_positions(config)
    snapshot = feature.model_copy(update={"close": Decimal("112"), "atr_14": Decimal("2")})
    await runtime._manage_position(_open_position(), snapshot, _candle("112", "112"), None)
    expected = Decimal("112") - config.strategy.stop_atr * Decimal("2")
    assert runtime.store.saved["BTCTMN"].trailing_stop == expected


@pytest.mark.asyncio
async def test_stop_exits_go_at_market_even_inside_min_hold(config, feature) -> None:
    runtime = _runtime_for_positions(config)
    just_opened = _open_position(opened_at=feature.observed_at)
    crash = feature.model_copy(update={"close": Decimal("89")})
    await runtime._manage_position(just_opened, crash, _candle("90", "89"), None)
    assert runtime.exits == [(1, True)]
    trailing_hit = _open_position(trailing_stop=Decimal("105"))
    dipped = feature.model_copy(update={"close": Decimal("104")})
    await runtime._manage_position(trailing_hit, dipped, _candle("104", "104"), None)
    assert runtime.exits[-1] == (1, True)
    target = _open_position()
    await runtime._manage_position(target, feature.model_copy(update={"close": Decimal("131")}),
                                   _candle("131", "131"), None)
    assert runtime.exits[-1] == (1, False)


@pytest.mark.asyncio
async def test_each_market_sees_positions_opened_earlier_in_the_same_cycle(config) -> None:
    from wallex_engine.runtime.engine import TradingRuntime

    runtime = object.__new__(TradingRuntime)
    runtime._markets = {"BTCTMN": object(), "ETHTMN": object()}
    opened: list = []
    seen: list = []

    class Store:
        async def load_positions(self):
            return list(opened)

    async def cash():
        return Decimal("5000000") - Decimal("250000") * len(opened)

    async def consider(symbol, market, positions, equity, cash_now, fee_rate):
        seen.append((symbol, len(positions), cash_now))
        opened.append(_open_position(symbol=symbol))

    runtime.store, runtime._cash_tmn, runtime._consider_symbol = Store(), cash, consider
    await runtime._consider_markets(Decimal("5000000"), Decimal("0.0025"))
    assert seen == [("BTCTMN", 0, Decimal("5000000")), ("ETHTMN", 1, Decimal("4750000"))]


def _order(status, filled="0", side="BUY", client_id="c1", quantity="1"):
    from wallex_engine.models import OrderRecord, OrderStatus, Side

    return OrderRecord(client_id=client_id, status=OrderStatus(status), symbol="BTCTMN",
                       side=Side(side), requested_quantity=Decimal(quantity),
                       filled_quantity=Decimal(filled), average_price=Decimal("100"),
                       updated_at=datetime(2026, 10, 23, tzinfo=UTC))


def _live_runtime(config, final_orders: dict):
    from wallex_engine.runtime.engine import TradingRuntime

    runtime = object.__new__(TradingRuntime)
    runtime.config = config.model_copy(
        update={"execution": config.execution.model_copy(update={"entry_timeout_seconds": 0})}
    )
    runtime.mode, runtime._feed = "live", object()
    runtime.submitted, runtime.canceled, runtime.positions = [], [], []

    class Broker:
        async def submit(self, intent):
            runtime.submitted.append(intent)
            return _order("OPEN", client_id=intent.client_id, side=intent.side.value)

        async def cancel(self, order):
            runtime.canceled.append(order.client_id)
            return order.model_copy(update={"status": "CANCELED"})

    class Client:
        async def get_order(self, *, client_id=None, order_id=None):
            return final_orders.get(client_id)

        async def get_open_orders(self, symbol=None):
            return [final_orders["protect"].model_copy(update={"status": "OPEN",
                                                               "filled_quantity": Decimal(0)})]

    class Store:
        async def delete_position(self, symbol):
            runtime.positions.append(("deleted", symbol))

    async def position_from_order(signal, order, market):
        runtime.positions.append(("opened", order.filled_quantity))

    runtime.broker, runtime.client, runtime.store = Broker(), Client(), Store()
    runtime._position_from_order = position_from_order
    return runtime


@pytest.mark.asyncio
async def test_entry_fill_that_lands_during_cancel_is_kept_and_not_rebought(config, market):
    from types import SimpleNamespace

    from wallex_engine.execution.brokers import client_order_id

    client_id = client_order_id("d1", 0)
    runtime = _live_runtime(config, {client_id: _order("CANCELED", "0.4", client_id=client_id)})
    book = SimpleNamespace(best_bid=Decimal("100"), best_ask=Decimal("101"))
    signal = SimpleNamespace(decision_id="d1", entry_price=Decimal("100"))
    outcome = await runtime._execute_entry(signal, Decimal("1"), market, book)
    assert outcome == "partial_protected"
    assert runtime.positions == [("opened", Decimal("0.4"))]
    assert len(runtime.submitted) == 1


@pytest.mark.asyncio
async def test_exit_skips_selling_what_the_protective_stop_already_sold(config) -> None:
    from types import SimpleNamespace

    protect = _order("FILLED", "1", side="SELL", client_id="protect")
    runtime = _live_runtime(config, {"protect": protect})
    runtime._markets = {}
    book = SimpleNamespace(best_bid=Decimal("100"), best_ask=Decimal("101"))
    await runtime._exit_position("d2", _open_position(), book, True)
    assert runtime.canceled == ["protect"]
    assert runtime.submitted == []
    assert runtime.positions == [("deleted", "BTCTMN")]
