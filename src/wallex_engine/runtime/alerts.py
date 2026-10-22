from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx


class AlertManager:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, event: str, message_fa: str, **context: object) -> None:
        record = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": "alert",
            "event": event,
            "message_fa": message_fa,
            **context,
        }
        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(url, json={"chat_id": self.chat_id, "text": message_fa})
        except httpx.HTTPError:
            print(
                json.dumps(
                    {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "level": "error",
                        "event": "telegram_delivery_failed",
                    }
                ),
                flush=True,
            )
