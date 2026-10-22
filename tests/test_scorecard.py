from __future__ import annotations

import json

from sqlalchemy import create_engine, text

from wallex_engine.config import AppConfig
from wallex_engine.reporting.scorecard import risk_state, run_scorecard
from wallex_engine.storage.database import Base


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'engine.db'}")
    Base.metadata.create_all(engine)
    rows = {
        "decisions": [
            ("d1", "live-1", "2026-10-23 09:00:00", "live", "BTCTMN", "filled", "ورود", "{}"),
            ("d2", "live-1", "2026-10-23 10:00:00", "live", "BTCTMN", "filled", "خروج", "{}"),
            ("s1", "shadow-1", "2026-10-23 08:00:00", "shadow", "BTCTMN", "filled", "ورود", "{}"),
        ],
        "order_intents": [
            ("c1", "d1", "BTCTMN", "2026-10-23 09:00:00", "{}"),
            ("c2", "d2", "BTCTMN", "2026-10-23 10:00:00", "{}"),
            ("c9", "s1", "BTCTMN", "2026-10-23 08:00:00", "{}"),
        ],
        "fills": [
            ("f1", "c1", "BTCTMN", "BUY", "0.01", "100000000", "2500", "2026-10-23 09:00:02", "{}"),
            ("f2", "c2", "BTCTMN", "SELL", "0.01", "101000000", "3030",
             "2026-10-23 10:00:04", "{}"),
            ("f9", "c9", "BTCTMN", "BUY", "1", "100000000", "1", "2026-10-23 08:00:01", "{}"),
            # Real Wallex trades carry no client id: attributed by the run's time window.
            ("wx-1", "", "BTCTMN", "BUY", "0.001", "100000000", "250", "2026-10-23 09:40:00", "{}"),
            ("wx-2", "", "BTCTMN", "BUY", "0.001", "100000000", "250", "2026-10-22 23:00:00", "{}"),
        ],
    }
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO decisions (decision_id, run_id, created_at, mode, symbol, outcome,"
            " reason_fa, payload_json) VALUES (:a,:b,:c,:d,:e,:f,:g,:h)"),
            [dict(zip("abcdefgh", r, strict=True)) for r in rows["decisions"]])
        connection.execute(text(
            "INSERT INTO order_intents (client_id, decision_id, symbol, created_at, payload_json)"
            " VALUES (:a,:b,:c,:d,:e)"),
            [dict(zip("abcde", r, strict=True)) for r in rows["order_intents"]])
        connection.execute(text(
            "INSERT INTO fills (fill_id, client_id, symbol, side, quantity, price, fee_tmn,"
            " filled_at, payload_json) VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i)"),
            [dict(zip("abcdefghi", r, strict=True)) for r in rows["fills"]])
        for at, equity, drawdown in (
            ("2026-10-23 09:00:00", "5000000", "0"),
            ("2026-10-23 09:30:00", "4990000", "0.002"),
            ("2026-10-23 10:00:05", "5004470", "0"),
        ):
            payload = {"equity_tmn": equity, "cash_tmn": equity, "positions_tmn": "0",
                       "drawdown": drawdown, "daily_pnl": "0.0009", "peak_equity_tmn": "5004470"}
            connection.execute(text(
                "INSERT INTO equity_snapshots (run_id, captured_at, equity_tmn, drawdown,"
                " daily_pnl, payload_json) VALUES ('live-1', :t, :e, :d, '0.0009', :p)"),
                {"t": at, "e": equity, "d": drawdown, "p": json.dumps(payload)})
    return engine


def test_scorecard_counts_only_the_selected_run(tmp_path) -> None:
    engine = _db(tmp_path)
    with engine.connect() as connection:
        card = run_scorecard(connection, "live", 5_000_000, tmp_path)
    assert card["run_id"] == "live-1"
    assert card["fills"] == 3 and card["round_trips"] == 1
    assert card["volume_tmn"] == 2_110_000
    assert card["fees_tmn"] == 5_780
    assert card["latency_median_s"] == 3
    assert round(card["return"], 6) == round(5_004_470 / 5_000_000 - 1, 6)
    assert round(card["max_drawdown"], 4) == 0.002


def test_risk_state_reports_limits_usage_and_pnl(tmp_path) -> None:
    engine = _db(tmp_path)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO positions (symbol, quantity, average_entry, updated_at, payload_json)"
            " VALUES ('BTCTMN', '0.01', '100000000', '2026-10-23 10:00:00', :p)"),
            {"p": json.dumps({"symbol": "BTCTMN", "quantity": "0.01", "average_entry": "100000000",
                              "stop_price": "98000000", "target_price": "106000000"})})
    with engine.connect() as connection:
        state = risk_state(connection, AppConfig(), "live", {"BTCTMN": 101_000_000})
    position = state["positions"][0]
    assert position["stop_price"] == 98_000_000 and position["unrealized_tmn"] == 10_000
    assert state["pnl"]["total_tmn"] == 4_470
    assert state["pnl"]["realized_tmn"] == 4_470 - 10_000
    limits = {limit["key"]: limit for limit in state["limits"]}
    assert {"daily_loss", "kill_switch", "total_exposure", "positions"} <= set(limits)
    assert limits["kill_switch"]["limit"] == 0.10
