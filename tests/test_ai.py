from __future__ import annotations

from decimal import Decimal

import pytest

from wallex_engine.ai.providers import AIReviewService, DecisionCache, compact_snapshot


class FailingProvider:
    async def review_regime(self, snapshot):
        raise TimeoutError

    async def review_signal(self, snapshot, signal):
        raise TimeoutError


@pytest.mark.asyncio
async def test_three_ai_failures_pause_entries(feature) -> None:
    service = AIReviewService(FailingProvider())
    for _ in range(3):
        result = await service.regime(feature)
    assert service.entries_paused
    assert not result.allow_entries
    assert result.size_multiplier == Decimal("0.5")


def test_cache_key_changes_with_prompt_version(tmp_path) -> None:
    cache = DecisionCache(tmp_path)
    payload = {"x": "1"}
    first = cache.key("p", "m", "v1", "s1", payload, {"seed": 1})
    second = cache.key("p", "m", "v2", "s1", payload, {"seed": 1})
    assert first != second


def test_compact_snapshot_contains_no_private_fields(feature) -> None:
    payload = compact_snapshot(feature)
    forbidden = {"api_key", "account", "balance", "credential", "token"}
    assert not forbidden.intersection(payload)
