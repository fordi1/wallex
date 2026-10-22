# ruff: noqa: E501
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import jdatetime  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402
from plotly.subplots import make_subplots  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from wallex_engine.app import build_ai_service  # noqa: E402
from wallex_engine.config import RuntimeSecrets, load_config  # noqa: E402
from wallex_engine.models import MarketSpec, SignalAction  # noqa: E402
from wallex_engine.reporting.results import (  # noqa: E402
    decision_frame,
    decision_summary,
    load_sweeps,
    parse_report_tables,
    trade_markers,
)
from wallex_engine.runtime.control import (  # noqa: E402
    EngineController,
    preflight,
    read_env_file,
    tail,
)
from wallex_engine.ui.market_explorer import (  # noqa: E402
    MarketExplorerView,
    WatchQuote,
    chart_indicator_frame,
    fetch_active_tmn_markets,
    fetch_market_view,
    fetch_watchlist_quotes,
)

# Local runs read .env directly; values already exported by the host win.
for _key, _value in read_env_file(ROOT / ".env").items():
    if _value:
        os.environ.setdefault(_key, _value)

PERSIAN_DIGITS = str.maketrans("0123456789.-", "۰۱۲۳۴۵۶۷۸۹٫−")
TEHRAN = ZoneInfo("Asia/Tehran")
CHART_PRIMARY = "#007863"
CHART_SUCCESS = "#007e4c"
CHART_DESTRUCTIVE = "#c01d20"
CHART_INFO = "#2d74ca"
CHART_WARNING = "#c78200"
CHART_ACCENT = "#ab4b90"
CHART_MUTED = "#616671"
CHART_FOREGROUND = "#151923"
CHART_GRID = "#e1e3e7"
CHART_SURFACE = "#ffffff"


def format_number(value: object, decimals: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    rendered = f"{number:,.{decimals}f}"
    return rendered.translate(PERSIAN_DIGITS).replace(",", "٬")


def format_money(value: object) -> str:
    return f"{format_number(value)}\u00a0تومان"


def format_percent(value: object) -> str:
    try:
        return f"{format_number(float(value) * 100, 2)}٪"
    except (TypeError, ValueError):
        return "—"


def format_datetime(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(UTC)
    local = parsed.tz_convert(TEHRAN).to_pydatetime()
    jalali = jdatetime.datetime.fromgregorian(datetime=local)
    return jalali.strftime("%Y/%m/%d، %H:%M").translate(PERSIAN_DIGITS)


def isolate_ltr(value: object) -> str:
    """Unicode-isolate technical identifiers inside RTL tables."""

    return f"\u2066{value}\u2069"


def format_duration(seconds: object) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{format_number(hours)} ساعت و {format_number(minutes)} دقیقه"


def require_password() -> None:
    expected = os.getenv("DASHBOARD_PASSWORD")
    if not expected:
        st.error("رمز داشبورد تنظیم نشده است. متغیر DASHBOARD_PASSWORD را ثبت کنید.")
        st.stop()
    if st.session_state.get("authenticated"):
        return
    st.title("ورود به پایش معاملات")
    password = st.text_input("رمز دسترسی", type="password")
    if st.button("ورود", type="primary", width="stretch"):
        supplied = hashlib.sha256(password.encode()).digest()
        target = hashlib.sha256(expected.encode()).digest()
        if hmac.compare_digest(supplied, target):
            st.session_state.authenticated = True
            st.rerun()
        st.error("رمز دسترسی درست نیست. دوباره تلاش کنید.")
    st.stop()


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700&display=swap');
        :root {
          --background: oklch(0.990 0.002 265.0);
          --foreground: oklch(0.215 0.021 265.0);
          --surface: oklch(1.000 0.000 265.0);
          --surface-muted: oklch(0.972 0.004 265.0);
          --primary: oklch(0.500 0.115 178.0);
          --muted-foreground: oklch(0.510 0.019 265.0);
          --border: oklch(0.915 0.006 265.0);
          --destructive: oklch(0.520 0.196 27.0);
          --success: oklch(0.520 0.125 158.0);
          --warning: oklch(0.620 0.150 75.0);
          --info: oklch(0.520 0.130 252.0);
          --space-2: 8px; --space-3: 12px; --space-4: 16px;
          --space-6: 24px; --space-8: 32px;
          --radius-md: 8px; --radius-lg: 12px; --radius-xl: 16px;
        }
        html, body, [class*="st-"] { font-family: Vazirmatn, Tahoma, Arial, sans-serif; }
        [data-testid="stIconMaterial"], .material-symbols-rounded {
          font-family:"Material Symbols Rounded" !important;
        }
        html, body, [data-testid="stAppViewContainer"] { direction: rtl; color: var(--foreground); }
        [data-testid="stAppViewContainer"] { background: var(--background); }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stMetric"] {
          background: var(--surface); border: 1px solid var(--border);
          border-radius: var(--radius-lg); padding: var(--space-4);
          min-height: 108px;
        }
        [data-testid="stMetricValue"] {
          direction: rtl; unicode-bidi: isolate;
          font-variant-numeric: tabular-nums;
          font-size:30px !important;
        }
        .status-strip {
          display:flex; flex-wrap:wrap; gap:var(--space-2); padding:var(--space-3) var(--space-4);
          background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg);
          margin-block-end:var(--space-6);
        }
        .status-item { color:var(--muted-foreground); }
        .status-ok { color:var(--success); font-weight:600; }
        .status-alert { color:var(--destructive); font-weight:600; }
        .ltr { direction:ltr; unicode-bidi:isolate; display:inline-block; }
        .readonly-banner {
          display:flex; align-items:center; gap:var(--space-3);
          padding:var(--space-3) var(--space-4);
          color:var(--foreground); background:color-mix(in oklch, var(--info) 8%, white);
          border:1px solid color-mix(in oklch, var(--info) 30%, white);
          border-radius:var(--radius-lg); margin-block:var(--space-4) var(--space-6);
        }
        .readonly-dot {
          width:9px; height:9px; border-radius:50%; background:var(--info); flex:0 0 auto;
        }
        .terminal-head {
          display:flex; align-items:flex-start; justify-content:space-between; gap:var(--space-4);
          padding:var(--space-4) var(--space-6); background:var(--surface);
          border:1px solid var(--border); border-radius:var(--radius-xl); margin-block-end:var(--space-4);
        }
        .terminal-brand { display:flex; align-items:center; gap:var(--space-4); min-width:0; }
        .terminal-mark {
          display:grid; place-items:center; width:42px; height:42px; border-radius:12px;
          color:white; background:var(--primary); font-weight:800; letter-spacing:-1px;
        }
        .terminal-title { margin:0; font-size:20px; line-height:1.45; }
        .terminal-subtitle { margin:2px 0 0; color:var(--muted-foreground); font-size:13px; }
        .live-pill {
          display:inline-flex; align-items:center; gap:7px; white-space:nowrap;
          padding:7px 11px; border-radius:999px; color:var(--success);
          background:color-mix(in oklch, var(--success) 8%, white);
          border:1px solid color-mix(in oklch, var(--success) 24%, white); font-size:13px; font-weight:600;
        }
        .live-pill::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--success); }
        .quote-strip {
          display:flex; flex-wrap:wrap; align-items:baseline; gap:var(--space-3) var(--space-6);
          padding:var(--space-4) var(--space-6); background:var(--surface);
          border:1px solid var(--border); border-radius:var(--radius-xl); margin-block-end:var(--space-4);
        }
        .quote-symbol { font-weight:700; font-size:18px; direction:ltr; unicode-bidi:isolate; }
        .quote-price { font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }
        .quote-meta { color:var(--muted-foreground); font-size:13px; }
        .panel-title {
          display:flex; align-items:center; justify-content:space-between; gap:var(--space-3);
          margin:0 0 var(--space-3); font-size:16px; font-weight:700;
        }
        .panel-kicker { color:var(--muted-foreground); font-size:12px; font-weight:500; }
        .watchlist {
          background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg);
          overflow:hidden; margin-block-end:var(--space-4);
        }
        .watch-row {
          display:grid; grid-template-columns:minmax(82px,1fr) minmax(96px,1.2fr) minmax(64px,.75fr);
          gap:var(--space-2); align-items:center; min-height:48px; padding:8px 12px;
          border-block-end:1px solid var(--border); font-variant-numeric:tabular-nums;
        }
        .watch-row:last-child { border-block-end:0; }
        .watch-row.active { background:color-mix(in oklch, var(--primary) 7%, white); box-shadow:inset -3px 0 var(--primary); }
        .watch-symbol { direction:ltr; unicode-bidi:isolate; text-align:left; font-weight:700; }
        .watch-price { text-align:left; white-space:nowrap; }
        .watch-spread { color:var(--muted-foreground); text-align:left; font-size:12px; white-space:nowrap; }
        .depth-book {
          background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg);
          overflow:hidden;
        }
        .depth-head, .depth-row {
          display:grid; grid-template-columns:1fr 1fr 1fr; gap:var(--space-2);
          padding:7px 10px; font-variant-numeric:tabular-nums;
        }
        .depth-head { color:var(--muted-foreground); font-size:12px; border-block-end:1px solid var(--border); }
        .depth-row { position:relative; font-size:13px; min-height:34px; align-items:center; }
        .depth-row > span { position:relative; z-index:1; white-space:nowrap; }
        .depth-fill { position:absolute; inset-block:2px; inset-inline-end:0; opacity:.09; border-radius:3px; }
        .depth-fill.ask { background:var(--destructive); }
        .depth-fill.bid { background:var(--success); }
        .depth-price.ask { color:var(--destructive); }
        .depth-price.bid { color:var(--success); }
        .depth-mid {
          display:flex; justify-content:space-between; gap:var(--space-3); padding:10px;
          background:var(--surface-muted); border-block:1px solid var(--border); font-weight:700;
        }
        .chart-shell {
          background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-xl);
          padding:4px 8px 0; overflow:hidden;
        }
        .workspace-note { color:var(--muted-foreground); font-size:12px; margin-block:5px 14px; }
        .terminal-section { margin-block-start:var(--space-4); }
        .signal-card {
          padding:var(--space-6); background:var(--surface); border:1px solid var(--border);
          border-radius:var(--radius-xl); min-height:168px;
        }
        .signal-label {
          color:var(--muted-foreground); font-size:14px; margin-block-end:var(--space-2);
        }
        .signal-value { font-size:20px; font-weight:700; margin-block-end:var(--space-3); }
        .signal-reason { color:var(--muted-foreground); line-height:1.8; margin:0; }
        [data-baseweb="tab-list"] { overflow-x:auto; scrollbar-width:thin; }
        .block-container { max-width: 1600px; padding-block-start: var(--space-4); padding-inline:var(--space-6); }
        button, input { min-height: 44px; }
        button:focus-visible, input:focus-visible {
          outline:3px solid color-mix(in oklch, var(--primary) 35%, transparent);
          outline-offset:2px;
        }
        [data-testid="stDataFrame"] {
          direction:rtl; border:1px solid var(--border);
          border-radius:var(--radius-lg); overflow:auto;
        }
        @media (max-width: 900px) {
          .terminal-head { padding:var(--space-4); }
          .quote-strip { padding:var(--space-4); }
          [data-testid="stHorizontalBlock"] { flex-wrap:wrap; }
          [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 calc(50% - var(--space-3)) !important;
            width:calc(50% - var(--space-3)) !important;
            min-width:calc(50% - var(--space-3)) !important;
          }
        }
        @media (max-width: 390px) {
          .block-container { padding-inline: var(--space-3); padding-block-start: var(--space-4); }
          h1 { font-size: 24px !important; line-height:1.4 !important; }
          .status-strip { display:block; }
          .status-item { margin-block:var(--space-2); }
          .signal-card { padding:var(--space-4); }
          .terminal-head { display:block; }
          .live-pill { margin-block-start:var(--space-3); }
          .terminal-mark { display:none; }
          .quote-strip { gap:6px var(--space-4); }
          .quote-price { width:100%; font-size:24px; }
          .watch-row { grid-template-columns:minmax(75px,1fr) minmax(90px,1.2fr) minmax(58px,.7fr); padding-inline:8px; }
          .depth-head, .depth-row { padding-inline:8px; font-size:12px; }
          [data-testid="stHorizontalBlock"] { flex-wrap:wrap; gap:var(--space-3); }
          [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; min-width:100% !important;
          }
          [data-testid="stMetric"] { min-height:96px; }
        }
        .stButton > button { white-space:nowrap; min-height:44px; }
        .engine-log { direction:ltr; text-align:left; }
        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after {
            animation-duration:0.01ms !important;
            transition-duration:0.01ms !important;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def database_engine():
    url = os.getenv("DASHBOARD_DATABASE_URL", "sqlite:///./data/wallex.db")
    if "+aiosqlite" in url:
        url = url.replace("+aiosqlite", "")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    return create_engine(url, pool_pre_ping=True)


def read_data() -> dict[str, pd.DataFrame]:
    engine = database_engine()
    queries = {
        "equity": "SELECT * FROM equity_snapshots ORDER BY captured_at DESC LIMIT 2000",
        "positions": "SELECT * FROM positions ORDER BY symbol",
        "decisions": "SELECT * FROM decisions ORDER BY created_at DESC LIMIT 100",
        "state": "SELECT * FROM service_state",
        "kill": "SELECT * FROM kill_switch_state LIMIT 1",
        "ai": "SELECT * FROM ai_calls ORDER BY created_at DESC LIMIT 1",
    }
    with engine.connect() as connection:
        return {name: pd.read_sql(text(query), connection) for name, query in queries.items()}


def chart(equity: pd.DataFrame, baseline: Decimal) -> go.Figure:
    ordered = equity.sort_values("captured_at")
    values = pd.to_numeric(ordered["equity_tmn"])
    minimum = min(float(values.min()), float(baseline))
    maximum = max(float(values.max()), float(baseline))
    if minimum == maximum:
        tick_values = [minimum]
    else:
        step = (maximum - minimum) / 4
        tick_values = [minimum + step * index for index in range(5)]
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=ordered["captured_at"].map(format_datetime),
            y=values,
            customdata=values.map(format_money),
            name="ارزش سبد",
            mode="lines",
            line={"color": CHART_PRIMARY, "width": 2},
            hovertemplate="%{customdata}<extra></extra>",
        )
    )
    figure.add_hline(
        y=float(baseline), line_dash="dash", line_color="#616671", annotation_text="سرمایه آغازین"
    )
    figure.update_layout(
        height=380,
        margin={"l": 12, "r": 12, "t": 24, "b": 24},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=CHART_SURFACE,
        font={"family": "Vazirmatn, Tahoma, sans-serif", "color": CHART_FOREGROUND},
        hoverlabel={"align": "right"},
        showlegend=False,
        xaxis={"showgrid": False, "title": None, "type": "category"},
        yaxis={
            "gridcolor": CHART_GRID,
            "title": None,
            "tickvals": tick_values,
            "ticktext": [format_number(value) for value in tick_values],
        },
    )
    return figure


def render_dashboard(config) -> None:
    st.title("پایش موتور معاملاتی Wallex")
    st.caption("نمای فقط‌خواندنی وضعیت، ریسک و تصمیم‌های معاملاتی")
    try:
        with st.spinner("در حال بارگیری داده‌های پایش…"):
            data = read_data()
    except Exception:
        st.error("داده‌های پایش بارگیری نشد. اتصال پایگاه‌داده را بررسی کنید.")
        return
    equity = data["equity"]
    if equity.empty:
        st.info("هنوز داده‌ای ثبت نشده است. پس از آغاز اجرای سایه، این صفحه تکمیل می‌شود.")
        return
    latest = equity.iloc[0]
    captured = pd.Timestamp(latest["captured_at"])
    if captured.tzinfo is None:
        captured = captured.tz_localize(UTC)
    age = datetime.now(tz=UTC) - captured.to_pydatetime()
    kill_active = not data["kill"].empty and bool(data["kill"].iloc[0]["active"])
    freshness_class = "status-ok" if age.total_seconds() < 120 else "status-alert"
    freshness = format_number(age.total_seconds(), 0)
    kill_class = "status-alert" if kill_active else "status-ok"
    kill_label = "فعال" if kill_active else "غیرفعال"
    captured_label = format_datetime(captured)
    st.markdown(
        f"""
        <div class="status-strip" role="status" aria-label="وضعیت سرویس">
          <span class="status-item">تازگی داده:
            <b class="{freshness_class}">{freshness} ثانیه</b>
          </span>
          <span class="status-item">توقف اضطراری:
            <b class="{kill_class}">{kill_label}</b>
          </span>
          <span class="status-item">آخرین ثبت: <bdi>{captured_label}</bdi></span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    columns = st.columns(4)
    columns[0].metric("ارزش سبد", format_money(latest["equity_tmn"]))
    columns[1].metric("سرمایه آغازین", format_money(config.challenge.initial_capital_tmn))
    columns[2].metric("افت سرمایه", format_percent(latest["drawdown"]))
    columns[3].metric("سود و زیان روز", format_percent(latest["daily_pnl"]))

    st.subheader("روند ارزش سبد")
    st.plotly_chart(
        chart(equity, config.challenge.initial_capital_tmn),
        width="stretch",
        config={"displayModeBar": False, "responsive": True},
    )
    with st.expander("جدول داده‌های نمودار"):
        table = equity[["captured_at", "equity_tmn", "drawdown"]].copy()
        table.columns = ["زمان", "ارزش سبد", "افت سرمایه"]
        table["زمان"] = table["زمان"].map(format_datetime)
        table["ارزش سبد"] = table["ارزش سبد"].map(format_money)
        table["افت سرمایه"] = table["افت سرمایه"].map(format_percent)
        st.dataframe(table, hide_index=True, width="stretch")

    left, right = st.columns([1, 1])
    with left:
        st.subheader("موقعیت‌های باز")
        if data["positions"].empty:
            st.caption("اکنون موقعیت بازی وجود ندارد.")
        else:
            positions = data["positions"][["symbol", "quantity", "average_entry"]].copy()
            positions.columns = ["نماد", "مقدار", "میانگین ورود"]
            positions["نماد"] = positions["نماد"].map(isolate_ltr)
            positions["مقدار"] = positions["مقدار"].map(lambda x: format_number(x, 8))
            positions["میانگین ورود"] = positions["میانگین ورود"].map(format_money)
            st.dataframe(positions, hide_index=True, width="stretch")
    with right:
        st.subheader("وضعیت هوش مصنوعی")
        if data["ai"].empty:
            st.caption("هنوز فراخوانی هوش مصنوعی ثبت نشده است.")
        else:
            row = data["ai"].iloc[0]
            st.write("تأمین‌کننده", isolate_ltr(row["provider"]))
            st.write("مدل", isolate_ltr(row["model"]))
            st.write("مسیر جایگزین", "فعال" if row["fallback"] else "غیرفعال")
            try:
                ai_payload = json.loads(row["payload_json"] or "{}")
                regime = ai_payload.get("result", {}).get("regime")
            except (TypeError, ValueError):
                regime = None
            if regime:
                st.write("رژیم بازار", isolate_ltr(regime))

    st.subheader("تصمیم‌های اخیر")
    decisions = data["decisions"]
    if decisions.empty:
        st.caption("هنوز تصمیم معاملاتی ثبت نشده است.")
    else:
        visible = decisions[["created_at", "symbol", "outcome", "reason_fa"]].copy()
        visible.columns = ["زمان", "نماد", "نتیجه", "دلیل"]
        visible["زمان"] = visible["زمان"].map(format_datetime)
        visible["نماد"] = visible["نماد"].map(isolate_ltr)
        st.dataframe(visible, hide_index=True, width="stretch")

    state = {row["key"]: row["value"] for _, row in data["state"].iterrows()}
    if state.get("flatten_stage") not in {None, "normal"}:
        countdown = format_duration(state.get("flatten_seconds_remaining"))
        st.warning(f"مرحله خروج پایانی فعال است؛ زمان باقی‌مانده: {countdown}")


@st.cache_data(ttl=300, show_spinner=False)
def load_market_catalog(config) -> list[dict[str, object]]:
    markets = asyncio.run(fetch_active_tmn_markets(config))
    return [market.model_dump(mode="python") for market in markets]


@st.cache_data(ttl=2, show_spinner=False)
def load_public_market_view(
    config, market_payload: dict[str, object], timeframe_minutes: int, bars: int
) -> MarketExplorerView:
    market = MarketSpec.model_validate(market_payload)
    return asyncio.run(
        fetch_market_view(
            config,
            market.symbol,
            timeframe_minutes=timeframe_minutes,
            bars=bars,
            market=market,
        )
    )


@st.cache_data(ttl=5, show_spinner=False)
def load_watchlist_quotes(config, symbols: tuple[str, ...]) -> list[WatchQuote]:
    return asyncio.run(fetch_watchlist_quotes(config, list(symbols)))


def _chart_time(value: object, timeframe_minutes: int, *, full: bool = False) -> str:
    rendered = format_datetime(value)
    if full or timeframe_minutes >= 60:
        return rendered
    return rendered.rsplit("، ", 1)[-1]


MARKER_STYLES = {
    "entry": ("ورود", "triangle-up", CHART_SUCCESS),
    "exit": ("خروج", "triangle-down", CHART_DESTRUCTIVE),
    "vetoed": ("رد توسط AI", "x", CHART_MUTED),
}


def _wrap(text: object, width: int = 48) -> str:
    words = html.escape(str(text or "—")).split()
    lines, current = [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return "<br>".join(lines)


def chart_markers(config, symbol: str) -> pd.DataFrame:
    """Shadow/live decisions for the chart, each with a Persian hover explanation."""
    path = Path(config.app.logs_dir) / "decisions.jsonl"
    if not path.exists():
        return pd.DataFrame()
    decisions = load_decisions(str(path), path.stat().st_mtime)
    markers = trade_markers(decisions, symbol)
    if markers.empty:
        return markers
    texts = []
    for marker in markers.itertuples():
        row = decisions.loc[marker.row]
        label = MARKER_STYLES[marker.kind][0]
        parts = [
            f"<b>{label} · {MODE_LABELS.get(row['mode'], row['mode'])}</b>",
            f"قیمت: {format_money(marker.price)}",
        ]
        if marker.kind == "entry" and pd.notna(marker.stop_price):
            parts.append(
                f"حد ضرر: {format_money(marker.stop_price)} · هدف: {format_money(marker.target_price)}"
            )
        parts.append(f"قواعد: {_wrap(row['signal_reason'])}")
        parts.append(f"رژیم AI: {_wrap(row['ai_regime_reason'])}")
        parts.append(f"بازبینی AI: {_wrap(row['ai_review_reason'])}")
        texts.append("<br>".join(parts))
    return markers.assign(text=texts)


def add_trade_markers(figure: go.Figure, markers: pd.DataFrame, x_values) -> None:
    if markers is None or markers.empty or not len(x_values):
        return
    start, end = x_values[0], x_values[-1]
    visible = markers[(markers["created_at"] >= start) & (markers["created_at"] <= end)]
    for kind, (label, symbol, color) in MARKER_STYLES.items():
        subset = visible[visible["kind"] == kind]
        if subset.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=subset["created_at"],
                y=subset["price"],
                name=label,
                mode="markers",
                marker={
                    "symbol": symbol,
                    "size": 15,
                    "color": color,
                    "line": {"color": CHART_SURFACE, "width": 1.5},
                },
                text=subset["text"],
                hovertemplate="%{text}<extra></extra>",
            ),
            row=1,
            col=1,
        )


def market_chart(
    view: MarketExplorerView,
    chart_type: str,
    indicators: list[str],
    markers: pd.DataFrame | None = None,
) -> go.Figure:
    frame = chart_indicator_frame(list(view.chart_candles))
    show_volume = "حجم" in indicators
    show_rsi = "RSI" in indicators
    rows = 1 + int(show_volume) + int(show_rsi)
    row_heights = [0.68] + ([0.18] if show_volume else []) + ([0.18] if show_rsi else [])
    total = sum(row_heights)
    row_heights = [height / total for height in row_heights]
    figure = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=row_heights,
    )
    x_values = frame.index
    full_labels = [_chart_time(value, view.chart_timeframe_minutes, full=True) for value in x_values]
    hover = [
        "<br>".join(
            (
                f"زمان: {label}",
                f"باز: {format_money(row.open)}",
                f"بیشینه: {format_money(row.high)}",
                f"کمینه: {format_money(row.low)}",
                f"بسته: {format_money(row.close)}",
            )
        )
        for label, row in zip(full_labels, frame.itertuples(), strict=True)
    ]
    if chart_type == "خطی":
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=frame["close"],
                name="قیمت پایانی",
                mode="lines",
                line={"color": CHART_PRIMARY, "width": 2},
                fill="tozeroy",
                fillcolor="rgba(0,120,99,0.05)",
                text=hover,
                hoverinfo="text",
            ),
            row=1,
            col=1,
        )
    else:
        figure.add_trace(
            go.Candlestick(
                x=x_values,
                open=frame["open"],
                high=frame["high"],
                low=frame["low"],
                close=frame["close"],
                name="قیمت",
                increasing_line_color=CHART_SUCCESS,
                decreasing_line_color=CHART_DESTRUCTIVE,
                text=hover,
                hoverinfo="text",
            ),
            row=1,
            col=1,
        )
    overlays: list[tuple[str, str, str, str]] = []
    if "EMA ۹/۲۱" in indicators:
        overlays.extend(
            [
                ("ema_9", "EMA ۹", CHART_INFO, "solid"),
                ("ema_21", "EMA ۲۱", CHART_WARNING, "solid"),
            ]
        )
    if "بولینگر" in indicators:
        overlays.extend(
            [
                ("bollinger_upper", "بولینگر بالا", CHART_MUTED, "dot"),
                ("bollinger_mid", "میانگین بولینگر", CHART_ACCENT, "dot"),
                ("bollinger_lower", "بولینگر پایین", CHART_MUTED, "dot"),
            ]
        )
    if "VWAP" in indicators:
        overlays.append(("vwap", "VWAP", CHART_ACCENT, "dash"))
    for column, name, color, dash in overlays:
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=frame[column],
                name=name,
                mode="lines",
                line={"color": color, "width": 1.4, "dash": dash},
                customdata=frame[column].map(format_money),
                hovertemplate=f"{name}: %{{customdata}}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    add_trade_markers(figure, markers, x_values)
    next_row = 2
    if show_volume:
        volume_colors = [
            CHART_SUCCESS if close >= open_ else CHART_DESTRUCTIVE
            for open_, close in zip(frame["open"], frame["close"], strict=True)
        ]
        figure.add_trace(
            go.Bar(
                x=x_values,
                y=frame["volume"],
                name="حجم",
                marker_color=volume_colors,
                customdata=frame["volume"].map(lambda value: format_number(value, 4)),
                hovertemplate="حجم: %{customdata}<extra></extra>",
            ),
            row=next_row,
            col=1,
        )
        next_row += 1
    if show_rsi:
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=frame["rsi_14"],
                name="RSI ۱۴",
                mode="lines",
                line={"color": CHART_ACCENT, "width": 1.6},
                customdata=frame["rsi_14"].map(lambda value: format_number(value, 1)),
                hovertemplate="RSI: %{customdata}<extra></extra>",
            ),
            row=next_row,
            col=1,
        )
        figure.add_hline(y=70, line_dash="dot", line_color=CHART_GRID, row=next_row, col=1)
        figure.add_hline(y=30, line_dash="dot", line_color=CHART_GRID, row=next_row, col=1)
        figure.update_yaxes(
            range=[0, 100], tickvals=[30, 50, 70], ticktext=["۳۰", "۵۰", "۷۰"], row=next_row, col=1
        )

    if len(frame) > 1:
        low = float(frame["low"].min())
        high = float(frame["high"].max())
        step = (high - low) / 4 if high != low else 1
        price_ticks = [low + step * index for index in range(5)]
        figure.update_yaxes(
            tickvals=price_ticks,
            ticktext=[format_number(value) for value in price_ticks],
            row=1,
            col=1,
        )
    if len(frame):
        tick_indexes = sorted(set(int(index) for index in np.linspace(0, len(frame) - 1, 5)))
        tick_values = [x_values[index] for index in tick_indexes]
        tick_text = [
            _chart_time(value, view.chart_timeframe_minutes) for value in tick_values
        ]
        figure.update_xaxes(tickvals=tick_values, ticktext=tick_text)
    figure.update_layout(
        height=610,
        margin={"l": 10, "r": 10, "t": 42, "b": 24},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=CHART_SURFACE,
        font={"family": "Vazirmatn, Tahoma, sans-serif", "color": CHART_FOREGROUND},
        hoverlabel={"align": "right", "font": {"family": "Vazirmatn, Tahoma, sans-serif"}},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.06, "x": 1, "xanchor": "right"},
        xaxis_rangeslider_visible=False,
        bargap=0.16,
        dragmode="pan",
    )
    figure.update_xaxes(
        showgrid=False,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor=CHART_MUTED,
        spikethickness=1,
    )
    figure.update_yaxes(gridcolor=CHART_GRID, fixedrange=False, zeroline=False)
    return figure


def render_watchlist(quotes: list[WatchQuote], selected_symbol: str) -> None:
    if not quotes:
        st.warning("دیده‌بان موقتاً داده‌ای دریافت نکرد.")
        return
    rows = []
    for quote in quotes:
        active = " active" if quote.symbol == selected_symbol else ""
        rows.append(
            f'<div class="watch-row{active}">'
            f'<span class="watch-symbol">{html.escape(quote.symbol)}</span>'
            f'<span class="watch-price">{format_money(quote.midpoint)}</span>'
            f'<span class="watch-spread">{format_number(quote.spread_bps, 1)} bp</span>'
            "</div>"
        )
    st.markdown('<div class="watchlist">' + "".join(rows) + "</div>", unsafe_allow_html=True)


def render_order_book(view: MarketExplorerView) -> None:
    asks = list(reversed(view.book.asks[:8]))
    bids = list(view.book.bids[:8])
    max_quantity = max(
        [float(level.quantity) for level in asks + bids] or [1.0]
    )

    def level_row(level, side: str) -> str:
        width = max(3, float(level.quantity) / max_quantity * 100)
        side_label = "ask" if side == "ask" else "bid"
        return (
            '<div class="depth-row">'
            f'<span class="depth-fill {side_label}" style="width:{width:.2f}%"></span>'
            f'<span class="depth-price {side_label}">{format_number(level.price)}</span>'
            f'<span>{format_number(level.quantity, 6)}</span>'
            f'<span>{format_money(level.price * level.quantity)}</span>'
            "</div>"
        )

    midpoint = (view.book.best_bid + view.book.best_ask) / 2
    content = [
        '<div class="depth-book">',
        '<div class="depth-head"><span>قیمت</span><span>مقدار</span><span>ارزش</span></div>',
        *(level_row(level, "ask") for level in asks),
        '<div class="depth-mid">'
        f'<span>میانه {format_money(midpoint)}</span>'
        f'<span>{format_number(view.feature.spread_bps, 2)} bp</span>'
        "</div>",
        *(level_row(level, "bid") for level in bids),
        "</div>",
    ]
    st.markdown("".join(content), unsafe_allow_html=True)


def render_ai_review(config, view: MarketExplorerView) -> None:
    st.subheader("بازبینی هوش مصنوعی")
    has_key = bool(os.getenv("BOX_API_KEY"))
    if not has_key:
        st.caption(
            "برای فعال‌سازی تحلیل، کلید جدید BoxAPI را فقط در متغیر محیطی "
            "BOX_API_KEY قرار دهید."
        )
    if st.button(
        "تحلیل با BoxAPI",
        type="primary",
        disabled=not has_key,
        help="این تحلیل فقط داده‌های عمومی بازار را می‌بیند و امکان ثبت سفارش ندارد.",
    ):
        try:
            box_config = config.model_copy(
                update={
                    "ai": config.ai.model_copy(
                        update={
                            "enabled": True,
                            "provider": "boxapi",
                            "base_url": os.getenv("BOX_API_BASE_URL")
                            or "https://ai.boxapi.ir/ai/v1",
                        }
                    )
                }
            )
            service = build_ai_service(box_config, RuntimeSecrets.from_environment())
            with st.spinner("در حال بازبینی فشردهٔ بازار…"):
                regime = asyncio.run(service.regime(view.feature))
                review = (
                    asyncio.run(service.review(view.feature, view.signal))
                    if view.signal.action == SignalAction.ENTER_LONG
                    else None
                )
            st.session_state[f"ai_review_{view.market.symbol}"] = (regime, review)
        except Exception:
            st.error(
                "بازبینی هوش مصنوعی انجام نشد. تنظیم کلید، نشانی سرویس و "
                "دسترسی شبکه را بررسی کنید."
            )
    result = st.session_state.get(f"ai_review_{view.market.symbol}")
    if result:
        regime, review = result
        columns = st.columns(3)
        columns[0].metric("رژیم", isolate_ltr(regime.regime))
        columns[1].metric("اعتماد", format_percent(regime.confidence))
        columns[2].metric("ضریب اندازه", format_number(regime.size_multiplier, 2))
        st.write(regime.reason_fa)
        if review is not None:
            label = "تأیید" if review.approve else "رد"
            st.write(f"بازبینی سیگنال: {label} — {review.reason_fa}")
        else:
            st.caption("چون سیگنال ورود صادر نشده، بازبینی ورود فراخوانی نشد.")


def render_signal_panel(view: MarketExplorerView) -> None:
    signal_label = {
        SignalAction.ENTER_LONG: "فرصت ورود",
        SignalAction.EXIT: "خروج",
        SignalAction.HOLD: "نگهداری",
        SignalAction.SKIP: "بدون ورود",
    }[view.signal.action]
    st.markdown(
        f"""
        <div class="signal-card">
          <div class="signal-label">نتیجهٔ آخرین کندل تکمیل‌شده</div>
          <div class="signal-value">{html.escape(signal_label)}</div>
          <p class="signal-reason">{html.escape(view.signal.reason_fa)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    feature = view.feature
    metrics = st.columns(2)
    metrics[0].metric("RSI ۱۴", format_number(feature.rsi_14, 1))
    metrics[1].metric("عدم‌تعادل", format_percent(feature.orderbook_imbalance_5))
    expected_move = f"{format_number(view.signal.expected_move_bps, 2)} bp"
    st.metric("حرکت مورد انتظار", expected_move)


@st.fragment(run_every="3s")
def render_live_terminal(
    config,
    market_payload: dict[str, object],
    timeframe_minutes: int,
    bars: int,
    chart_type: str,
    indicators: tuple[str, ...],
    watch_symbols: tuple[str, ...],
) -> None:
    symbol = str(market_payload["symbol"])
    try:
        view = load_public_market_view(
            config, market_payload, timeframe_minutes, bars
        )
    except Exception:
        st.error(
            "دادهٔ زندهٔ این بازار کامل دریافت نشد. صفحه به‌طور خودکار دوباره تلاش می‌کند."
        )
        return
    midpoint = (view.book.best_bid + view.book.best_ask) / 2
    reference = view.feature.close
    change = (midpoint - reference) / reference if reference else Decimal("0")
    change_label = format_percent(change)
    change_color = CHART_SUCCESS if change >= 0 else CHART_DESTRUCTIVE
    age_seconds = max(0, int((datetime.now(tz=UTC) - view.book.captured_at).total_seconds()))
    st.markdown(
        f"""
        <div class="quote-strip" aria-live="polite">
          <span class="quote-symbol">{html.escape(symbol)}</span>
          <span class="quote-price">{format_money(midpoint)}</span>
          <span style="color:{change_color};font-weight:700">{change_label}</span>
          <span class="quote-meta">خرید {format_money(view.book.best_bid)}</span>
          <span class="quote-meta">فروش {format_money(view.book.best_ask)}</span>
          <span class="quote-meta">اسپرد {format_number(view.feature.spread_bps, 2)} bp</span>
          <span class="quote-meta">تازگی {format_number(age_seconds)} ثانیه</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    chart_column, watch_column, side_column = st.columns([3.8, 1.1, 1.35])
    with chart_column:
        st.markdown(
            '<div class="panel-title"><span>نمودار بازار</span>'
            '<span class="panel-kicker">بزرگ‌نمایی، حرکت و نشانگر متقاطع فعال است</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="chart-shell">', unsafe_allow_html=True)
        st.plotly_chart(
            market_chart(
                view, chart_type, list(indicators), chart_markers(config, view.market.symbol)
            ),
            width="stretch",
            config={
                "displayModeBar": True,
                "displaylogo": False,
                "responsive": True,
                "scrollZoom": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                "toImageButtonOptions": {"format": "png", "filename": f"wallex-{symbol}"},
            },
        )
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown(
            f'<p class="workspace-note">بازهٔ {format_number(timeframe_minutes)} دقیقه · '
            f'{format_number(len(view.chart_candles))} کندل واقعی از Wallex REST</p>',
            unsafe_allow_html=True,
        )
        with st.expander("جدول دسترس‌پذیر داده‌های نمودار"):
            rows = pd.DataFrame(
                [
                    {
                        "زمان": format_datetime(candle.opened_at),
                        "باز": format_money(candle.open),
                        "بیشینه": format_money(candle.high),
                        "کمینه": format_money(candle.low),
                        "بسته": format_money(candle.close),
                        "حجم": format_number(candle.volume, 4),
                        "وضعیت": "تکمیل‌شده" if candle.completed else "در حال تشکیل",
                    }
                    for candle in view.chart_candles
                ]
            )
            st.dataframe(rows, hide_index=True, width="stretch", height=360)
    with watch_column:
        st.markdown(
            '<div class="panel-title"><span>دیده‌بان</span><span class="panel-kicker">زنده</span></div>',
            unsafe_allow_html=True,
        )
        try:
            quotes = load_watchlist_quotes(config, watch_symbols)
            render_watchlist(quotes, symbol)
        except Exception:
            st.warning("بخشی از دیده‌بان اکنون در دسترس نیست.")
        st.markdown(
            '<p class="workspace-note">قیمت میانی از بهترین سفارش خرید و فروش محاسبه شده است.</p>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="panel-title"><span>تصمیم قواعد</span></div>', unsafe_allow_html=True)
        render_signal_panel(view)
    with side_column:
        st.markdown(
            '<div class="panel-title"><span>دفتر سفارش</span>'
            '<span class="panel-kicker">۸ سطح</span></div>',
            unsafe_allow_html=True,
        )
        render_order_book(view)
        st.markdown('<div class="terminal-section"></div>', unsafe_allow_html=True)
        render_ai_review(config, view)


def render_market_explorer(config) -> None:
    st.markdown(
        """
        <div class="terminal-head">
          <div class="terminal-brand">
            <span class="terminal-mark">W</span>
            <div>
              <h1 class="terminal-title">ترمینال زندهٔ بازار Wallex</h1>
              <p class="terminal-subtitle">نمودار، دیده‌بان و دفتر سفارش بازارهای فعال تومانی</p>
            </div>
          </div>
          <span class="live-pill">زنده · Wallex REST · هر ۳ ثانیه</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        <div class="readonly-banner" role="status">
          <span class="readonly-dot" aria-hidden="true"></span>
          <span><b>فقط‌خواندنی:</b> داده‌ها واقعی‌اند، اما این صفحه هیچ سفارشی به والکس ارسال نمی‌کند.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    try:
        with st.spinner("در حال دریافت بازارهای فعال والکس…"):
            raw_markets = load_market_catalog(config)
    except Exception:
        st.error("فهرست بازارهای والکس دریافت نشد. اتصال شبکه یا سرویس والکس را بررسی کنید.")
        return
    symbols = [str(item["symbol"]) for item in raw_markets]
    if not symbols:
        st.info("در حال حاضر بازار فعال تومانی قابل نمایش نیست.")
        return
    preferred = "USDTTMN" if "USDTTMN" in symbols else "BTCTMN"
    if preferred not in symbols:
        preferred = symbols[0]
    symbol_column, timeframe_column, type_column, bars_column, refresh_column = st.columns(
        [2.2, 1, 1, 1.1, 0.8]
    )
    with symbol_column:
        symbol = st.selectbox(
            "نماد بازار",
            symbols,
            index=symbols.index(preferred),
            format_func=lambda item: isolate_ltr(item),
        )
    with timeframe_column:
        timeframe_minutes = st.selectbox(
            "بازهٔ زمانی",
            [1, 5, 15, 60, 240],
            index=1,
            format_func=lambda value: {
                1: "۱ دقیقه",
                5: "۵ دقیقه",
                15: "۱۵ دقیقه",
                60: "۱ ساعت",
                240: "۴ ساعت",
            }[value],
        )
    with type_column:
        chart_type = st.selectbox("نوع نمودار", ["کندل", "خطی"])
    with bars_column:
        bars = st.selectbox(
            "تعداد کندل",
            [96, 180, 240, 360, 500],
            index=2,
            format_func=lambda value: format_number(value),
        )
    with refresh_column:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("تازه‌سازی", width="stretch", help="پاک‌کردن حافظهٔ کوتاه‌مدت و دریافت دوباره"):
            load_public_market_view.clear()
            load_watchlist_quotes.clear()
            st.rerun()

    indicator_column, watch_column = st.columns([2.4, 2])
    with indicator_column:
        indicators = st.multiselect(
            "شاخص‌های نمودار",
            ["EMA ۹/۲۱", "بولینگر", "VWAP", "حجم", "RSI"],
            default=["EMA ۹/۲۱", "حجم", "RSI"],
        )
    default_watch = [candidate for candidate in ("USDTTMN", "BTCTMN", "ETHTMN", "SOLTMN") if candidate in symbols]
    if symbol not in default_watch:
        default_watch.insert(0, symbol)
    with watch_column:
        watch_symbols = st.multiselect(
            "نمادهای دیده‌بان",
            symbols,
            default=default_watch[:6],
            max_selections=8,
            format_func=lambda item: isolate_ltr(item),
        )
    effective_watch = tuple(dict.fromkeys([symbol, *watch_symbols]))
    market_payload = next(item for item in raw_markets if item["symbol"] == symbol)
    render_live_terminal(
        config,
        market_payload,
        timeframe_minutes,
        bars,
        chart_type,
        tuple(indicators),
        effective_watch,
    )


MODE_LABELS = {"backtest": "بک‌تست", "shadow": "اجرای سایه", "live": "معامله واقعی"}


def render_engine_control(config) -> None:
    st.title("کنترل موتور")
    if os.getenv("DASHBOARD_CONTROL_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        st.info(
            "کنترل موتور غیرفعال است. برای فعال‌سازی، متغیر "
            "<bdi>DASHBOARD_CONTROL_ENABLED=true</bdi> را تنظیم کنید."
        )
        return
    controller = EngineController(config.app.logs_dir, cwd=ROOT)
    env = {**os.environ, **read_env_file(ROOT / ".env")}
    running = controller.status()
    if running is not None:
        st.success(
            f"موتور در حالت «{MODE_LABELS[running.mode]}» در حال اجراست "
            f"(از {format_datetime(running.started_at)})."
        )
        if running.mode == "live":
            st.warning("توقف موتور موقعیت‌های باز را نمی‌بندد؛ بستن نهایی در پایان چالش انجام می‌شود.")
        if st.button("توقف موتور", type="primary"):
            controller.stop()
            st.rerun()
        st.caption("آخرین خطوط گزارش اجرا")
        st.code(tail(running.log_path) or "…", language="text")
        return

    mode = st.radio(
        "حالت اجرا",
        list(MODE_LABELS),
        format_func=MODE_LABELS.get,
        horizontal=True,
    )
    reasons = preflight(config, mode, env)
    if reasons:
        st.error("این حالت فعلاً قابل اجرا نیست:")
        st.markdown("\n".join(f"- <bdi>{html.escape(reason)}</bdi>" for reason in reasons), unsafe_allow_html=True)
    confirmed = True
    if mode == "live":
        st.warning(
            "در این حالت الگوریتم با کلید API والکس و سرمایه واقعی، بدون دخالت دستی معامله می‌کند."
        )
        confirmed = st.checkbox("تأیید می‌کنم که اجرای سایه بررسی شده و آماده معامله واقعی هستم.")
    if st.button("شروع موتور", type="primary", disabled=bool(reasons) or not confirmed):
        try:
            controller.start(mode, env)
        except RuntimeError:
            st.error("یک موتور دیگر در حال اجراست.")
        st.rerun()
    last_logs = sorted(Path(config.app.logs_dir).glob("engine-*.log"))
    if last_logs:
        st.caption(f"گزارش آخرین اجرا: <bdi>{last_logs[-1].name}</bdi>", unsafe_allow_html=True)
        st.code(tail(last_logs[-1]) or "…", language="text")


OUTCOME_LABELS = {"filled": "اجرا شد", "skipped": "ورود نکرد", "rejected": "رد شد"}
SWEEP_LABELS = {"sweep_15m": "۱۵ دقیقه", "sweep_5m": "۵ دقیقه", "param_sweep": "دور اول"}


@st.cache_data(ttl=30, show_spinner=False)
def load_decisions(path: str, modified: float) -> pd.DataFrame:
    return decision_frame(Path(path))


def _percent_points(value: object) -> str:
    return f"{format_number(value, 3)}٪"


def render_ai_comparison(config) -> None:
    st.subheader("اثر هوش مصنوعی: روشن در برابر خاموش")
    reports = []
    for path in sorted(Path(config.app.reports_dir).glob("bt-*.md"), reverse=True):
        if path.stem.endswith("-ai-off"):
            continue
        metrics, comparison = parse_report_tables(path)
        if comparison:
            reports.append((path, metrics, comparison))
    if not reports:
        st.info("هنوز بک‌تست با AI روشن کامل نشده است. پس از اجرای بک‌تست، این بخش پر می‌شود.")
        return
    labels = {str(path): path.stem.split("-")[1] for path, _, _ in reports}
    chosen = st.selectbox("بازار", list(labels), format_func=labels.get, key="ai_report")
    _, metrics, comparison = next(r for r in reports if str(r[0]) == chosen)
    first = st.columns(2)
    first[0].metric("بازده AI روشن", format_percent(comparison.get("بازده AI روشن")))
    first[1].metric("بازده AI خاموش", format_percent(comparison.get("بازده AI خاموش")))
    second = st.columns(2)
    second[0].metric("افت سرمایه AI روشن", format_percent(comparison.get("افت سرمایه AI روشن")))
    second[1].metric("افت سرمایه AI خاموش", format_percent(comparison.get("افت سرمایه AI خاموش")))
    third = st.columns(2)
    third[0].metric("اختلاف سود تحقق‌یافته", format_money(comparison.get("اختلاف سود تحقق‌یافته")))
    third[1].metric("اختلاف تعداد معاملات", format_number(comparison.get("اختلاف تعداد معاملات")))
    st.caption(
        f"معاملات با AI روشن: {format_number(metrics.get('تعداد معاملات'))} · "
        f"مسیرهای جایگزین AI: {format_number(metrics.get('تعداد مسیرهای جایگزین AI'))}"
    )


def render_walk_forward(config) -> None:
    st.subheader("آزمون رو به جلو")
    st.caption("پارامتر فقط روی بازه انتخاب رتبه‌بندی شد؛ بازه آزمون دست‌نخورده گزارش می‌شود.")
    sweeps = load_sweeps(config.app.reports_dir)
    if not sweeps:
        st.info("نتیجه‌ای از اسکریپت param_sweep پیدا نشد.")
        return
    name = st.radio(
        "مجموعه آزمون",
        sorted(sweeps, key=lambda key: list(SWEEP_LABELS).index(key) if key in SWEEP_LABELS else 99),
        format_func=lambda key: SWEEP_LABELS.get(key, key),
        horizontal=True,
        key="sweep_set",
    )
    strategy = config.strategy
    rows = []
    for row in sweeps[name]:
        current = (
            int(row.get("signal_timeframe_minutes", 0)) == strategy.signal_timeframe_minutes
            and "safety_factor" in row
            and Decimal(str(row["safety_factor"])) == strategy.safety_factor
            and Decimal(str(row.get("trend_target_r"))) == strategy.trend_target_r
        )
        trades_in = sum(v for k, v in row.items() if k.endswith("_in_trades"))
        trades_out = sum(v for k, v in row.items() if k.endswith("_out_trades"))
        rows.append(
            {
                "تنظیم فعلی": "✓" if current else "",
                "تایم‌فریم": f"{format_number(row.get('signal_timeframe_minutes'))} دقیقه",
                "ضریب اطمینان": format_number(row.get("safety_factor"), 2),
                "هدف R": format_number(row.get("trend_target_r"), 1),
                "بازده انتخاب": _percent_points(row.get("in_ret_sum")),
                "بازده آزمون": _percent_points(row.get("out_ret_sum")),
                "معاملات انتخاب": format_number(trades_in),
                "معاملات آزمون": format_number(trades_out),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render_decision_explorer(config) -> None:
    st.subheader("کاوشگر تصمیم‌ها")
    path = Path(config.app.logs_dir) / "decisions.jsonl"
    if not path.exists():
        st.info("لاگ تصمیم هنوز ساخته نشده است.")
        return
    frame = load_decisions(str(path), path.stat().st_mtime)
    if frame.empty:
        st.info("لاگ تصمیم خالی است.")
        return
    modes = [mode for mode in ("live", "shadow", "backtest") if mode in set(frame["mode"])]
    mode = st.radio(
        "حالت", modes, format_func=MODE_LABELS.get, horizontal=True, key="decision_mode"
    )
    view = frame[frame["mode"] == mode]
    symbols = sorted(view["symbol"].dropna().unique())
    symbol = st.selectbox("بازار", ["همه", *symbols], key="decision_symbol")
    if symbol != "همه":
        view = view[view["symbol"] == symbol]
    only_signals = st.toggle("فقط سیگنال‌های ورود", value=mode == "backtest", key="decision_signals")
    summary = decision_summary(view)
    cards = st.columns(2)
    cards[0].metric("تصمیم‌ها", format_number(summary["decisions"]))
    cards[1].metric("سیگنال قواعد", format_number(summary["signals"]))
    cards = st.columns(2)
    cards[0].metric("رد توسط AI", format_number(summary["ai_vetoes"]))
    cards[1].metric("اجرا شده", format_number(summary["filled"]))
    if only_signals:
        view = view[view["action"].fillna("SKIP") != "SKIP"]
    view = view.sort_values("created_at", ascending=False).head(200)
    if view.empty:
        st.info("با این فیلتر تصمیمی وجود ندارد.")
        return
    table = pd.DataFrame(
        {
            "زمان": view["created_at"].map(format_datetime),
            "بازار": view["symbol"],
            "نتیجه": view["outcome"].map(lambda value: OUTCOME_LABELS.get(value, value)),
            "رژیم AI": view["ai_regime"].fillna("—"),
            "دلیل": view["reason_fa"],
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")
    picked = st.selectbox(
        "جزئیات تصمیم",
        list(view.index),
        format_func=lambda index: f"{format_datetime(view.at[index, 'created_at'])} · {view.at[index, 'symbol']}",
        key="decision_detail",
    )
    row = view.loc[picked]
    if (row["action"] or "SKIP") == "SKIP":
        review = "بدون سیگنال"
    else:
        review = "تأیید" if row["ai_approve"] is not False else "رد"
    st.markdown(
        f"""
- **قواعد:** {html.escape(str(row['signal_reason'] or '—'))}
- **رژیم AI** (<bdi>{html.escape(str(row['ai_regime'] or '—'))}</bdi>، اطمینان {format_number(row['ai_confidence'], 2)}): {html.escape(str(row['ai_regime_reason'] or '—'))}
- **بازبینی AI:** {review} — {html.escape(str(row['ai_review_reason'] or '—'))}
- **ریسک:** {html.escape(str(row['risk_reason'] or '—'))}
- **نتیجه:** {html.escape(str(row['reason_fa'] or '—'))}
""",
        unsafe_allow_html=True,
    )


def render_results(config) -> None:
    st.title("نتایج و شواهد")
    st.caption("همه اعداد از گزارش‌ها و لاگ تصمیم همین مخزن خوانده می‌شوند.")
    render_ai_comparison(config)
    st.divider()
    render_walk_forward(config)
    st.divider()
    render_decision_explorer(config)


st.set_page_config(page_title="پایش Wallex", page_icon=":material/monitoring:", layout="wide")
inject_theme()
require_password()
CONFIG = load_config(os.getenv("CONFIG_PATH", "config.yaml"))

market_tab, operations_tab, results_tab, control_tab = st.tabs(
    ["بازار زنده", "عملیات موتور", "نتایج", "کنترل موتور"]
)
with market_tab:
    render_market_explorer(CONFIG)
with operations_tab:
    render_dashboard(CONFIG)
with results_tab:
    render_results(CONFIG)
with control_tab:
    render_engine_control(CONFIG)
