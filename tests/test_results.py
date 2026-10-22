from __future__ import annotations

import json

from wallex_engine.reporting.results import (
    decision_frame,
    decision_summary,
    load_sweeps,
    parse_report_tables,
)


def decision(mode: str, outcome: str, approve: bool = True, allow: bool = True) -> dict:
    return {
        "decision_id": f"{mode}-{outcome}-{approve}-{allow}",
        "run_id": f"{mode}-run",
        "created_at": "2026-10-22T19:30:00Z",
        "mode": mode,
        "symbol": "BTCTMN",
        "outcome": outcome,
        "reason_fa": "دلیل",
        "signal": {"action": "SKIP" if outcome == "skipped" else "ENTER_LONG", "strategy": "trend"},
        "regime": {"regime": "trend", "allow_entries": allow, "size_multiplier": "0.5",
                   "confidence": "0.7", "reason_fa": "رژیم"},
        "review": {"approve": approve, "size_adjustment": "1", "reason_fa": "بازبینی"},
        "risk": {"approved": outcome == "filled", "reason_fa": "ریسک"},
    }


def test_decision_frame_and_summary_count_ai_vetoes(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    rows = [
        decision("shadow", "skipped"),
        decision("shadow", "filled"),
        decision("shadow", "rejected", approve=False),
        decision("shadow", "rejected", allow=False),
        decision("backtest", "filled"),
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    frame = decision_frame(path)
    assert len(frame) == 5
    assert {"ai_regime", "ai_approve", "risk_reason"} <= set(frame.columns)
    summary = decision_summary(frame[frame["mode"] == "shadow"])
    assert summary == {"decisions": 4, "signals": 3, "filled": 1, "ai_vetoes": 2}


def test_decision_frame_handles_missing_file(tmp_path) -> None:
    assert decision_frame(tmp_path / "none.jsonl").empty


def test_report_tables_split_metrics_and_ai_comparison(tmp_path) -> None:
    report = tmp_path / "bt-X.md"
    report.write_text(
        "# گزارش\n\n| شاخص | مقدار |\n|---|---:|\n| بازده | 0.0125 |\n| تعداد معاملات | 3 |\n\n"
        "## مقایسه AI روشن/خاموش\n\n| شاخص | اختلاف |\n|---|---:|\n| اختلاف بازده | -0.001 |\n",
        encoding="utf-8",
    )
    metrics, comparison = parse_report_tables(report)
    assert metrics == {"بازده": 0.0125, "تعداد معاملات": 3.0}
    assert comparison == {"اختلاف بازده": -0.001}


def test_load_sweeps_reads_rows(tmp_path) -> None:
    (tmp_path / "sweep_15m.json").write_text(
        json.dumps({"split": "s", "rows": [{"signal_timeframe_minutes": 15, "in_ret_sum": 0.1}]}),
        encoding="utf-8",
    )
    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    sweeps = load_sweeps(tmp_path)
    assert list(sweeps) == ["sweep_15m"]
    assert sweeps["sweep_15m"][0]["in_ret_sum"] == 0.1


def test_trade_markers_pick_live_and_shadow_fills_and_ai_vetoes(tmp_path) -> None:
    from wallex_engine.reporting.results import trade_markers

    entry = decision("shadow", "filled")
    entry["signal"].update(entry_price="100", stop_price="95", target_price="115")
    exit_ = decision("live", "filled")
    exit_["signal"].update(action="EXIT", entry_price="110")
    vetoed = decision("shadow", "rejected", approve=False)
    vetoed["signal"]["entry_price"] = "105"
    ignored = [decision("backtest", "filled"), decision("shadow", "skipped")]
    other_symbol = decision("shadow", "filled")
    other_symbol["symbol"] = "ETHTMN"
    path = tmp_path / "decisions.jsonl"
    rows = [entry, exit_, vetoed, other_symbol, *ignored]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    markers = trade_markers(decision_frame(path), "BTCTMN")
    assert sorted(markers["kind"]) == ["entry", "exit", "vetoed"]
    assert markers.set_index("kind").loc["entry", "price"] == 100
    assert markers.set_index("kind").loc["exit", "price"] == 110


def test_equity_stats_measure_peak_to_trough_drawdown() -> None:
    import pandas as pd

    from wallex_engine.reporting.results import equity_curve

    snapshots = pd.DataFrame(
        {
            "run_id": ["live-a"] * 4 + ["shadow-b"],
            "captured_at": [
                "2026-10-23 08:00:00",
                "2026-10-23 09:00:00",
                "2026-10-23 10:00:00",
                "2026-10-23 11:00:00",
                "2026-10-23 08:00:00",
            ],
            "equity_tmn": ["5000000", "5100000", "4998000", "5050000", "5000000"],
        }
    )
    curve, stats = equity_curve(snapshots, "live-a", 5_000_000)
    assert list(curve["equity"]) == [5_000_000, 5_100_000, 4_998_000, 5_050_000]
    assert round(curve["drawdown"].max(), 4) == 0.02
    assert round(stats["return"], 4) == 0.01
    assert round(stats["max_drawdown"], 4) == 0.02
    assert stats["hours"] == 3


def test_find_decision_prefers_requested_time_then_latest(tmp_path) -> None:
    from wallex_engine.reporting.results import find_decision

    first = decision("shadow", "skipped")
    second = decision("shadow", "filled")
    second["created_at"] = "2026-10-22T19:45:00Z"
    backtest = decision("backtest", "filled")
    path = tmp_path / "decisions.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (first, second, backtest)), encoding="utf-8")
    assert find_decision(path, "BTCTMN", modes={"shadow"})["outcome"] == "filled"
    at = find_decision(path, "BTCTMN", at="2026-10-22T19:30:00Z", modes={"shadow"})
    assert at["outcome"] == "skipped"
    assert find_decision(path, "ETHTMN") is None
