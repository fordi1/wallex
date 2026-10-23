"""Tests must never inherit trading credentials or notification destinations."""
import pytest


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    import pishro.config
    monkeypatch.setattr(pishro.config, "load_dotenv", lambda *a, **k: None)
    for name in ("WALLEX_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "BALE_BOT_TOKEN",
                 "BALE_CHAT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "LIVE_CONFIRM", "DASHBOARD_PASSWORD"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("PISHRO_MODE", "paper")
