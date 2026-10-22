"""Challenge scorecard and live risk state, read from the engine's own database.

Fills carry no run id, so they are attributed through
fills.client_id -> order_intents.decision_id -> decisions.run_id.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import AppConfig
from .results import equity_curve


def latest_run(connection: Connection, mode: str) -> str | None:
    row = connection.execute(
        text(
            "SELECT run_id FROM equity_snapshots WHERE run_id LIKE :prefix "
            "ORDER BY captured_at DESC LIMIT 1"
        ),
        {"prefix": f"{mode}-%"},
    ).first()
    return row[0] if row else None


def run_scorecard(
    connection: Connection, mode: str, baseline: float, logs_dir: Path
) -> dict[str, Any]:
    """The brief's Part 1 metrics for the latest run of ``mode``."""
    run_id = latest_run(connection, mode)
    card: dict[str, Any] = {"run_id": run_id, "mode": mode}
    if run_id is None:
        return card
    fills = pd.read_sql(
        text(
            "SELECT f.side, f.quantity, f.price, f.fee_tmn, f.filled_at, f.client_id, "
            "i.created_at AS intended_at FROM fills f "
            "JOIN order_intents i ON i.client_id = f.client_id "
            "JOIN decisions d ON d.decision_id = i.decision_id AND d.run_id = :run"
        ),
        connection,
        params={"run": run_id},
    )
    notional = pd.to_numeric(fills["quantity"]) * pd.to_numeric(fills["price"])
    latencies = []
    for _, group in fills.groupby("client_id"):
        first_fill = pd.to_datetime(group["filled_at"]).min()
        intended = pd.to_datetime(group["intended_at"]).min()
        latencies.append((first_fill - intended).total_seconds())
    snapshots = pd.read_sql(
        text("SELECT run_id, captured_at, equity_tmn FROM equity_snapshots WHERE run_id = :run"),
        connection,
        params={"run": run_id},
    )
    _, stats = equity_curve(snapshots, run_id, baseline)
    decisions = connection.execute(
        text("SELECT COUNT(*) FROM decisions WHERE run_id = :run"), {"run": run_id}
    ).scalar_one()
    card.update(
        fills=len(fills),
        round_trips=int((fills["side"].str.upper() == "SELL").sum()),
        volume_tmn=float(notional.sum()),
        avg_trade_tmn=float(notional.mean()) if len(fills) else 0.0,
        fees_tmn=float(pd.to_numeric(fills["fee_tmn"]).sum()),
        latency_median_s=statistics.median(latencies) if latencies else None,
        latency_max_s=max(latencies) if latencies else None,
        decisions=int(decisions),
        errors=_count_errors(Path(logs_dir), mode),
        **stats,
    )
    return card


def risk_state(
    connection: Connection, config: AppConfig, mode: str, prices: dict[str, float]
) -> dict[str, Any]:
    """Each configured limit next to its current usage, open positions and P&L."""
    baseline = float(config.challenge.initial_capital_tmn)
    run_id = latest_run(connection, mode)
    latest: dict[str, Any] = {}
    if run_id:
        row = connection.execute(
            text(
                "SELECT payload_json FROM equity_snapshots WHERE run_id = :run "
                "ORDER BY captured_at DESC LIMIT 1"
            ),
            {"run": run_id},
        ).first()
        latest = json.loads(row[0]) if row else {}
    equity = float(latest.get("equity_tmn", baseline))
    positions = []
    for (payload,) in connection.execute(text("SELECT payload_json FROM positions")):
        position = json.loads(payload)
        quantity = float(position.get("quantity", 0))
        if quantity <= 0:
            continue
        entry = float(position["average_entry"])
        last = prices.get(position["symbol"])
        positions.append(
            {
                "symbol": position["symbol"],
                "quantity": quantity,
                "entry": entry,
                "last": last,
                "stop_price": _float(position.get("stop_price")),
                "target_price": _float(position.get("target_price")),
                "unrealized_tmn": None if last is None else quantity * (last - entry),
                "value_tmn": quantity * (last if last is not None else entry),
            }
        )
    exposure = sum(p["value_tmn"] for p in positions)
    unrealized = sum(p["unrealized_tmn"] or 0 for p in positions)
    risk, execution = config.risk, config.execution
    canary = execution.canary
    kill = connection.execute(text("SELECT active FROM kill_switch_state LIMIT 1")).first()
    limits = [
        _limit("daily_loss", "حد ضرر روزانه", max(0.0, -float(latest.get("daily_pnl", 0))),
               float(risk.daily_loss_stop)),
        _limit("halve_size", "نصف شدن حجم با افت", float(latest.get("drawdown", 0)),
               float(risk.halve_size_drawdown)),
        _limit("kill_switch", "توقف اضطراری با افت", float(latest.get("drawdown", 0)),
               float(risk.kill_switch_drawdown)),
        _limit("total_exposure", "سقف کل سرمایه درگیر", exposure / equity if equity else 0,
               float(execution.canary_total_exposure if canary else risk.max_total_exposure)),
        _limit("tmn_reserve", "حداقل ذخیره تومانی (باقی‌مانده)", 1 - exposure / equity
               if equity else 1, float(risk.min_tmn_reserve), floor=True),
        _limit("positions", "تعداد موقعیت باز", len(positions),
               float(execution.canary_max_positions if canary else risk.max_positions)),
    ]
    return {
        "run_id": run_id,
        "mode": mode,
        "canary": canary,
        "kill_switch": bool(kill and kill[0]),
        "equity_tmn": equity,
        "limits": limits,
        "per_trade": {
            "risk_per_trade": float(
                execution.canary_risk_per_trade if canary else risk.risk_per_trade
            ),
            "max_symbol_exposure": float(
                execution.canary_symbol_exposure if canary else risk.max_symbol_exposure
            ),
            "stop_atr": float(config.strategy.stop_atr),
            "target_r": float(config.strategy.trend_target_r),
            "time_stop_bars": config.strategy.time_stop_bars,
            "cooldown_minutes": risk.cooldown_minutes,
            "loss_cooldown_minutes": risk.loss_cooldown_minutes,
        },
        "positions": positions,
        "pnl": {
            "total_tmn": equity - baseline,
            "unrealized_tmn": unrealized,
            "realized_tmn": equity - baseline - unrealized,
        },
    }


def _limit(key: str, label: str, used: float, limit: float, floor: bool = False) -> dict:
    breached = used < limit if floor else used >= limit
    return {"key": key, "label_fa": label, "used": used, "limit": limit,
            "floor": floor, "breached": breached}


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _count_errors(logs_dir: Path, mode: str) -> int:
    count = 0
    for path in logs_dir.glob(f"engine-{mode}-*.log"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"level": "error"' in line or '"level": "critical"' in line or "Traceback" in line:
                count += 1
    return count
