"""Read judging evidence (decision log, sweeps, backtest reports) for the dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

AI_COMPARISON_HEADING = "## مقایسه AI"


def decision_frame(path: Path) -> pd.DataFrame:
    """Flatten logs/decisions.jsonl: one row per decision with rule, AI and risk fields."""
    if not path.exists():
        return pd.DataFrame()
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            signal = record.get("signal") or {}
            regime = record.get("regime") or {}
            review = record.get("review") or {}
            risk = record.get("risk") or {}
            rows.append(
                {
                    "created_at": record.get("created_at"),
                    "mode": record.get("mode"),
                    "run_id": record.get("run_id"),
                    "symbol": record.get("symbol"),
                    "outcome": record.get("outcome"),
                    "reason_fa": record.get("reason_fa"),
                    "action": signal.get("action"),
                    "strategy": signal.get("strategy"),
                    "signal_reason": signal.get("reason_fa"),
                    "ai_regime": regime.get("regime"),
                    "ai_allow": regime.get("allow_entries"),
                    "ai_size": regime.get("size_multiplier"),
                    "ai_confidence": regime.get("confidence"),
                    "ai_regime_reason": regime.get("reason_fa"),
                    "ai_approve": review.get("approve"),
                    "ai_review_reason": review.get("reason_fa"),
                    "risk_approved": risk.get("approved"),
                    "risk_reason": risk.get("reason_fa"),
                }
            )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True)
    return frame


def decision_summary(frame: pd.DataFrame) -> dict[str, int]:
    """Counts for the judging story: how many rule signals the AI vetoed."""
    if frame.empty:
        return {"decisions": 0, "signals": 0, "filled": 0, "ai_vetoes": 0}
    signals = frame[frame["action"].fillna("SKIP") != "SKIP"]
    vetoed = (signals["ai_approve"] == False) | (signals["ai_allow"] == False)  # noqa: E712
    return {
        "decisions": len(frame),
        "signals": len(signals),
        "filled": int((frame["outcome"] == "filled").sum()),
        "ai_vetoes": int(vetoed.sum()),
    }


def parse_report_tables(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    """Return (metrics, AI on/off comparison) from a generated backtest markdown report."""
    metrics: dict[str, float] = {}
    comparison: dict[str, float] = {}
    target = metrics
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(AI_COMPARISON_HEADING):
            target = comparison
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        try:
            target[cells[0]] = float(cells[1])
        except ValueError:
            continue
    return metrics, comparison


def load_sweeps(reports_dir: Path) -> dict[str, list[dict]]:
    """Every walk-forward sweep JSON in the reports folder, keyed by file stem."""
    sweeps: dict[str, list[dict]] = {}
    for path in sorted(Path(reports_dir).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            sweeps[path.stem] = payload["rows"]
    return sweeps
