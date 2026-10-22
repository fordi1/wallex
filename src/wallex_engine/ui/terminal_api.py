"""Local TradingView-style terminal: one page plus a small JSON API.

It reuses the engine's own pieces (Wallex client, decision log, EngineController,
preflight) so the page can never start a mode the command line would refuse.
There is no password: it serves 127.0.0.1 only and refuses any request whose
Host or Origin is not this terminal, which blocks other websites (CSRF) and
DNS rebinding from driving the engine.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from ..config import load_config
from ..exchange.wallex_client import WallexClient
from ..models import FeatureSnapshot
from ..reporting.results import decision_frame, find_decision, trade_markers
from ..reporting.scorecard import risk_state, run_scorecard
from ..runtime.control import (
    MODES,
    EngineController,
    mode_database_url,
    preflight,
    read_env_file,
    tail,
)
from ..strategy.hybrid import HybridStrategy

TIMEFRAMES = (1, 5, 15, 60, 240, 1440)
PAGE = Path(__file__).resolve().parents[3] / "web" / "terminal.html"
MODEL_PATTERN = r"^[A-Za-z0-9._/:-]{2,80}$"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class StartRequest(BaseModel):
    mode: str
    confirm_live: bool = False


class SettingsRequest(BaseModel):
    model: str = Field(pattern=MODEL_PATTERN)


def create_terminal_app(
    config_path: Path,
    root: Path,
    controller: Any | None = None,
    hosts: set[str] | frozenset[str] = LOCAL_HOSTS,
) -> FastAPI:
    config_path = Path(config_path)
    root = Path(root)
    app = FastAPI(title="Wallex terminal", docs_url=None, redoc_url=None)
    settings_file = root / "data" / "ui-settings.json"
    decisions_cache: dict[str, Any] = {}

    def config():
        return load_config(config_path)

    def env() -> dict[str, str]:
        values = {**os.environ, **read_env_file(root / ".env")}
        # The engine's health server must not inherit this terminal's own port.
        return {**values, "PORT": values.get("ENGINE_PORT", "8080")}

    def engine_controller():
        return controller or EngineController(root / config().app.logs_dir, cwd=root)

    def authorize(request: Request) -> None:
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        origin = request.headers.get("origin")
        origin_host = (origin or "").split("://", 1)[-1].rsplit(":", 1)[0]
        if host not in hosts or (origin is not None and origin_host not in hosts):
            raise HTTPException(403, "فقط از صفحه ترمینال روی همین رایانه مجاز است.")

    def wallex() -> WallexClient:
        cfg = config()
        return WallexClient(
            cfg.exchange.rest_url, cfg.exchange.websocket_url, None,
            cfg.exchange.request_timeout_seconds, cfg.exchange.max_retries,
        )

    def decisions() -> pd.DataFrame:
        path = root / config().app.logs_dir / "decisions.jsonl"
        if not path.exists():
            return pd.DataFrame()
        stamp = path.stat().st_mtime
        if decisions_cache.get("stamp") != stamp:
            decisions_cache.update(stamp=stamp, frame=decision_frame(path))
        return decisions_cache["frame"]

    def saved_models() -> list[str]:
        try:
            return list(json.loads(settings_file.read_text(encoding="utf-8")).get("models", []))
        except (OSError, ValueError):
            return []

    guard = [Depends(authorize)]

    def mode_engine(mode: str):
        """Read-only connection to the database the engine uses for ``mode``."""
        url = mode_database_url(mode, env()).replace("+aiosqlite", "")
        url = url.replace("+asyncpg", "+psycopg")
        if url.startswith("sqlite:///./"):
            path = root / url.removeprefix("sqlite:///./")
            if not path.exists():
                return None
            url = f"sqlite:///{path}"
        return create_engine(url)

    async def last_prices() -> dict[str, float]:
        try:
            async with wallex() as client:
                tickers = await client.get_tickers(config().challenge.allowed_markets)
        except httpx.HTTPError:
            return {}
        return {symbol: float(t["last"]) for symbol, t in tickers.items()}

    @app.api_route("/", methods=["GET", "HEAD"], dependencies=guard)
    def page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html; charset=utf-8")

    @app.get("/api/markets", dependencies=guard)
    async def markets() -> dict[str, Any]:
        symbols = config().challenge.allowed_markets
        async with wallex() as client:
            tickers = await client.get_tickers(symbols)
        return {
            "symbols": symbols,
            "tickers": {s: {k: float(v) for k, v in t.items()} for s, t in tickers.items()},
        }

    @app.get("/api/candles", dependencies=guard)
    async def candles(
        symbol: str, tf: int = Query(15), bars: int = Query(300, ge=50, le=1000)
    ) -> dict[str, Any]:
        if tf not in TIMEFRAMES:
            raise HTTPException(422, "unsupported timeframe")
        end = datetime.now(tz=UTC)
        base = base_resolution(tf)
        async with wallex() as client:
            rows = await client.get_candles(symbol, base, end - timedelta(minutes=tf * bars), end)
        frame = aggregate(
            pd.DataFrame(
                {
                    "opened_at": [c.opened_at for c in rows],
                    "open": [float(c.open) for c in rows],
                    "high": [float(c.high) for c in rows],
                    "low": [float(c.low) for c in rows],
                    "close": [float(c.close) for c in rows],
                    "volume": [float(c.volume) for c in rows],
                }
            ),
            tf,
        )
        frame["time"] = frame.pop("opened_at").map(lambda at: int(at.timestamp()))
        if not frame.empty:
            frame["ema9"] = frame["close"].ewm(span=9, adjust=False).mean()
            frame["ema21"] = frame["close"].ewm(span=21, adjust=False).mean()
        return {"symbol": symbol, "tf": tf, "candles": frame.to_dict(orient="records")}

    @app.get("/api/book", dependencies=guard)
    async def book(symbol: str) -> dict[str, Any]:
        async with wallex() as client:
            snapshot = await client.get_order_book(symbol, depth=10)
        levels = lambda side: [[float(x.price), float(x.quantity)] for x in side[:10]]  # noqa: E731
        return {"bids": levels(snapshot.bids), "asks": levels(snapshot.asks)}

    @app.get("/api/signals", dependencies=guard)
    def signals(symbol: str, limit: int = Query(40, le=200)) -> dict[str, Any]:
        frame = decisions()
        if frame.empty:
            return {"markers": [], "decisions": []}
        markers = trade_markers(frame, symbol)
        view = frame[(frame["symbol"] == symbol) & frame["mode"].isin(["shadow", "live"])]
        view = view.sort_values("created_at", ascending=False).head(limit)
        columns = [
            "created_at", "mode", "outcome", "action", "reason_fa", "signal_reason",
            "ai_regime", "ai_regime_reason", "ai_approve", "ai_review_reason", "entry_price",
        ]
        latest = view[columns].astype(object).where(view[columns].notna(), None)
        return {
            "markers": [
                {"time": int(m.created_at.timestamp()), "kind": m.kind, "price": m.price}
                for m in markers.itertuples()
            ],
            "decisions": [
                {**row, "created_at": row["created_at"].isoformat()}
                for row in latest.to_dict(orient="records")
            ],
        }

    @app.get("/api/explain", dependencies=guard)
    def explain(symbol: str, at: str | None = None, mode: str | None = None) -> dict[str, Any]:
        path = root / config().app.logs_dir / "decisions.jsonl"
        modes = {mode} if mode else {"live", "shadow"}
        record = find_decision(path, symbol, at, modes)
        if record is None and not mode and not at:
            record = find_decision(path, symbol, None, {"backtest"})
        if record is None or not record.get("features"):
            return {"decision": None}
        snapshot = FeatureSnapshot.model_validate(record["features"])
        result = HybridStrategy(config().strategy).explain(snapshot)
        families = ("trend", "mean_reversion")
        risk = record.get("risk") or {}
        return {
            "decision": {k: record.get(k) for k in ("created_at", "mode", "run_id", "outcome",
                                                     "reason_fa")},
            "features": {k: record["features"].get(k) for k in (
                "close", "rsi_14", "atr_14", "vwap", "orderbook_imbalance_5", "spread_bps")},
            "rules": {
                "action": result.action.value,
                "strategy": result.strategy.value,
                "failed": {f: result.failed(f) for f in families},
                "total": {f: sum(c.family == f for c in result.checks) for f in families},
                "checks": [c.__dict__ for c in result.checks],
                "logged_action": (record.get("signal") or {}).get("action"),
            },
            "ai": {"regime": record.get("regime"), "review": record.get("review")},
            "risk": {
                "approved": risk.get("approved"),
                "reason_fa": risk.get("reason_fa"),
                "constraints": list(risk.get("constraints") or []),
                "quantity": risk.get("quantity"),
                "notional_tmn": risk.get("notional_tmn"),
                "risk_tmn": risk.get("risk_tmn"),
            },
        }

    @app.get("/api/risk", dependencies=guard)
    async def risk(mode: str = Query("live")) -> dict[str, Any]:
        engine = mode_engine(mode)
        if engine is None:
            return {"run_id": None, "mode": mode}
        prices = await last_prices()
        try:
            with engine.connect() as connection:
                return risk_state(connection, config(), mode, prices)
        except Exception:  # noqa: BLE001 - an empty or foreign database has no run yet
            return {"run_id": None, "mode": mode}

    @app.get("/api/scorecard", dependencies=guard)
    def scorecard(mode: str = Query("live")) -> dict[str, Any]:
        engine = mode_engine(mode)
        if engine is None:
            return {"run_id": None, "mode": mode}
        cfg = config()
        try:
            with engine.connect() as connection:
                card = run_scorecard(
                    connection, mode, float(cfg.challenge.initial_capital_tmn),
                    root / cfg.app.logs_dir,
                )
                ai = connection.execute(text(
                    "SELECT COUNT(*), COALESCE(SUM(fallback), 0), COALESCE(SUM(cache_hit), 0) "
                    "FROM ai_calls"
                )).first()
        except Exception:  # noqa: BLE001 - an empty or foreign database has no run yet
            return {"run_id": None, "mode": mode}
        frame = decisions()
        if card.get("run_id") and not frame.empty:
            run = frame[frame["run_id"] == card["run_id"]]
            signals = run[run["action"].fillna("SKIP") != "SKIP"]
            card["signals"] = len(signals)
            card["ai_vetoes"] = int(
                ((signals["ai_approve"] == False) | (signals["ai_allow"] == False)).sum()  # noqa: E712
            )
        card["ai_calls"], card["ai_fallbacks"], card["ai_cache_hits"] = (int(v) for v in ai)
        return card

    @app.get("/api/engine", dependencies=guard)
    async def engine() -> dict[str, Any]:
        cfg, values = config(), env()
        running = engine_controller().status()
        health = None
        try:
            async with httpx.AsyncClient(timeout=1.5) as http:
                port = values.get("ENGINE_PORT", "8080")
                health = (await http.get(f"http://127.0.0.1:{port}/health")).json()
        except (httpx.HTTPError, ValueError):
            health = None
        return {
            "running": None if running is None else {
                "mode": running.mode, "pid": running.pid, "started_at": running.started_at,
                "log": tail(running.log_path, 12),
            },
            "gates": {mode: preflight(cfg, mode, values) for mode in MODES},
            "health": health,
            "equity": latest_equity(values),
            "ai": {"provider": cfg.ai.provider, "model": cfg.ai.model, "enabled": cfg.ai.enabled},
            "canary": cfg.execution.canary,
            "ends_at": cfg.challenge.ends_at.isoformat() if cfg.challenge.ends_at else None,
        }

    @app.post("/api/engine/start", dependencies=guard)
    def start(request: StartRequest) -> dict[str, Any]:
        if request.mode not in MODES:
            raise HTTPException(422, "unknown mode")
        if request.mode == "live" and not request.confirm_live:
            raise HTTPException(400, "معامله واقعی نیاز به تأیید صریح دارد.")
        reasons = preflight(config(), request.mode, env())
        if reasons:
            raise HTTPException(409, reasons)
        try:
            process = engine_controller().start(request.mode, env())
        except RuntimeError as error:
            raise HTTPException(409, [str(error)]) from error
        return {"mode": process.mode, "pid": process.pid}

    @app.post("/api/engine/stop", dependencies=guard)
    def stop() -> dict[str, bool]:
        engine_controller().stop()
        return {"stopped": True}

    @app.get("/api/settings", dependencies=guard)
    def get_settings() -> dict[str, Any]:
        cfg = config()
        models = list(dict.fromkeys([cfg.ai.model, *saved_models()]))
        return {
            "model": cfg.ai.model, "models": models,
            "provider": cfg.ai.provider, "enabled": cfg.ai.enabled,
        }

    @app.post("/api/settings", dependencies=guard)
    def save_settings(request: SettingsRequest) -> dict[str, Any]:
        previous = config().ai.model
        text_ = config_path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^  model: .*$", lambda _: f"  model: {request.model}", text_, count=1
        )
        if count != 1:
            raise HTTPException(500, "ai.model line not found in config")
        config_path.write_text(updated, encoding="utf-8")
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        models = list(dict.fromkeys([previous, *saved_models(), request.model]))
        settings_file.write_text(json.dumps({"models": models}), encoding="utf-8")
        return get_settings()

    return app


def base_resolution(tf: int) -> int:
    """Wallex only returns true bars for 1m (short windows), 60m and 1D; build the rest."""
    if tf >= 1440:
        return 1440
    return 60 if tf >= 60 else 1


def aggregate(frame: pd.DataFrame, tf: int) -> pd.DataFrame:
    """OHLCV bars of ``tf`` minutes from finer bars; the last bar may still be forming."""
    if frame.empty or tf == base_resolution(tf):
        return frame.reset_index(drop=True)
    grouped = frame.set_index("opened_at").resample(f"{tf}min", label="left", closed="left")
    bars = grouped.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])
    return bars.reset_index()


def latest_equity(values: dict[str, str]) -> float | None:
    url = values.get("DASHBOARD_DATABASE_URL", "sqlite:///./data/wallex.db")
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
    try:
        with create_engine(url).connect() as connection:
            row = connection.execute(
                text("SELECT equity_tmn FROM equity_snapshots ORDER BY captured_at DESC LIMIT 1")
            ).first()
    except Exception:  # noqa: BLE001 - missing database simply shows no equity
        return None
    return float(row[0]) if row else None
