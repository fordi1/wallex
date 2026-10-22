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
