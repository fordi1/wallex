from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Response, status


class HealthState:
    def __init__(self) -> None:
        self.started_at = datetime.now(tz=UTC)
        self.last_market_data_at: datetime | None = None
        self.last_reconciliation_at: datetime | None = None
        self.mode = "starting"
        self.ready = False
        self.detail = "در حال راه‌اندازی"

    def payload(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "mode": self.mode,
            "detail": self.detail,
            "started_at": self.started_at.isoformat(),
            "last_market_data_at": self.last_market_data_at.isoformat()
            if self.last_market_data_at
            else None,
            "last_reconciliation_at": self.last_reconciliation_at.isoformat()
            if self.last_reconciliation_at
            else None,
        }


def create_health_app(state: HealthState) -> FastAPI:
    app = FastAPI(title="Wallex Engine Health", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        if not state.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return state.payload()

    return app
