from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from wallex_engine.ai.providers import BoxAIProvider, DecisionCache
from wallex_engine.app import build_ai_service
from wallex_engine.config import AppConfig, RuntimeSecrets


def test_boxapi_provider_uses_official_default_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BOX_API_BASE_URL", raising=False)
    config = AppConfig.model_validate(
        {
            "app": {"data_dir": str(tmp_path)},
            "ai": {"enabled": True, "provider": "boxapi", "base_url": None},
        }
    )
    secrets = RuntimeSecrets(box_api_key=SecretStr("test-only"))

    service = build_ai_service(config, secrets)

    assert isinstance(service.provider, BoxAIProvider)
    assert service.provider.name == "boxapi"
    assert str(service.provider.client.base_url) == "https://ai.boxapi.ir/ai/v1/"


def test_boxapi_secret_is_redacted_from_runtime_repr() -> None:
    secrets = RuntimeSecrets(box_api_key=SecretStr("not-visible"))

    assert "not-visible" not in repr(secrets)


@pytest.mark.asyncio
async def test_boxapi_omits_unsupported_sampling_controls_and_sends_schema(
    tmp_path, feature
) -> None:
    config = AppConfig.model_validate(
        {
            "ai": {
                "enabled": True,
                "provider": "boxapi",
                "base_url": "https://ai.boxapi.ir/ai/v1",
            }
        }
    )
    provider = BoxAIProvider(config.ai, "test-only", DecisionCache(tmp_path))
    captured: dict[str, object] = {}

    async def create(**kwargs):
        captured.update(kwargs)
        content = json.dumps(
            {
                "regime": "range",
                "allow_entries": True,
                "size_multiplier": "0.5",
                "confidence": "0.8",
                "reason_fa": "بازار خنثی است.",
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    result = await provider.review_regime(feature)

    assert result.regime == "range"
    assert "temperature" not in captured
    assert "seed" not in captured
    user_payload = json.loads(captured["messages"][1]["content"])
    assert "output_schema" in user_payload
