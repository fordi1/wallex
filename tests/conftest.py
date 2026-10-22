from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wallex_engine.config import AppConfig  # noqa: E402
from wallex_engine.models import (  # noqa: E402
    FeatureSnapshot,
    MarketSpec,
    RegimeDecision,
    ReviewDecision,
    SignalAction,
    StrategyFamily,
    TradeSignal,
)


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "challenge": {
                "allowed_markets": ["BTCTMN"],
                "starts_at": "2026-10-20T00:00:00+03:30",
                "ends_at": "2026-10-30T00:00:00+03:30",
            },
            "ai": {"enabled": False, "provider": "deterministic"},
        }
    )


@pytest.fixture
def market() -> MarketSpec:
    return MarketSpec(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        active=True,
        tick_size=Decimal("1000"),
        step_size=Decimal("0.000001"),
        min_quantity=Decimal("0.000001"),
        min_notional=Decimal("50000"),
    )


@pytest.fixture
def signal() -> TradeSignal:
    return TradeSignal(
        decision_id="d1",
        symbol="BTCTMN",
        created_at=datetime(2026, 10, 22, tzinfo=UTC),
        action=SignalAction.ENTER_LONG,
        strategy=StrategyFamily.TREND,
        entry_price=Decimal("1000000"),
        stop_price=Decimal("980000"),
        target_price=Decimal("1040000"),
        expected_move_bps=Decimal("400"),
        reason_fa="آزمون",
    )


@pytest.fixture
def regime() -> RegimeDecision:
    return RegimeDecision(
        regime="trend",
        allow_entries=True,
        size_multiplier=Decimal("1"),
        confidence=Decimal("1"),
        reason_fa="آزمون",
    )


@pytest.fixture
def review() -> ReviewDecision:
    return ReviewDecision(approve=True, size_adjustment=Decimal("1"), reason_fa="آزمون")


@pytest.fixture
def feature() -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="BTCTMN",
        observed_at=datetime(2026, 10, 22, tzinfo=UTC),
        close=Decimal("100"),
        previous_close=Decimal("98"),
        ema_9=Decimal("99"),
        ema_21=Decimal("98.5"),
        previous_ema_9=Decimal("97"),
        previous_ema_21=Decimal("97.5"),
        hourly_ema_20=Decimal("98"),
        hourly_ema_50=Decimal("95"),
        rsi_14=Decimal("55"),
        previous_rsi_14=Decimal("50"),
        atr_14=Decimal("2"),
        bollinger_mid=Decimal("98"),
        bollinger_lower=Decimal("90"),
        bollinger_upper=Decimal("106"),
        previous_bollinger_lower=Decimal("90"),
        vwap=Decimal("97"),
        volume_zscore_20=Decimal("1"),
        spread_bps=Decimal("5"),
        orderbook_imbalance_5=Decimal("0.2"),
        fee_rate=Decimal("0.001"),
        modeled_slippage_bps=Decimal("3"),
    )
