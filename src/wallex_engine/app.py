from __future__ import annotations

import os
from pathlib import Path

from .ai.providers import (
    AIReviewService,
    BoxAIProvider,
    DecisionCache,
    DeterministicAIProvider,
    DirectOpenAIProvider,
    LiaraAIProvider,
)
from .config import AISection, AppConfig, RuntimeSecrets
from .errors import ConfigurationError


def build_ai_service(config: AppConfig, secrets: RuntimeSecrets) -> AIReviewService:
    cache = DecisionCache(config.app.data_dir / "ai-cache")
    ai_config: AISection = config.ai
    if not ai_config.enabled or ai_config.provider == "deterministic":
        provider = DeterministicAIProvider()
    elif ai_config.provider == "liara":
        if secrets.liara_ai_api_key is None:
            raise ConfigurationError("LIARA_AI_API_KEY is required when ai.provider is liara")
        base_url = os.getenv("LIARA_AI_BASE_URL") or ai_config.base_url
        if not base_url:
            raise ConfigurationError(
                "Liara base URL is required in ai.base_url or LIARA_AI_BASE_URL"
            )
        provider = LiaraAIProvider(
            ai_config.model_copy(update={"base_url": base_url}),
            secrets.liara_ai_api_key.get_secret_value(),
            cache,
        )
    elif ai_config.provider == "boxapi":
        if secrets.box_api_key is None:
            raise ConfigurationError("BOX_API_KEY is required when ai.provider is boxapi")
        base_url = (
            os.getenv("BOX_API_BASE_URL")
            or ai_config.base_url
            or "https://ai.boxapi.ir/ai/v1"
        )
        provider = BoxAIProvider(
            ai_config.model_copy(update={"base_url": base_url}),
            secrets.box_api_key.get_secret_value(),
            cache,
        )
    else:
        if config.app.environment == "liara" or os.getenv("LIARA_APP_NAME"):
            raise ConfigurationError("direct OpenAI access is disabled in the Liara runtime")
        if secrets.openai_api_key is None:
            raise ConfigurationError("OPENAI_API_KEY is required when ai.provider is openai")
        provider = DirectOpenAIProvider(ai_config, secrets.openai_api_key.get_secret_value(), cache)
    return AIReviewService(provider, ai_config.fallback_size_multiplier)


def safe_config_payload(config: AppConfig) -> dict[str, object]:
    payload = config.model_dump(mode="json")
    return payload


def resolve_path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value).resolve()
