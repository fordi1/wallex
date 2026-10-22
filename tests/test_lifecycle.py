from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
