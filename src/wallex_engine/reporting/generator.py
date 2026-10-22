from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import text

from ..backtest.engine import BacktestResult
from ..storage.database import SqlEventStore


def generate_backtest_report(
    result: BacktestResult,
    reports_dir: Path,
    baseline: float,
    comparison: dict[str, float] | None = None,
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    metrics = performance_metrics(result.equity, result.trades, baseline)
    metrics.update({"reject_count": result.rejects, "fallback_count": result.fallbacks})
    markdown_path = reports_dir / f"{result.run_id}.md"
    html_path = reports_dir / f"{result.run_id}.html"
    markdown_path.write_text(_markdown(result.run_id, metrics, comparison), encoding="utf-8")
    figure = _equity_figure(result.equity, result.trades, baseline)
    summary = _html_summary(metrics, comparison)
    figure.write_html(
        html_path,
        include_plotlyjs="cdn",
        full_html=True,
        post_script=f"document.body.insertAdjacentHTML('afterbegin', {json.dumps(summary)});",
    )
    return markdown_path, html_path


async def generate_database_report(
    store: SqlEventStore, run_id: str, reports_dir: Path, baseline: float
) -> tuple[Path, Path]:
    async with store.engine.connect() as connection:
        equity_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT captured_at, equity_tmn, drawdown, daily_pnl "
                        "FROM equity_snapshots WHERE run_id=:run_id ORDER BY captured_at"
                    ),
                    {"run_id": run_id},
                )
            )
            .mappings()
            .all()
        )
        decisions = (
            (
                await connection.execute(
                    text("SELECT outcome, payload_json FROM decisions WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
            )
            .mappings()
            .all()
        )
    equity = pd.DataFrame(equity_rows)
    if not equity.empty:
        equity["equity_tmn"] = pd.to_numeric(equity["equity_tmn"])
    trades = pd.DataFrame()
    result = BacktestResult(
        run_id=run_id,
        equity=equity,
        trades=trades,
        decisions=len(decisions),
        rejects=sum(row["outcome"].startswith("reject") for row in decisions),
        fallbacks=sum("fallback" in row["payload_json"] for row in decisions),
    )
    return generate_backtest_report(result, reports_dir, baseline)


def performance_metrics(
    equity: pd.DataFrame, trades: pd.DataFrame, baseline: float
) -> dict[str, float]:
    if equity.empty:
        return {key: 0.0 for key in _metric_keys()}
    values = pd.to_numeric(equity["equity_tmn"], errors="coerce").dropna()
    returns = values.pct_change().dropna()
    downside = returns[returns < 0]
    annualizer = np.sqrt(365 * 24 * 12)
    sharpe = float(returns.mean() / returns.std(ddof=0) * annualizer) if returns.std(ddof=0) else 0
    sortino = (
        float(returns.mean() / downside.std(ddof=0) * annualizer)
        if not downside.empty and downside.std(ddof=0)
        else 0
    )
    running_peak = values.cummax()
    max_drawdown = float(((running_peak - values) / running_peak).max())
    if trades.empty:
        pnls = pd.Series(dtype=float)
        turnover = exposure_time = 0.0
    else:
        pnls = pd.to_numeric(trades["pnl_tmn"], errors="coerce").dropna()
        notionals = pd.to_numeric(trades["entry"], errors="coerce") * pd.to_numeric(
            trades["quantity"], errors="coerce"
        )
        turnover = float(notionals.sum() / baseline)
        opened = pd.to_datetime(trades["opened_at"], utc=True)
        closed = pd.to_datetime(trades["closed_at"], utc=True)
        total_span = pd.to_datetime(equity["captured_at"], utc=True)
        duration = (total_span.max() - total_span.min()).total_seconds()
        exposure_time = (
            float((closed - opened).dt.total_seconds().sum() / duration) if duration else 0
        )
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    profit_factor = (
        float(wins.sum() / abs(losses.sum()))
        if not losses.empty
        else (float("inf") if not wins.empty else 0)
    )
    return {
        "ending_equity_tmn": float(values.iloc[-1]),
        "realized_pnl_tmn": float(pnls.sum()) if not pnls.empty else 0,
        "return": float(values.iloc[-1] / baseline - 1),
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "sortino": sortino,
        "win_rate": float((pnls > 0).mean()) if not pnls.empty else 0,
        "profit_factor": profit_factor,
        "average_trade_tmn": float(pnls.mean()) if not pnls.empty else 0,
        "turnover": turnover,
        "exposure_time": exposure_time,
        "trade_count": float(len(pnls)),
    }


def _metric_keys() -> tuple[str, ...]:
    return (
        "ending_equity_tmn",
        "realized_pnl_tmn",
        "return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "win_rate",
        "profit_factor",
        "average_trade_tmn",
        "turnover",
        "exposure_time",
        "trade_count",
    )


def _equity_figure(equity: pd.DataFrame, trades: pd.DataFrame, baseline: float) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.72, 0.28],
        subplot_titles=("ارزش سبد", "افت سرمایه"),
    )
    if not equity.empty:
        ordered = equity.copy()
        ordered["captured_at"] = pd.to_datetime(ordered["captured_at"], utc=True)
        ordered = ordered.sort_values("captured_at")
        values = pd.to_numeric(ordered["equity_tmn"], errors="coerce")
        running_peak = values.cummax()
        drawdown = (running_peak - values) / running_peak
        figure.add_trace(
            go.Scatter(
                x=ordered["captured_at"],
                y=values,
                name="ارزش سبد",
                line={"color": "#007863", "width": 2},
            ),
            row=1,
            col=1,
        )
        figure.add_hline(
            y=baseline,
            line_dash="dash",
            line_color="#616671",
            annotation_text="سرمایه آغازین",
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=ordered["captured_at"],
                y=drawdown,
                name="افت سرمایه",
                mode="lines",
                fill="tozeroy",
                line={"color": "#b42318", "width": 1.5},
                hovertemplate="%{y:.2%}<extra></extra>",
            ),
            row=2,
            col=1,
        )
        if not trades.empty:
            times = pd.DatetimeIndex(ordered["captured_at"])
            value_series = pd.Series(values.to_numpy(), index=times)
            _add_trade_markers(figure, trades, value_series)
    figure.update_layout(
        title="عملکرد اجرای معاملاتی",
        height=640,
        template="plotly_white",
        font={"family": "Vazirmatn, Tahoma, sans-serif"},
        margin={"l": 48, "r": 24, "t": 64, "b": 48},
        hovermode="x unified",
    )
    figure.update_yaxes(title_text="تومان", row=1, col=1)
    figure.update_yaxes(tickformat=".1%", row=2, col=1)
    return figure


def _add_trade_markers(
    figure: go.Figure,
    trades: pd.DataFrame,
    equity_values: pd.Series,
) -> None:
    for column, name, symbol, color in (
        ("opened_at", "ورود", "triangle-up", "#007863"),
        ("closed_at", "خروج", "triangle-down", "#b42318"),
    ):
        event_times = pd.to_datetime(trades[column], utc=True)
        marker_values = [
            float(equity_values.asof(timestamp))
            if not pd.isna(equity_values.asof(timestamp))
            else float(equity_values.iloc[0])
            for timestamp in event_times
        ]
        figure.add_trace(
            go.Scatter(
                x=event_times,
                y=marker_values,
                name=name,
                mode="markers",
                marker={"symbol": symbol, "size": 10, "color": color},
            ),
            row=1,
            col=1,
        )


METRIC_LABELS = {
    "ending_equity_tmn": "ارزش پایانی سبد (تومان)",
    "realized_pnl_tmn": "سود و زیان تحقق‌یافته (تومان)",
    "return": "بازده",
    "max_drawdown": "بیشترین افت سرمایه",
    "sharpe": "نسبت شارپ",
    "sortino": "نسبت سورتینو",
    "win_rate": "نرخ برد",
    "profit_factor": "ضریب سود",
    "average_trade_tmn": "میانگین هر معامله (تومان)",
    "turnover": "گردش سرمایه",
    "exposure_time": "زمان در معرض بازار",
    "trade_count": "تعداد معاملات",
    "reject_count": "تعداد ردها",
    "fallback_count": "تعداد مسیرهای جایگزین AI",
}


def _metric_label(key: str) -> str:
    return METRIC_LABELS.get(key, key)


def _markdown(run_id: str, metrics: dict[str, float], comparison: dict[str, float] | None) -> str:
    lines = [f"# گزارش اجرای {run_id}", "", "| شاخص | مقدار |", "|---|---:|"]
    lines.extend(f"| {_metric_label(key)} | {value:.6g} |" for key, value in metrics.items())
    if comparison:
        lines.extend(["", "## مقایسه AI روشن/خاموش", "", "| شاخص | اختلاف |", "|---|---:|"])
        lines.extend(
            f"| {_metric_label(key)} | {value:.6g} |" for key, value in comparison.items()
        )
    return "\n".join(lines) + "\n"


def _html_summary(metrics: dict[str, float], comparison: dict[str, float] | None) -> str:
    rows = "".join(
        f"<tr><th>{_metric_label(key)}</th><td>{value:.6g}</td></tr>"
        for key, value in metrics.items()
    )
    comparison_html = ""
    if comparison:
        comparison_html = (
            "<h2>مقایسه AI روشن/خاموش</h2><pre>"
            + json.dumps(comparison, ensure_ascii=False, indent=2)
            + "</pre>"
        )
    return (
        "<section dir='rtl' lang='fa' style='font-family:Vazirmatn,Tahoma,sans-serif;"
        "max-width:1100px;margin:auto;padding:24px'><h1>گزارش اجرای معاملاتی</h1>"
        f"<table>{rows}</table>{comparison_html}</section>"
    )
