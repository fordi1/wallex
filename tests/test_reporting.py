from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd

from wallex_engine.backtest.engine import BacktestResult
from wallex_engine.reporting.generator import generate_backtest_report, performance_metrics


def test_report_contains_drawdown_trade_markers_and_persian_labels(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    equity = pd.DataFrame(
        {
            "captured_at": [start, start + timedelta(minutes=5), start + timedelta(minutes=10)],
            "equity_tmn": [5_000_000, 5_050_000, 5_025_000],
        }
    )
    trades = pd.DataFrame(
        {
            "opened_at": [start],
            "closed_at": [start + timedelta(minutes=10)],
            "entry": [100],
            "exit": [101],
            "quantity": [10],
            "pnl_tmn": [10],
        }
    )
    result = BacktestResult("qa-report", equity, trades, decisions=1, rejects=0, fallbacks=0)

    markdown, html = generate_backtest_report(result, tmp_path, 5_000_000)

    assert "ارزش پایانی سبد" in markdown.read_text(encoding="utf-8")
    html_text = html.read_text(encoding="utf-8")
    for label in ("افت سرمایه", "ورود", "خروج"):
        assert json.dumps(label, ensure_ascii=True)[1:-1] in html_text
    assert performance_metrics(equity, trades, 5_000_000)["trade_count"] == 1
