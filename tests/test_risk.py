from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from wallex_engine.models import Position
from wallex_engine.risk.manager import PortfolioRiskManager, quantize_down


@given(
    value=st.decimals(min_value="0", max_value="100000", places=8),
    quantum=st.sampled_from([Decimal("1"), Decimal("0.1"), Decimal("0.001")]),
)
def test_quantize_down_never_increases(value: Decimal, quantum: Decimal) -> None:
    result = quantize_down(value, quantum)
    assert result <= value
    assert result % quantum == 0


def test_risk_respects_reserve_and_ai_shrink(config, market, signal, regime, review) -> None:
    manager = PortfolioRiskManager(config.risk, config.execution, Decimal("5000000"))
    smaller_review = review.model_copy(update={"size_adjustment": Decimal("0.25")})
    result = manager.assess_entry(
        signal,
        market,
        Decimal("5000000"),
        Decimal("5000000"),
        [],
        regime,
        smaller_review,
        signal.created_at,
    )
    assert result.approved
    assert result.notional_tmn <= Decimal("250000")  # 5% canary cap
    assert result.notional_tmn <= Decimal("3000000")  # 40% reserve retained


def test_ai_can_never_enlarge_size(config, market, signal, regime, review) -> None:
    manager = PortfolioRiskManager(config.risk, config.execution, Decimal("5000000"))
    baseline = manager.assess_entry(
        signal,
        market,
        Decimal("5000000"),
        Decimal("5000000"),
        [],
        regime,
        review,
        signal.created_at,
    )
    shrunken = manager.assess_entry(
        signal,
        market,
        Decimal("5000000"),
        Decimal("5000000"),
        [],
        regime.model_copy(update={"size_multiplier": Decimal("0.5")}),
        review.model_copy(update={"size_adjustment": Decimal("0.8")}),
        signal.created_at,
    )
    assert shrunken.quantity <= baseline.quantity


def test_drawdown_transitions_persist_kill_switch(config) -> None:
    manager = PortfolioRiskManager(config.risk, config.execution, Decimal("5000000"))
    now = datetime(2026, 10, 22, tzinfo=UTC)
    assert "half_size" in manager.update_equity(Decimal("4700000"), now)
    assert manager.state.size_multiplier == Decimal("0.5")
    assert "kill_switch" in manager.update_equity(Decimal("4500000"), now)
    assert manager.state.kill_switch_active


def test_loss_cooldown_blocks_symbol(config, market, signal, regime, review) -> None:
    manager = PortfolioRiskManager(config.risk, config.execution, Decimal("5000000"))
    manager.record_exit(signal.symbol, Decimal("-1"), signal.created_at)
    result = manager.assess_entry(
        signal,
        market,
        Decimal("5000000"),
        Decimal("5000000"),
        [],
        regime,
        review,
        signal.created_at + timedelta(minutes=30),
    )
    assert not result.approved
    assert "زیان" in result.reason_fa


def test_existing_position_prevents_opposing_entry(config, market, signal, regime, review) -> None:
    manager = PortfolioRiskManager(config.risk, config.execution, Decimal("5000000"))
    position = Position(
        symbol=signal.symbol,
        quantity=Decimal("0.1"),
        average_entry=Decimal("1000000"),
        opened_at=signal.created_at,
    )
    result = manager.assess_entry(
        signal,
        market,
        Decimal("5000000"),
        Decimal("4000000"),
        [position],
        regime,
        review,
        signal.created_at,
    )
    assert not result.approved
