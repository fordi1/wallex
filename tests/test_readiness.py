import asyncio
import json

import httpx
import pytest

from pishro.config import Secrets, load_config
from pishro.broker.live import LiveBroker
from pishro.exchange.wallex import WallexClient
from pishro.readiness import preflight
from pishro.types import Order


def test_preflight_offline_does_not_construct_bot_or_expose_secrets(tmp_path, monkeypatch):
    import pishro.bot

    def forbidden(*a, **kw):
        raise AssertionError("preflight must not construct the trading bot")

    monkeypatch.setattr(pishro.bot, "Bot", forbidden)
    cfg = load_config(overrides={"data_dir": str(tmp_path), "challenge.end": None})
    cfg.secrets = Secrets(wallex_api_key="secret-test-key", dashboard_password="secret-test-password")
    report = asyncio.run(preflight(cfg))
    assert report["checks_passed"] and not report["online_checked"]
    assert "secret-test" not in json.dumps(report)


@pytest.mark.parametrize("payload", [{}, {"result": {"status": "FILLED"}},
    {"result": {"status": "BOGUS", "executedQty": "0", "executedSum": "0"}},
    {"result": {"status": "FILLED", "executedQty": "NaN", "executedSum": "100"}}])
def test_malformed_submission_is_uncertain(payload):
    async def run():
        client = WallexClient("https://offline.example", "fake", max_retries=0)
        await client.close()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=payload)))
        try:
            return await LiveBroker(client, .0025, .003).place(Order("id", "BTCTMN", "BUY", "LIMIT", 100, 1))
        finally:
            await client.close()

    assert asyncio.run(run()).status == "UNKNOWN"
