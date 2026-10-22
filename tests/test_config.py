from __future__ import annotations

import pytest

from wallex_engine.config import AppConfig, RuntimeSecrets
from wallex_engine.errors import ConfigurationError


def test_challenge_defaults_fail_closed() -> None:
    with pytest.raises(ConfigurationError, match="allowed_markets"):
        AppConfig().require_challenge_ready("shadow")


def test_backtest_uses_its_own_window_without_challenge_times() -> None:
    config = AppConfig.model_validate(
        {
            "challenge": {"allowed_markets": ["BTCTMN"]},
            "backtest": {
                "start": "2026-10-01T00:00:00+03:30",
                "end": "2026-10-15T00:00:00+03:30",
            },
        }
    )
    config.require_challenge_ready("backtest")
    with pytest.raises(ConfigurationError, match="starts_at"):
        config.require_challenge_ready("shadow")


def test_live_requires_every_readiness_gate(config, monkeypatch) -> None:
    monkeypatch.setenv("WALLEX_API_KEY", "never-log-this")
    secrets = RuntimeSecrets.from_environment()
    with pytest.raises(ConfigurationError, match="exposed key"):
        secrets.require_live_readiness(config)


def test_direct_api_is_not_a_config_secret(config) -> None:
    serialized = config.model_dump_json()
    assert "api_key" not in serialized.lower()
