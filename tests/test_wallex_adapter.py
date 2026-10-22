from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from wallex_engine.exchange.wallex_client import WallexClient, _parse_order
from wallex_engine.models import OrderStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_market_precision_and_active_spot_are_merged(monkeypatch) -> None:
    fixture = json.loads((FIXTURES / "wallex_markets.json").read_text())
    client = WallexClient("https://example.test", "wss://example.test")

    async def request(method, path, **kwargs):
        return fixture["active"] if path.startswith("/hector") else fixture["legacy"]

    monkeypatch.setattr(client, "_request", request)
    try:
        market = (await client.get_markets())["BTCTMN"]
    finally:
        await client.close()
    assert market.active
    assert str(market.tick_size) == "1"
    assert str(market.step_size) == "1E-8"


def test_sanitized_order_variant_parses() -> None:
    payload = json.loads((FIXTURES / "wallex_order.json").read_text())
    order = _parse_order(payload["result"])
    assert order.status == OrderStatus.FILLED
    assert order.client_id == "wai-sanitized"
    assert str(order.average_price) == "100"


@pytest.mark.asyncio
async def test_book_refresh_is_bounded(monkeypatch) -> None:
    client = WallexClient(
        "https://example.test",
        "wss://example.test",
        timeout_seconds=0.01,
    )

    async def slow_book(symbol):
        await asyncio.sleep(1)

    monkeypatch.setattr(client, "get_order_book", slow_book)
    try:
        assert await client._bounded_book_refresh("BTCTMN") is None
    finally:
        await client.close()
