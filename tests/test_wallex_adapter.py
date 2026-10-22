from __future__ import annotations

import asyncio
import json
from decimal import Decimal
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


async def _client_returning(monkeypatch, payload) -> WallexClient:
    client = WallexClient("https://example.test", "wss://example.test", "test-key")

    async def request(method, path, **kwargs):
        return payload

    monkeypatch.setattr(client, "_request", request)
    return client


@pytest.mark.asyncio
async def test_balances_use_value_minus_locked(monkeypatch) -> None:
    payload = {
        "result": {
            "balances": {
                "TMN": {"asset": "TMN", "value": "5000000", "locked": "250000"},
                "BTC": {"asset": "BTC", "value": "0", "locked": "0"},
            }
        }
    }
    client = await _client_returning(monkeypatch, payload)
    try:
        balances = {b.asset: b for b in await client.get_balances()}
    finally:
        await client.close()
    assert balances["TMN"].available == Decimal("4750000")
    assert balances["TMN"].locked == Decimal("250000")


@pytest.mark.asyncio
async def test_fees_are_per_symbol_with_conservative_fallback(monkeypatch) -> None:
    payload = {
        "result": {
            "BTCTMN": {"makerFeeRate": "0.00250000", "takerFeeRate": "0.00300000"},
            "XYZTMN": {"makerFeeRate": "0.00400000", "takerFeeRate": "0.00500000"},
        }
    }
    client = await _client_returning(monkeypatch, payload)
    try:
        btc = await client.get_fees("BTCTMN")
        worst = await client.get_fees()
    finally:
        await client.close()
    assert (btc.maker, btc.taker) == (Decimal("0.0025"), Decimal("0.003"))
    assert (worst.maker, worst.taker) == (Decimal("0.004"), Decimal("0.005"))


@pytest.mark.asyncio
async def test_tickers_parse_market_stats(monkeypatch) -> None:
    payload = {
        "result": {
            "symbols": {
                "BTCTMN": {
                    "stats": {
                        "lastPrice": "22062999871.0",
                        "24h_ch": -2.39,
                        "24h_highPrice": "22619999994",
                        "24h_lowPrice": "21660000000",
                        "24h_quoteVolume": "101222712575.31",
                    }
                },
                "EMPTYTMN": {"stats": {"lastPrice": "-"}},
            }
        }
    }
    client = await _client_returning(monkeypatch, payload)
    try:
        tickers = await client.get_tickers(["BTCTMN", "EMPTYTMN"])
    finally:
        await client.close()
    assert tickers["BTCTMN"]["last"] == Decimal("22062999871.0")
    assert tickers["BTCTMN"]["change_24h"] == Decimal("-2.39")
    assert "EMPTYTMN" not in tickers


def _client_with(handler) -> WallexClient:
    import httpx

    client = WallexClient("https://example.test", "wss://example.test", "test-key", max_retries=2)
    client._http = httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
        headers={"X-API-Key": "test-key"},
    )
    return client


@pytest.mark.asyncio
async def test_reads_retry_after_a_server_disconnect(monkeypatch) -> None:
    import httpx

    calls = []
    monkeypatch.setattr("wallex_engine.exchange.wallex_client.asyncio.sleep", _no_sleep)

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"result": {"symbols": {}}})

    client = _client_with(handler)
    try:
        assert await client.get_tickers(["BTCTMN"]) == {}
    finally:
        await client.close()
    assert calls == ["GET", "GET"]


@pytest.mark.asyncio
async def test_order_writes_are_never_blindly_retried(monkeypatch) -> None:
    import httpx

    from wallex_engine.errors import ExchangeError

    calls = []
    monkeypatch.setattr("wallex_engine.exchange.wallex_client.asyncio.sleep", _no_sleep)

    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("no answer")

    client = _client_with(handler)
    try:
        with pytest.raises(ExchangeError, match="ambiguous"):
            await client._request("POST", "/v1/account/orders", private=True, json_body={})
    finally:
        await client.close()
    assert calls == ["POST"]


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_fills_parse_the_documented_account_trades_shape(monkeypatch) -> None:
    # Example from the official Wallex docs for GET /v1/account/trades.
    trade = {
        "symbol": "PAXGTMN", "quantity": "0.0032340000000000",
        "price": "61133333.0000000000000000", "sum": "197705.1989220000000000",
        "fee": "0.0000097020000000", "feeCoefficient": "0.0030000000000000",
        "feeAsset": "PAXG", "isBuyer": True, "timestamp": "2022-06-18T17:22:25Z",
    }
    sell = {**trade, "isBuyer": False, "timestamp": "2022-06-18T18:00:00Z"}
    client = await _client_returning(
        monkeypatch, {"result": {"AccountLatestTrades": [trade, sell]}, "success": True}
    )
    try:
        fills = await client.get_fills()
        again = await client.get_fills()
    finally:
        await client.close()
    assert [f.side.value for f in fills] == ["BUY", "SELL"]
    assert fills[0].quantity == Decimal("0.003234") and fills[0].fee_asset == "PAXG"
    assert fills[0].fill_id != fills[1].fill_id
    assert [f.fill_id for f in fills] == [f.fill_id for f in again]


@pytest.mark.asyncio
async def test_empty_account_trades_is_an_empty_list(monkeypatch) -> None:
    client = await _client_returning(monkeypatch, {"result": {"AccountLatestTrades": []}})
    try:
        assert await client.get_fills() == []
    finally:
        await client.close()
