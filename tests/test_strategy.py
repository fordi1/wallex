from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


def test_explain_lists_every_condition_with_pass_or_fail(config, feature) -> None:
    strategy = HybridStrategy(config.strategy)
    explanation = strategy.explain(feature)
    assert explanation.action == SignalAction.ENTER_LONG
    assert explanation.strategy.value == "trend"
    trend = [c for c in explanation.checks if c.family == "trend"]
    assert trend and all(c.passed for c in trend)
    no_cross = feature.model_copy(update={"previous_ema_9": Decimal("99"), "rsi_14": Decimal("40")})
    blocked = strategy.explain(no_cross)
    assert blocked.action == SignalAction.SKIP
    assert blocked.failed("trend") >= 1
    assert {c.key for c in blocked.checks} >= {"spread", "ema_cross", "hourly_trend", "edge_trend"}


# Fixtures are immutable models; each example only uses model_copy().
@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    close=st.decimals("90", "110", places=2),
    ema_9=st.decimals("90", "110", places=2),
    previous_ema_9=st.decimals("90", "110", places=2),
    hourly_ema_20=st.decimals("90", "110", places=2),
    previous_close=st.decimals("85", "110", places=2),
    previous_rsi=st.decimals("10", "70", places=1),
    atr=st.decimals("0.1", "6", places=2),
    spread=st.decimals("0", "40", places=1),
    volume_z=st.decimals("-2", "2", places=2),
)
def test_explain_verdict_always_matches_evaluate(
    config, feature, close, ema_9, previous_ema_9, hourly_ema_20, previous_close,
    previous_rsi, atr, spread, volume_z,
) -> None:
    snapshot = feature.model_copy(
        update={
            "close": close, "ema_9": ema_9, "previous_ema_9": previous_ema_9,
            "hourly_ema_20": hourly_ema_20, "previous_close": previous_close,
            "previous_rsi_14": previous_rsi, "atr_14": atr, "spread_bps": spread,
            "volume_zscore_20": volume_z,
        }
    )
    strategy = HybridStrategy(config.strategy)
    signal = strategy.evaluate(snapshot)
    explanation = strategy.explain(snapshot)
    assert (explanation.action, explanation.strategy) == (signal.action, signal.strategy)
