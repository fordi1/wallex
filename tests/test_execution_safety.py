"""Offline regressions for order lifecycle and cumulative fill accounting."""
import asyncio
from types import SimpleNamespace

import pytest
import httpx

from pishro.config import load_config
from pishro.engine.reversion import Entry, ExitOrder, ReversionEngine
from pishro.portfolio import Portfolio
from pishro.risk import RiskManager
from pishro.types import Order, Position
from pishro.broker.live import LiveBroker
from pishro.exchange.wallex import WallexClient


def engine():
    cfg = load_config(overrides={"mode": "paper"})
    broker = SimpleNamespace(name="paper", fallback=(0, 0))
    return ReversionEngine(cfg, SimpleNamespace(infos={}), broker, RiskManager(cfg),
                           Portfolio(), SimpleNamespace(log=lambda *a, **k: None),
                           lambda: None, lambda: None)


def entry(order):
    return Entry(order, "maker", 100, .01, None, {}, 100, 0)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_unconfirmed_cancel_keeps_order_tracked(side):
    eng = engine()
    order = Order("id", "BTCTMN", side, "LIMIT", 100, 2)

    async def cancel(o):
        return o  # cancel and status lookup failed; last known state is still NEW

    eng.broker.cancel = cancel
    if side == "BUY":
        eng.entries[order.symbol] = entry(order)
        asyncio.run(eng._cancel_entry(order.symbol, "test"))
        assert eng.entries[order.symbol].order is order
        assert eng.account.free_tmn == 0
    else:
        eng.exits["pos"] = ExitOrder(order, "pos", "target")
        asyncio.run(eng._cancel_exit("pos"))
        assert eng.exits["pos"].order is order


def test_buy_partial_fills_use_incremental_notional():
    eng = engine()
    order = Order("id", "BTCTMN", "BUY", "LIMIT", 120, 2,
                  filled_qty=1, avg_price=100)
    e = entry(order)
    eng._on_buy_fill(e, 1000)
    order.filled_qty, order.avg_price = 2, 110  # second unit cost 120
    eng._on_buy_fill(e, 1001)
    pos = next(iter(eng.portfolio.positions.values()))
    assert pos.entry_price == pytest.approx(110)
    assert pos.meta["cost_tmn"] == pytest.approx(220)


def test_sell_partial_fills_use_incremental_notional():
    eng = engine()
    pos = eng.portfolio.open(Position("reversion", "BTCTMN", "BTC", "TMN", 2, 90, 1))
    trades = []
    eng.on_trade_closed = trades.append
    order = Order("id", "BTCTMN", "SELL", "LIMIT", 100, 2,
                  filled_qty=1, avg_price=100)
    x = ExitOrder(order, pos.id, "target")
    eng._on_sell_fill(x, 1000)
    order.filled_qty, order.avg_price = 2, 110
    eng._on_sell_fill(x, 1001)
    assert sum(t.pnl_tmn for t in trades) == pytest.approx(40)


def test_small_partial_remainder_is_not_discarded():
    portfolio = Portfolio()
    pos = portfolio.open(Position("reversion", "BTCTMN", "BTC", "TMN", 1, 100, 1))
    portfolio.close(pos, 110, 0, 1, "target", qty=.999)
    assert portfolio.positions[pos.id].qty == pytest.approx(.001)


def test_ambiguous_submission_is_not_retried_or_rejected():
    requests = []

    def handler(req):
        requests.append(req.method)
        raise httpx.ReadTimeout("response lost", request=req)

    async def run():
        client = WallexClient("https://offline.example", api_key="fake", max_retries=1)
        await client.close()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        broker = LiveBroker(client, .0025, .003)
        order = await broker.place(Order("id", "BTCTMN", "BUY", "LIMIT", 100, 2))
        await client.close()
        return order

    order = asyncio.run(run())
    assert requests.count("POST") == 1
    assert order.status == "UNKNOWN" and order.is_open


def test_cancel_uses_documented_query_parameter():
    def handler(req):
        assert req.method == "DELETE"
        assert req.url.path == "/v1/account/orders"
        assert req.url.params["clientOrderId"] == "test-id"
        return httpx.Response(200, json={"success": True})

    async def run():
        client = WallexClient("https://offline.example", api_key="fake")
        await client.close()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.cancel_order("test-id")
        finally:
            await client.close()

    asyncio.run(run())
