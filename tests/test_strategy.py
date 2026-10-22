from __future__ import annotations

from decimal import Decimal

from wallex_engine.models import SignalAction
from wallex_engine.strategy.hybrid import HybridStrategy


def test_trend_entry_requires_fee_adjusted_edge(config, feature) -> None:
    strategy = HybridStrategy(config.strategy)
    accepted = strategy.evaluate(feature)
    assert accepted.action == SignalAction.ENTER_LONG
    expensive = feature.model_copy(
        update={"fee_rate": Decimal("0.02"), "modeled_slippage_bps": Decimal("100")}
    )
    rejected = strategy.evaluate(expensive)
    assert rejected.action == SignalAction.SKIP


def test_spread_rejects_before_strategy(config, feature) -> None:
    strategy = HybridStrategy(config.strategy)
    result = strategy.evaluate(
        feature.model_copy(update={"spread_bps": config.strategy.max_spread_bps + 1})
    )
    assert result.action == SignalAction.SKIP
    assert "اسپرد" in result.reason_fa
