from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from wallex_engine.ai.providers import (  # noqa: E402
    AIReviewService,
    DeterministicAIProvider,
)
from wallex_engine.app import build_ai_service, safe_config_payload  # noqa: E402
from wallex_engine.backtest.engine import BacktestEngine  # noqa: E402
from wallex_engine.config import RuntimeSecrets, load_config, redact_secrets  # noqa: E402
from wallex_engine.data.market_data import ParquetCandleStore, WallexMarketDataSource  # noqa: E402
from wallex_engine.errors import ConfigurationError, EngineError  # noqa: E402
from wallex_engine.exchange.wallex_client import WallexClient  # noqa: E402
from wallex_engine.models import WALLEX_FEES  # noqa: E402
from wallex_engine.reporting.generator import (  # noqa: E402
    generate_backtest_report,
    generate_database_report,
    performance_metrics,
)
from wallex_engine.runtime.alerts import AlertManager  # noqa: E402
from wallex_engine.runtime.engine import TradingRuntime  # noqa: E402
from wallex_engine.runtime.health import HealthState, create_health_app  # noqa: E402
from wallex_engine.storage.database import SqlEventStore, write_run_manifest  # noqa: E402


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Wallex AI Trading Engine")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("backtest", "shadow", "live"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="config.yaml")
    report = commands.add_parser("report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--config", default="config.yaml")
    return root


async def run_backtest(config_path: str) -> None:
    config = load_config(config_path)
    config.require_challenge_ready("backtest")
    secrets = RuntimeSecrets.from_environment()
    database_url = secrets.database_url.get_secret_value()
    store = SqlEventStore(database_url, config.app.logs_dir)
    await store.initialize()
    api_key = secrets.wallex_api_key.get_secret_value() if secrets.wallex_api_key else None
    client = WallexClient(
        config.exchange.rest_url,
        config.exchange.websocket_url,
        api_key,
        config.exchange.request_timeout_seconds,
        config.exchange.max_retries,
    )
    try:
        markets = await client.get_markets()
        candle_store = ParquetCandleStore(config.app.data_dir / "candles")
        source = WallexMarketDataSource(client, candle_store)
        ai_comparison_enabled = config.ai.enabled and config.ai.provider != "deterministic"
        try:
            ai = build_ai_service(config, secrets)
        except ConfigurationError:
            ai_comparison_enabled = False
            config = config.model_copy(
                update={"ai": config.ai.model_copy(update={"enabled": False})}
            )
            ai = build_ai_service(config, secrets)
            print("AI credentials are absent; running deterministic AI-off baseline.")
        ai.audit_hook = store.append_ai_call
        start = config.backtest.start or config.challenge.starts_at
        end = config.backtest.end or (
            config.challenge.ends_at and min(config.challenge.ends_at, datetime.now(tz=UTC))
        )
        if start is None or end is None or start >= end:
            raise ConfigurationError("backtest start/end define an empty interval")
        for symbol in config.challenge.allowed_markets:
            spec = markets.get(symbol.upper())
            if spec is None or not spec.active or spec.quote_asset != "TMN":
                raise ConfigurationError(f"allowed market is unavailable or unsafe: {symbol}")
            candles_1m = await source.history(symbol, 1, start, end)
            from wallex_engine.data.market_data import resample_completed

            candles_5m = resample_completed(candles_1m, config.strategy.signal_timeframe_minutes)
            candles_1h = await source.history(symbol, 60, start, end)
            run_id = f"bt-{symbol}-{datetime.now(tz=UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"
            write_run_manifest(
                config.app.data_dir / "manifests",
                run_id,
                "backtest",
                safe_config_payload(config),
            )
            comparison = None
            if ai_comparison_enabled:
                baseline_run_id = f"{run_id}-ai-off"
                write_run_manifest(
                    config.app.data_dir / "manifests",
                    baseline_run_id,
                    "backtest-ai-off",
                    safe_config_payload(config),
                )
                baseline_ai = AIReviewService(DeterministicAIProvider())
                baseline_ai.audit_hook = store.append_ai_call
                baseline_result = await BacktestEngine(config, store, baseline_ai).run(
                    baseline_run_id,
                    spec,
                    candles_5m,
                    candles_1h,
                    WALLEX_FEES,
                )
                generate_backtest_report(
                    baseline_result,
                    config.app.reports_dir,
                    float(config.challenge.initial_capital_tmn),
                )
            result = await BacktestEngine(config, store, ai).run(
                run_id,
                spec,
                candles_5m,
                candles_1h,
                WALLEX_FEES,
            )
            if ai_comparison_enabled:
                comparison = _ai_comparison(
                    result,
                    baseline_result,
                    float(config.challenge.initial_capital_tmn),
                )
            md, html = generate_backtest_report(
                result,
                config.app.reports_dir,
                float(config.challenge.initial_capital_tmn),
                comparison,
            )
            print(f"Backtest complete: {run_id}\nReport: {md}\nInteractive: {html}")
    finally:
        await client.close()
        await store.close()


def _ai_comparison(ai_on, ai_off, baseline: float) -> dict[str, float]:
    on = performance_metrics(ai_on.equity, ai_on.trades, baseline)
    off = performance_metrics(ai_off.equity, ai_off.trades, baseline)
    return {
        "بازده AI روشن": on["return"],
        "بازده AI خاموش": off["return"],
        "اختلاف بازده": on["return"] - off["return"],
        "افت سرمایه AI روشن": on["max_drawdown"],
        "افت سرمایه AI خاموش": off["max_drawdown"],
        "اختلاف سود تحقق‌یافته": on["realized_pnl_tmn"] - off["realized_pnl_tmn"],
        "اختلاف تعداد معاملات": on["trade_count"] - off["trade_count"],
    }


async def run_service(mode: str, config_path: str) -> None:
    config = load_config(config_path)
    config.require_challenge_ready(mode)
    secrets = RuntimeSecrets.from_environment()
    if mode == "live":
        secrets.require_live_readiness(config)
    api_key = secrets.wallex_api_key.get_secret_value() if secrets.wallex_api_key else None
    client = WallexClient(
        config.exchange.rest_url,
        config.exchange.websocket_url,
        api_key,
        config.exchange.request_timeout_seconds,
        config.exchange.max_retries,
    )
    store = SqlEventStore(secrets.database_url.get_secret_value(), config.app.logs_dir)
    source = WallexMarketDataSource(client, ParquetCandleStore(config.app.data_dir / "candles"))
    ai = build_ai_service(config, secrets)
    ai.audit_hook = store.append_ai_call
    alerts = AlertManager(
        secrets.telegram_bot_token.get_secret_value() if secrets.telegram_bot_token else None,
        secrets.telegram_chat_id.get_secret_value() if secrets.telegram_chat_id else None,
    )
    health = HealthState()
    runtime = TradingRuntime(mode, config, client, source, store, ai, alerts, health)
    write_run_manifest(
        config.app.data_dir / "manifests",
        runtime.run_id,
        mode,
        safe_config_payload(config),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_health_app(health),
            host="0.0.0.0",
            port=int(os.getenv("PORT", "8080")),
            log_level="warning",
        )
    )
    try:
        await asyncio.gather(runtime.run_forever(), server.serve())
    finally:
        await client.close()
        await store.close()


async def run_report(config_path: str, run_id: str) -> None:
    config = load_config(config_path)
    secrets = RuntimeSecrets.from_environment()
    store = SqlEventStore(secrets.database_url.get_secret_value(), config.app.logs_dir)
    await store.initialize()
    try:
        md, html = await generate_database_report(
            store,
            run_id,
            config.app.reports_dir,
            float(config.challenge.initial_capital_tmn),
        )
        print(f"Report: {md}\nInteractive: {html}")
    finally:
        await store.close()


async def async_main() -> None:
    args = parser().parse_args()
    if args.command == "backtest":
        await run_backtest(args.config)
    elif args.command in {"shadow", "live"}:
        await run_service(args.command, args.config)
    else:
        await run_report(args.config, args.run_id)


if __name__ == "__main__":
    secrets = RuntimeSecrets.from_environment()
    try:
        asyncio.run(async_main())
    except (ConfigurationError, EngineError, ValueError) as exc:
        print(redact_secrets(f"ERROR: {exc}", secrets), file=sys.stderr)
        raise SystemExit(2) from None
