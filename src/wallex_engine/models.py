from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    EXIT = "EXIT"
    HOLD = "HOLD"
    SKIP = "SKIP"


class StrategyFamily(StrEnum):
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    NONE = "none"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class MarketSpec(StrictModel):
    symbol: str
    base_asset: str
    quote_asset: str
    active: bool
    tick_size: Decimal = Field(gt=0)
    step_size: Decimal = Field(gt=0)
    min_quantity: Decimal = Field(gt=0)
    min_notional: Decimal = Field(gt=0)


class Candle(StrictModel):
    symbol: str
    timeframe_minutes: int = Field(gt=0)
    opened_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    completed: bool = True

    @field_validator("opened_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("candle timestamps must be timezone-aware")
        return value


class BookLevel(StrictModel):
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(ge=0)


class OrderBookSnapshot(StrictModel):
    symbol: str
    captured_at: datetime
    sequence: int | None = None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price


class FeatureSnapshot(StrictModel):
    symbol: str
    observed_at: datetime
    close: Decimal
    previous_close: Decimal
    ema_9: Decimal
    ema_21: Decimal
    previous_ema_9: Decimal
    previous_ema_21: Decimal
    hourly_ema_20: Decimal
    hourly_ema_50: Decimal
    rsi_14: Decimal
    previous_rsi_14: Decimal
    atr_14: Decimal
    bollinger_mid: Decimal
    bollinger_lower: Decimal
    bollinger_upper: Decimal
    previous_bollinger_lower: Decimal
    vwap: Decimal
    volume_zscore_20: Decimal
    spread_bps: Decimal
    orderbook_imbalance_5: Decimal
    fee_rate: Decimal
    modeled_slippage_bps: Decimal
    # Bars since EMA 9 crossed above EMA 21 (0 = this bar); None while EMA 9 <= EMA 21
    # or in decisions logged before this feature existed.
    ema_cross_age: int | None = None


class TradeSignal(StrictModel):
    decision_id: str
    symbol: str
    created_at: datetime
    action: SignalAction
    strategy: StrategyFamily = StrategyFamily.NONE
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    expected_move_bps: Decimal = Decimal("0")
    reason_fa: str


class RegimeDecision(StrictModel):
    regime: str
    allow_entries: bool
    size_multiplier: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    reason_fa: str


class ReviewDecision(StrictModel):
    approve: bool
    size_adjustment: Decimal = Field(ge=0, le=1)
    reason_fa: str


class RiskDecision(StrictModel):
    approved: bool
    quantity: Decimal = Field(ge=0)
    notional_tmn: Decimal = Field(ge=0)
    risk_tmn: Decimal = Field(ge=0)
    reason_fa: str
    constraints: tuple[str, ...] = ()


class OrderIntent(StrictModel):
    decision_id: str
    client_id: str
    attempt: int = Field(ge=0)
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    created_at: datetime


class OrderRecord(StrictModel):
    client_id: str
    exchange_order_id: str | None = None
    status: OrderStatus
    symbol: str
    side: Side
    requested_quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    raw_status: str | None = None
    updated_at: datetime


class Fill(StrictModel):
    fill_id: str
    client_id: str
    symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee_quantity: Decimal = Field(ge=0)
    fee_asset: str
    fee_tmn: Decimal = Field(ge=0)
    filled_at: datetime


class Position(StrictModel):
    symbol: str
    quantity: Decimal = Field(ge=0)
    average_entry: Decimal = Field(ge=0)
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    trailing_stop: Decimal | None = None
    opened_at: datetime | None = None
    bars_held: int = Field(default=0, ge=0)
    realized_pnl_tmn: Decimal = Decimal("0")


class EquitySnapshot(StrictModel):
    run_id: str | None = None
    captured_at: datetime
    cash_tmn: Decimal
    positions_tmn: Decimal
    equity_tmn: Decimal
    peak_equity_tmn: Decimal
    drawdown: Decimal
    daily_pnl: Decimal


class DecisionRecord(StrictModel):
    decision_id: str
    run_id: str
    created_at: datetime
    mode: str
    symbol: str
    features: FeatureSnapshot | None
    signal: TradeSignal
    regime: RegimeDecision | None
    review: ReviewDecision | None
    risk: RiskDecision | None
    outcome: str
    reason_fa: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeeSchedule(StrictModel):
    maker: Decimal = Field(ge=0)
    taker: Decimal = Field(ge=0)


# Wallex /v1/account/fee for BTCTMN and ETHTMN, read on 2026-10-22.
WALLEX_FEES = FeeSchedule(maker=Decimal("0.0025"), taker=Decimal("0.003"))


class Balance(StrictModel):
    asset: str
    available: Decimal = Field(ge=0)
    locked: Decimal = Field(ge=0)


class FlattenPlan(StrictModel):
    stage: Literal["normal", "limit_exit", "market_exit", "ended"]
    stop_new_entries: bool
    cancel_pending_buys: bool
    use_market_exits: bool
    seconds_remaining: int
