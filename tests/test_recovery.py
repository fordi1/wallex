"""Recovery and risk acceptance tests. No real exchange traffic."""
import asyncio
from types import SimpleNamespace

import pytest

from pishro.ai.policy import Policy
from pishro.broker.live import LiveBroker
from pishro.config import load_config
from pishro.engine.reversion import Entry, ExitOrder, ReversionEngine
from pishro.exchange.wallex import parse_order
from pishro.portfolio import Portfolio
from pishro.risk import RiskManager
from pishro.store import Store
from pishro.types import MarketInfo, Order, Position


def setup_engine(tmp_path):
    cfg = load_config(overrides={"mode": "paper", "risk.min_order_tmn": 1})
    store = Store(tmp_path / "offline.db")
    broker = LiveBroker(SimpleNamespace(), .0025, .003)
    market = SimpleNamespace(infos={"BTCTMN": MarketInfo("BTCTMN", "BTC", "TMN")},
                             data_age=lambda: 0, book_value=lambda *a: 1e12,
                             client=SimpleNamespace(errors_last_minute=lambda: 0))
    eng = ReversionEngine(cfg, market, broker, RiskManager(cfg, store), Portfolio(store),
                          SimpleNamespace(log=lambda *a, **k: None), Policy, lambda: None)
    return eng, store


def test_loss_pause_and_flatten_survive_restart(tmp_path):
    eng, store = setup_engine(tmp_path)
    for _ in range(eng.risk.max_consec):
        eng.risk.on_trade_closed(-.01, 1000)
    restored = RiskManager(eng.cfg, store)
    assert not restored.trading_allowed(1001)[0]
    eng.risk.halt("test")
    assert RiskManager(eng.cfg, store).flatten_requested


def test_zero_fee_fill_does_not_change_other_markets(tmp_path):
    eng, _ = setup_engine(tmp_path)
    order = Order("id", "BTCTMN", "BUY", "LIMIT", 100, 1, fee_reported=True)
    eng._detect_fee(order, 0)
    assert eng.broker.fee("ETHTMN") == (.0025, .003)
    assert eng.broker.fee("BTCTMN") == (.0025, .003)  # don't infer a permanent discount from one fill


def test_open_bids_reserve_exposure_and_slots(tmp_path):
    eng, _ = setup_engine(tmp_path)
    eng.account.equity = eng.account.free_tmn = 1_000_000
    eng.account.exposure = 700_000
    eng.entries["ETHTMN"] = Entry(Order("old", "ETHTMN", "BUY", "LIMIT", 100, 500),
                                    "maker", 100, .01, None, {}, 100, 0)
    c = {"symbol": "BTCTMN", "level": 100, "size_mult": 1}
    assert eng._notional(c, 1_000_000)[0] == 0  # 75% total exposure already reserved
    eng.account.exposure = 0
    eng.risk.max_open = 1
    assert eng._notional(c, 1_000_000)[0] == 0


def test_order_intent_is_durable_before_submission(tmp_path):
    eng, store = setup_engine(tmp_path)
    pos = eng.portfolio.open(Position("reversion", "BTCTMN", "BTC", "TMN", 1, 100, 1))

    async def place(order):
        saved = store.get_kv("execution_state")
        assert saved["exits"][pos.id]["order"]["client_id"] == order.client_id
        assert saved["exits"][pos.id]["order"]["status"] == "UNKNOWN"
        raise RuntimeError("simulated crash after send")

    eng.broker.place = place
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(eng._place_exit(pos, 110, 1, "target", "test", 1000))
    restored, _ = setup_engine(tmp_path)
    assert restored.exits[pos.id].order.status == "UNKNOWN"


def test_sell_fill_checkpoint_prevents_replay_after_restart(tmp_path):
    eng, store = setup_engine(tmp_path)
    pos = eng.portfolio.open(Position("reversion", "BTCTMN", "BTC", "TMN", 2, 100, 1))
    order = Order("id", "BTCTMN", "SELL", "LIMIT", 110, 2, filled_qty=1, avg_price=110)
    eng.exits[pos.id] = ExitOrder(order, pos.id, "target")
    eng._on_sell_fill(eng.exits[pos.id], 1000)
    restored, _ = setup_engine(tmp_path)
    assert restored.exits[pos.id].seen_qty == 1
    restored._on_sell_fill(restored.exits[pos.id], 1001)
    assert len(store.query("SELECT * FROM trades")) == 1
    assert restored.portfolio.positions[pos.id].qty == 1


def test_estimated_sell_fee_uses_incremental_notional():
    order = Order("id", "BTCTMN", "SELL", "LIMIT", 100, 2, filled_qty=1,
                  avg_price=100, fee_quote=.3)

    async def get_order(o):
        return parse_order({"executedQty": "2", "executedSum": "220", "status": "FILLED"}, o)

    broker = LiveBroker(SimpleNamespace(get_order=get_order), .0025, .003)
    asyncio.run(broker.refresh(order))
    assert order.fee_quote == pytest.approx(.66)


def test_fill_accounting_rolls_back_with_checkpoint_failure(tmp_path, monkeypatch):
    eng, store = setup_engine(tmp_path)
    pos = eng.portfolio.open(Position("reversion", "BTCTMN", "BTC", "TMN", 2, 100, 1))
    order = Order("id", "BTCTMN", "SELL", "LIMIT", 110, 2)
    eng.exits[pos.id] = ExitOrder(order, pos.id, "target")
    eng._save_execution()
    order.filled_qty, order.avg_price = 1, 110

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_execution", fail)
    with pytest.raises(OSError, match="disk full"):
        eng._on_sell_fill(eng.exits[pos.id], 1000)
    assert eng.execution_fault
    assert store.query("SELECT * FROM trades") == []
    restored, _ = setup_engine(tmp_path)
    assert restored.portfolio.positions[pos.id].qty == 2
    assert restored.exits[pos.id].seen_qty == 0
    restored.exits[pos.id].order.filled_qty = 1
    restored.exits[pos.id].order.avg_price = 110
    restored._on_sell_fill(restored.exits[pos.id], 1001)
    assert len(store.query("SELECT * FROM trades")) == 1


def test_recovery_does_not_adopt_external_holdings(tmp_path):
    from pishro.bot import Bot
    eng, _ = setup_engine(tmp_path)

    async def open_orders():
        return [{"clientOrderId": "external", "symbol": "ETHTMN"}]

    async def balances(refresh=False):
        return {"ETH": (10, 0), "TMN": (1000000, 0)}

    bot = Bot.__new__(Bot)
    bot.engine, bot.portfolio, bot.broker = eng, eng.portfolio, eng.broker
    bot.client = SimpleNamespace(open_orders=open_orders)
    eng.broker.balances = balances
    asyncio.run(bot._recover_execution())
    assert not bot.portfolio.positions


def test_recovery_refuses_account_api_failure(tmp_path):
    from pishro.bot import Bot
    eng, _ = setup_engine(tmp_path)

    async def open_orders():
        raise RuntimeError("account unavailable")

    bot = Bot.__new__(Bot)
    bot.engine = eng
    bot.portfolio = eng.portfolio
    bot.client = SimpleNamespace(open_orders=open_orders)
    with pytest.raises(RuntimeError, match="account unavailable"):
        asyncio.run(bot._recover_execution())


def test_only_one_process_can_own_journal(tmp_path):
    from pishro.execution_lock import ExecutionLease
    path = tmp_path / "execution.lock"
    lease = ExecutionLease(path)
    try:
        with pytest.raises(RuntimeError, match="another process"):
            ExecutionLease(path)
    finally:
        lease.close()
    lease = ExecutionLease(path)
    lease.close()


def test_market_outage_still_runs_risk_tick():
    from pishro.market.live import LiveMarket
    market = LiveMarket.__new__(LiveMarket)
    market.cfg = SimpleNamespace(get=lambda *a: .001)
    ticks = []

    async def run():
        stop = asyncio.Event()

        async def fail():
            stop.set()
            raise RuntimeError("market offline")

        async def tick():
            ticks.append(1)

        market.refresh_markets = market.refresh_refs = fail
        await market.run(stop, tick)

    asyncio.run(run())
    assert ticks == [1]


def test_buy_intent_and_fill_are_recoverable(tmp_path):
    eng, store = setup_engine(tmp_path)
    c = {"symbol": "BTCTMN", "level": 100, "edge": .01, "feats": {}, "fair": 101,
         "baseline": 0, "dev_now": -.01, "std": .01}

    async def place(o):
        assert store.get_kv("execution_state")["entries"]["BTCTMN"]["order"]["status"] == "UNKNOWN"
        o.status, o.filled_qty, o.avg_price = "PARTIALLY_FILLED", 1, 100
        return o

    eng.broker.place = place
    asyncio.run(eng._place_entry(c, 100, 2, "maker", 1000))
    restored, _ = setup_engine(tmp_path)
    assert restored.entries["BTCTMN"].seen_qty == 1
    restored._on_buy_fill(restored.entries["BTCTMN"], 1001)
    assert restored.portfolio.committed_base("BTC") == 1


def test_reported_fee_floor_survives_restart(tmp_path):
    eng, _ = setup_engine(tmp_path)
    o = Order("id", "BTCTMN", "BUY", "LIMIT", 100, 1, fee_reported=True)
    eng._detect_fee(o, .004)
    eng._save_execution()
    restored, _ = setup_engine(tmp_path)
    assert restored.broker.fee("BTCTMN") == (.004, .004)
    assert restored.broker.fee("ETHTMN") == (.0025, .003)


def test_previous_fee_report_does_not_cover_new_partial_fill():
    o = Order("id", "BTCTMN", "BUY", "LIMIT", 100, 2, filled_qty=1,
              avg_price=100, fee_reported=True, fee_base=.003)
    updated = parse_order({"status": "FILLED", "executedQty": "2", "executedSum": "200"}, o)
    assert not updated.fee_reported
    assert updated.fee_base == .003


def test_nonfinite_risk_input_is_rejected(tmp_path):
    eng, _ = setup_engine(tmp_path)
    assert not eng.risk.check_open(notional=float("nan"), equity=1000, exposure_total=0,
        exposure_base=0, n_open=0, free_quote_tmn=1000, data_age=0, api_errors=0).ok


def test_loss_cooldown_cannot_shorten_daily_pause(tmp_path):
    eng, _ = setup_engine(tmp_path)
    eng.risk.pause_reason, eng.risk.paused_until = "daily", 100000
    for _ in range(eng.risk.max_consec):
        eng.risk.on_trade_closed(-.01, 1000)
    assert eng.risk.pause_reason == "daily" and eng.risk.paused_until == 100000
