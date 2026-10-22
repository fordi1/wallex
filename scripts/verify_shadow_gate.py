from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the immutable 24-hour shadow gate")
    parser.add_argument("--output", default="reports/shadow-gate.json")
    args = parser.parse_args()
    url = os.getenv("DASHBOARD_DATABASE_URL", "sqlite:///./data/wallex.db")
    url = url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
    engine = create_engine(url, pool_pre_ping=True)
    now = datetime.now(tz=UTC)
    with engine.connect() as connection:
        summary = (
            connection.execute(
                text(
                    "SELECT MIN(created_at) AS started_at, MAX(created_at) AS ended_at, "
                    "COUNT(*) AS decisions FROM decisions WHERE mode='shadow'"
                )
            )
            .mappings()
            .one()
        )
        duplicate_ids = connection.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT client_id FROM order_intents "
                "GROUP BY client_id HAVING COUNT(*) > 1) AS duplicates"
            )
        ).scalar_one()
        unknown_orders = connection.execute(
            text("SELECT COUNT(*) FROM orders WHERE status IN ('UNKNOWN', 'REJECTED')")
        ).scalar_one()
        reconciliation = connection.execute(
            text("SELECT value FROM service_state WHERE key='reconciliation_status'")
        ).scalar_one_or_none()
        incomplete = connection.execute(
            text("SELECT COUNT(*) FROM decisions WHERE payload_json IS NULL OR reason_fa='' ")
        ).scalar_one()
    started = _utc(summary["started_at"])
    ended = _utc(summary["ended_at"])
    duration_ok = bool(started and ended and ended - started >= timedelta(hours=24))
    freshness_ok = bool(ended and now - ended <= timedelta(minutes=10))
    checks = {
        "continuous_24h": duration_ok,
        "fresh_decisions": freshness_ok,
        "no_duplicate_client_ids": duplicate_ids == 0,
        "no_invalid_orders": unknown_orders == 0,
        "reconciliation_clean": reconciliation == "ok",
        "complete_decision_records": incomplete == 0 and summary["decisions"] > 0,
    }
    result = {
        "approved": all(checks.values()),
        "checked_at": now.isoformat(),
        "checks": checks,
        "summary": dict(summary),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["approved"] else 2


def _utc(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
