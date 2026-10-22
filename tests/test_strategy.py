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
    cross_age=st.one_of(st.none(), st.integers(0, 8)),
    window=st.integers(1, 5),
    confluence=st.booleans(),
    imbalance=st.decimals("-0.5", "0.5", places=2),
)
def test_explain_verdict_always_matches_evaluate(
    config, feature, close, ema_9, previous_ema_9, hourly_ema_20, previous_close,
    previous_rsi, atr, spread, volume_z, cross_age, window, confluence, imbalance,
) -> None:
    snapshot = feature.model_copy(
        update={
            "close": close, "ema_9": ema_9, "previous_ema_9": previous_ema_9,
            "hourly_ema_20": hourly_ema_20, "previous_close": previous_close,
            "previous_rsi_14": previous_rsi, "atr_14": atr, "spread_bps": spread,
            "volume_zscore_20": volume_z, "ema_cross_age": cross_age,
            "orderbook_imbalance_5": imbalance,
        }
    )
    strategy = HybridStrategy(
        config.strategy.model_copy(
            update={"cross_window_bars": window, "confluence_enabled": confluence}
        )
    )
    signal = strategy.evaluate(snapshot)
    explanation = strategy.explain(snapshot)
    assert (explanation.action, explanation.strategy) == (signal.action, signal.strategy)


def test_cross_window_allows_entry_a_few_bars_after_the_cross(config, feature) -> None:
    # EMA 9 crossed above EMA 21 two bars ago and is still above.
    later = feature.model_copy(
        update={"previous_ema_9": Decimal("98.8"), "previous_ema_21": Decimal("98.4"),
                "ema_cross_age": 2}
    )
    strict = HybridStrategy(config.strategy.model_copy(update={"cross_window_bars": 1}))
    wide = HybridStrategy(config.strategy.model_copy(update={"cross_window_bars": 3}))
    assert strict.evaluate(later).action == SignalAction.SKIP
    assert wide.evaluate(later).action == SignalAction.ENTER_LONG
    below = later.model_copy(update={"ema_9": Decimal("98"), "ema_cross_age": None})
    assert wide.evaluate(below).action == SignalAction.SKIP


def test_ema_cross_age_counts_bars_since_upward_cross() -> None:
    import pandas as pd

    from wallex_engine.features.indicators import ema_cross_age

    ema_9 = pd.Series([1.0, 1.0, 3.0, 3.0, 3.0, 1.0, 3.0])
    ema_21 = pd.Series([2.0] * 7)
    assert list(ema_cross_age(ema_9, ema_21)) == [None, None, 0, 1, 2, None, 0]
    always_above = ema_cross_age(pd.Series([3.0, 3.0]), pd.Series([2.0, 2.0]))
    assert all(age is not None and age >= 10_000 for age in always_above)


def test_indicator_frame_keeps_bars_where_ema_9_is_below(feature) -> None:
    from datetime import UTC, datetime, timedelta

    from wallex_engine.features.indicators import indicator_frame
    from wallex_engine.models import Candle

    start = datetime(2026, 10, 1, tzinfo=UTC)
    closes = [100 + (i % 40 if i % 80 < 40 else 40 - i % 40) for i in range(400)]

    def candle(i: int, minutes: int, close: float) -> Candle:
        return Candle(symbol="BTCTMN", timeframe_minutes=minutes,
                      opened_at=start + timedelta(minutes=minutes * i),
                      open=Decimal(str(close)), high=Decimal(str(close + 1)),
                      low=Decimal(str(close - 1)), close=Decimal(str(close)),
                      volume=Decimal("1") + i % 3, completed=True)

    fifteen = [candle(i, 15, c) for i, c in enumerate(closes)]
    hourly = [candle(i, 60, c) for i, c in enumerate(closes[:110])]
    frame = indicator_frame(fifteen, hourly)
    below = frame[frame["ema_9"] <= frame["ema_21"]]
    assert len(below) > 20
    assert below["ema_cross_age"].isna().all()


def test_votes_give_each_indicator_a_direction(config, feature) -> None:
    strategy = HybridStrategy(config.strategy)
    votes = {v.key: v.direction for v in strategy.votes(feature)}
    assert set(votes) == {"ema", "hourly", "vwap", "rsi", "bollinger", "orderbook"}
    assert votes["ema"] == "up" and votes["hourly"] == "up" and votes["vwap"] == "up"
    bearish = feature.model_copy(
        update={"ema_9": Decimal("97"), "hourly_ema_20": Decimal("94"), "close": Decimal("93"),
                "rsi_14": Decimal("35"), "orderbook_imbalance_5": Decimal("-0.4")}
    )
    assert all(v.direction == "down" for v in strategy.votes(bearish))


def test_confluence_enters_when_all_bullish_and_exits_when_all_bearish(config, feature) -> None:
    from wallex_engine.models import Position

    aligned = feature.model_copy(
        update={"previous_ema_9": Decimal("99"), "previous_ema_21": Decimal("98.4"),
                "ema_cross_age": 4, "rsi_14": Decimal("60")}
    )
    off = HybridStrategy(config.strategy)
    on = HybridStrategy(config.strategy.model_copy(update={"confluence_enabled": True}))
    assert off.evaluate(aligned).action == SignalAction.SKIP
    entry = on.evaluate(aligned)
    assert entry.action == SignalAction.ENTER_LONG and entry.strategy.value == "confluence"
    # One neutral vote (a balanced order book) still counts as aligned; one bearish does not.
    neutral_book = aligned.model_copy(update={"orderbook_imbalance_5": Decimal("0")})
    assert on.evaluate(neutral_book).action == SignalAction.ENTER_LONG
    bearish_book = aligned.model_copy(update={"orderbook_imbalance_5": Decimal("-0.3")})
    assert on.evaluate(bearish_book).action == SignalAction.SKIP
    bearish = aligned.model_copy(
        update={"ema_9": Decimal("97"), "hourly_ema_20": Decimal("94"), "close": Decimal("93"),
                "rsi_14": Decimal("35"), "orderbook_imbalance_5": Decimal("-0.4")}
    )
    position = Position(symbol="BTCTMN", quantity=Decimal("1"), average_entry=Decimal("99"),
                        stop_price=Decimal("80"), target_price=Decimal("130"))
    assert on.evaluate_exit(bearish, position).action == SignalAction.EXIT
    assert off.evaluate_exit(bearish, position).action == SignalAction.SKIP
