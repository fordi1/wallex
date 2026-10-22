from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..config import AISection
from ..models import FeatureSnapshot, RegimeDecision, ReviewDecision, TradeSignal

T = TypeVar("T", bound=BaseModel)


SYSTEM_PROMPT = """You are a conservative spot-market risk reviewer.
You may only veto or shrink an entry. Never suggest leverage, a new trade, a larger size,
or weaker risk limits. Return only the requested JSON object. The reason must be concise Persian.
The input contains public market features only."""


class DecisionCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def key(
        self,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> str:
        normalized = json.dumps(
            {
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "payload": payload,
                "settings": settings,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(normalized.encode()).hexdigest()

    def get(self, key: str, schema: type[T]) -> T | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, key: str, value: BaseModel) -> None:
        path = self.root / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value.model_dump_json(), encoding="utf-8")
        temporary.replace(path)


class LiaraAIProvider:
    name = "liara"
    supports_temperature = True
    supports_seed = True

    def __init__(self, config: AISection, api_key: str, cache: DecisionCache) -> None:
        if not config.base_url:
            raise ValueError("Liara AI base URL is required")
        self.config = config
        self.cache = cache
        self.client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)
        self.last_call: dict[str, Any] = {}

    async def review_regime(self, snapshot: FeatureSnapshot) -> RegimeDecision:
        payload = {"kind": "regime", "snapshot": compact_snapshot(snapshot)}
        return await self._cached_call(payload, RegimeDecision)

    async def review_signal(self, snapshot: FeatureSnapshot, signal: TradeSignal) -> ReviewDecision:
        payload = {
            "kind": "signal",
            "snapshot": compact_snapshot(snapshot),
            "signal": {
                "strategy": signal.strategy.value,
                "expected_move_bps": str(signal.expected_move_bps),
                "entry_price": _string(signal.entry_price),
                "stop_price": _string(signal.stop_price),
                "target_price": _string(signal.target_price),
            },
        }
        return await self._cached_call(payload, ReviewDecision)

    async def _cached_call(self, payload: dict[str, Any], schema: type[T]) -> T:
        settings = {
            "temperature": (
                self.config.temperature if self.supports_temperature else "provider-default"
            ),
            "seed": self.config.seed if self.supports_seed else "unsupported",
        }
        key = self.cache.key(
            self.name,
            self.config.model,
            self.config.prompt_version,
            self.config.schema_version,
            payload,
            settings,
        )
        if cached := self.cache.get(key, schema):
            self.last_call = {"cache_key": key, "cache_hit": True}
            return cached
        error: Exception | None = None
        for _ in range(2):
            try:
                request: dict[str, Any] = {
                    "model": self.config.model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "input": payload,
                                    "output_schema": schema.model_json_schema(),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                }
                if self.supports_temperature:
                    request["temperature"] = self.config.temperature
                if self.supports_seed and self.config.seed is not None:
                    request["seed"] = self.config.seed
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request),
                    timeout=self.config.timeout_seconds,
                )
                content = response.choices[0].message.content or ""
                parsed = schema.model_validate_json(content)
                self.cache.put(key, parsed)
                self.last_call = {"cache_key": key, "cache_hit": False}
                return parsed
            except Exception as exc:
                error = exc
        detail = str(error) if error is not None else "unknown error"
        raise RuntimeError(
            f"AI output failed strict validation: {type(error).__name__}: {detail}"
        )


class BoxAIProvider(LiaraAIProvider):
    """OpenAI-compatible BoxAPI adapter with the same strict JSON contract."""

    name = "boxapi"
    supports_temperature = False
    supports_seed = False


class DirectOpenAIProvider:
    name = "openai"

    def __init__(self, config: AISection, api_key: str, cache: DecisionCache) -> None:
        self.config = config
        self.model_name = config.model.removeprefix("openai/")
        self.cache = cache
        self.client = AsyncOpenAI(api_key=api_key)
        self.last_call: dict[str, Any] = {}

    async def review_regime(self, snapshot: FeatureSnapshot) -> RegimeDecision:
        return await self._cached_parse(
            {"kind": "regime", "snapshot": compact_snapshot(snapshot)}, RegimeDecision
        )

    async def review_signal(self, snapshot: FeatureSnapshot, signal: TradeSignal) -> ReviewDecision:
        return await self._cached_parse(
            {
                "kind": "signal",
                "snapshot": compact_snapshot(snapshot),
                "signal": {
                    "strategy": signal.strategy.value,
                    "expected_move_bps": str(signal.expected_move_bps),
                },
            },
            ReviewDecision,
        )

    async def _cached_parse(self, payload: dict[str, Any], schema: type[T]) -> T:
        settings = {"temperature": self.config.temperature, "seed": self.config.seed}
        key = self.cache.key(
            self.name,
            self.model_name,
            self.config.prompt_version,
            self.config.schema_version,
            payload,
            settings,
        )
        if cached := self.cache.get(key, schema):
            self.last_call = {"cache_key": key, "cache_hit": True}
            return cached
        response = await asyncio.wait_for(
            self.client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                text_format=schema,
                temperature=self.config.temperature,
            ),
            timeout=self.config.timeout_seconds,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned a refusal or no structured output")
        self.cache.put(key, parsed)
        self.last_call = {"cache_key": key, "cache_hit": False}
        return parsed


class DeterministicAIProvider:
    name = "deterministic"
    last_call = {"cache_key": "deterministic", "cache_hit": True}

    async def review_regime(self, snapshot: FeatureSnapshot) -> RegimeDecision:
        trending = abs(snapshot.hourly_ema_20 - snapshot.hourly_ema_50) / snapshot.close
        return RegimeDecision(
            regime="trend" if trending >= Decimal("0.01") else "range",
            allow_entries=True,
            size_multiplier=Decimal("1"),
            confidence=Decimal("1"),
            reason_fa="رژیم با قواعد قطعی محاسبه شد.",
        )

    async def review_signal(self, snapshot: FeatureSnapshot, signal: TradeSignal) -> ReviewDecision:
        return ReviewDecision(
            approve=True,
            size_adjustment=Decimal("1"),
            reason_fa="بازبینی قطعی سیگنال را تأیید کرد.",
        )


class AIReviewService:
    def __init__(self, provider: Any, fallback_multiplier: Decimal = Decimal("0.5")) -> None:
        self.provider = provider
        self.fallback_multiplier = fallback_multiplier
        self.consecutive_failures = 0
        self.entries_paused = False
        self.audit_hook: Callable[..., Awaitable[None]] | None = None

    async def regime(self, snapshot: FeatureSnapshot) -> RegimeDecision:
        try:
            result = await self.provider.review_regime(snapshot)
        except Exception:
            self.consecutive_failures += 1
            self.entries_paused = self.consecutive_failures >= 3
            result = RegimeDecision(
                regime="deterministic-fallback",
                allow_entries=not self.entries_paused,
                size_multiplier=self.fallback_multiplier,
                confidence=Decimal("0"),
                reason_fa="پاسخ هوش مصنوعی معتبر نبود؛ قواعد قطعی با نصف اندازه فعال شد.",
            )
            await self._audit("regime", True, result)
            return result
        self.consecutive_failures = 0
        self.entries_paused = False
        await self._audit("regime", False, result)
        return result

    async def review(self, snapshot: FeatureSnapshot, signal: TradeSignal) -> ReviewDecision:
        try:
            result = await self.provider.review_signal(snapshot, signal)
        except Exception:
            self.consecutive_failures += 1
            self.entries_paused = self.consecutive_failures >= 3
            result = ReviewDecision(
                approve=not self.entries_paused,
                size_adjustment=self.fallback_multiplier,
                reason_fa="بازبینی هوش مصنوعی ناموفق بود؛ قواعد قطعی اعمال شد.",
            )
            await self._audit("signal", True, result)
            return result
        self.consecutive_failures = 0
        await self._audit("signal", False, result)
        return result

    async def _audit(self, kind: str, fallback: bool, result: BaseModel) -> None:
        if self.audit_hook is None:
            return
        metadata = getattr(self.provider, "last_call", {})
        config = getattr(self.provider, "config", None)
        await self.audit_hook(
            provider=getattr(self.provider, "name", "unknown"),
            model=getattr(config, "model", "deterministic"),
            cache_key=str(metadata.get("cache_key", "unavailable")),
            cache_hit=bool(metadata.get("cache_hit", False)),
            fallback=fallback,
            payload={"kind": kind, "result": result.model_dump(mode="json")},
        )


def compact_snapshot(snapshot: FeatureSnapshot) -> dict[str, str]:
    allowed = (
        "symbol",
        "observed_at",
        "close",
        "ema_9",
        "ema_21",
        "hourly_ema_20",
        "hourly_ema_50",
        "rsi_14",
        "atr_14",
        "bollinger_mid",
        "bollinger_lower",
        "bollinger_upper",
        "vwap",
        "volume_zscore_20",
        "spread_bps",
        "orderbook_imbalance_5",
        "fee_rate",
    )
    result: dict[str, str] = {}
    for key in allowed:
        value = getattr(snapshot, key)
        result[key] = value.isoformat() if isinstance(value, datetime) else str(value)
    return result


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def cache_audit_record(cache_key: str, hit: bool) -> dict[str, Any]:
    return {"cache_key": cache_key, "cache_hit": hit, "at": datetime.now(tz=UTC).isoformat()}
