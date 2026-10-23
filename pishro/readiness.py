"""Read-only readiness report: never constructs Bot or submits/cancels an order."""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time


def configuration_issues(cfg):
    issues = []
    for name in ("max_drawdown_pct", "daily_loss_limit_pct", "risk_per_trade_pct",
                 "max_position_pct", "max_total_exposure_pct", "min_cash_reserve_pct",
                 "max_book_participation"):
        value = cfg.get(f"risk.{name}")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
            issues.append(f"risk.{name} must be finite and between 0 and 1")
    for key in ("risk.max_open_positions", "risk.stale_data_sec", "risk.min_order_tmn",
                "exchange.request_timeout_sec", "exchange.max_orders_per_sec"):
        value = cfg.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            issues.append(f"{key} must be finite and positive")
    for key in ("exchange.fallback_maker_fee", "exchange.fallback_taker_fee"):
        value = cfg.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value < 1:
            issues.append(f"{key} must be a finite fractional rate")
    try:
        if cfg.challenge_end <= time.time():
            issues.append("challenge.end has expired")
    except (ValueError, TypeError):
        issues.append("challenge.end is invalid")
    if not cfg.secrets.wallex_api_key:
        issues.append("WALLEX_API_KEY is missing")
    if not cfg.secrets.dashboard_password:
        issues.append("DASHBOARD_PASSWORD is missing")
    return issues


async def preflight(cfg, online=False):
    issues = configuration_issues(cfg)
    report = {"read_only": True, "online_checked": online, "issues": issues, "checks": {}}
    state = {}
    db = cfg.data_dir / "pishro.db"
    if db.exists():
        try:
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT v FROM kv WHERE k='execution_state'").fetchone()
                state = json.loads(row[0]) if row else {}
                npos = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
                if npos and not state:
                    issues.append("legacy positions require journal migration/reconciliation")
            finally:
                conn.close()
            if state and state.get("version") != 1:
                issues.append("unsupported execution journal version")
        except (sqlite3.Error, ValueError, TypeError):
            issues.append("execution journal could not be read")
    tracked = [v["order"] for key in ("entries", "exits") for v in state.get(key, {}).values()]
    if any(o["status"] == "UNKNOWN" for o in tracked):
        issues.append("unresolved journal orders require reconciliation")
    if online:
        from .exchange.wallex import WallexClient
        from .exchange.binance import BinanceRef
        client = WallexClient(cfg.get("exchange.base_url"), cfg.secrets.wallex_api_key,
                              timeout=8, max_retries=0)
        reference = BinanceRef(cfg.get("references.binance_rest"), timeout=8,
                               fallback_rest=cfg.get("references.binance_rest_fallbacks", []))
        try:
            results = await asyncio.gather(client.markets(), client.balances(),
                client.fees(cfg.get("exchange.fallback_maker_fee"), cfg.get("exchange.fallback_taker_fee")),
                client.open_orders(), reference.poll(), return_exceptions=True)
            for name, result in zip(("markets", "balances", "fees", "open_orders", "reference"), results):
                report["checks"][name] = "failed" if isinstance(result, Exception) else "ok"
                if isinstance(result, Exception):
                    # Never print headers, payloads or exception text containing account data.
                    issues.append(f"{name}: {type(result).__name__}")
            if not isinstance(results[0], Exception):
                infos, quotes = results[0]
                if not infos or not any(q.valid for q in quotes.values()):
                    issues.append("market response contains no valid prices")
            if not isinstance(results[2], Exception) and not results[2]:
                issues.append("fee table is empty")
            if not isinstance(results[4], Exception) and not results[4]:
                issues.append("reference price response is empty")
            if not isinstance(results[3], Exception):
                known = {o["client_id"] for o in tracked}
                for o in results[3]:
                    cid = o.get("clientOrderId") or o.get("client_id") or ""
                    if cid.startswith("PR") and cid not in known:
                        issues.append("exchange has unjournaled bot orders")
                        break
        finally:
            await client.close()
            await reference.close()
    report["checks_passed"] = not issues
    report["scope"] = "Configuration and read-only API checks; no execution or profitability certification."
    return report
