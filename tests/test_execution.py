from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wallex_engine.errors import ReconciliationError
from wallex_engine.execution.brokers import LiveBroker, client_order_id, fee_value_tmn
from wallex_engine.models import (
    Fill,
    MarketSpec,
    OrderIntent,
    OrderRecord,
    OrderStatus,
    OrderType,
    Side,
)


class MemoryStore:
    async def persist_intent(self, intent):
        pass

    async def persist_order(self, order):
        pass


class FakeClient:
    async def get_order(self, **kwargs):
        return None

    async def get_open_orders(self, symbol=None):
        return [
            OrderRecord(
                client_id="other",
                status=OrderStatus.OPEN,
                symbol="BTCTMN",
                side=Side.SELL,
                requested_quantity=Decimal("1"),
                updated_at=datetime.now(tz=UTC),
            )
        ]

    async def create_order(self, intent):
        raise AssertionError("must not submit")


def test_client_order_id_is_deterministic() -> None:
    assert client_order_id("d", 0) == client_order_id("d", 0)
    assert client_order_id("d", 0) != client_order_id("d", 1)


@pytest.mark.asyncio
async def test_no_simultaneous_opposing_orders() -> None:
    broker = LiveBroker(FakeClient(), MemoryStore())
    intent = OrderIntent(
        decision_id="d",
        client_id=client_order_id("d", 0),
        attempt=0,
        symbol="BTCTMN",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        price=Decimal("100"),
        created_at=datetime.now(tz=UTC),
    )
    with pytest.raises(ReconciliationError):
        await broker.submit(intent)


@pytest.mark.asyncio
async def test_fee_asset_is_converted_to_tmn_at_fill_price() -> None:
    fill = Fill(
        fill_id="f",
        client_id="c",
        symbol="BTCTMN",
        side=Side.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("20000000000"),
        fee_quantity=Decimal("0.0001"),
        fee_asset="BTC",
        fee_tmn=Decimal("0"),
        filled_at=datetime.now(tz=UTC),
    )
    market = MarketSpec(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        active=True,
        tick_size=Decimal("1"),
        step_size=Decimal("0.00000001"),
        min_quantity=Decimal("0.00001"),
        min_notional=Decimal("100000"),
    )
    assert await fee_value_tmn(fill, {"BTCTMN": market}, FakeClient()) == Decimal("2000000")
