from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .models import (
    Balance,
    Candle,
    DecisionRecord,
    EquitySnapshot,
    FeatureSnapshot,
    FeeSchedule,
    Fill,
    MarketSpec,
    OrderBookSnapshot,
    OrderIntent,
    OrderRecord,
    Position,
    RegimeDecision,
    ReviewDecision,
    RiskDecision,
    TradeSignal,
)


class ExchangeClient(Protocol):
    async def get_markets(self) -> dict[str, MarketSpec]: ...
    async def get_candles(
        self, symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> list[Candle]: ...
    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot: ...
    async def get_balances(self) -> list[Balance]: ...
    async def get_fees(self, symbol: str | None = None) -> FeeSchedule: ...
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderRecord]: ...
    async def get_order(
        self, *, order_id: str | None = None, client_id: str | None = None
    ) -> OrderRecord | None: ...
    async def get_fills(self, symbol: str | None = None) -> list[Fill]: ...
    async def create_order(self, intent: OrderIntent) -> OrderRecord: ...
    async def cancel_order(
        self, *, order_id: str | None = None, client_id: str | None = None
    ) -> OrderRecord: ...
    async def stream_books(self, symbols: Sequence[str]) -> AsyncIterator[OrderBookSnapshot]: ...


class Broker(Protocol):
    async def submit(self, intent: OrderIntent) -> OrderRecord: ...
    async def cancel(self, order: OrderRecord) -> OrderRecord: ...
    async def reconcile(self) -> bool: ...


class MarketDataSource(Protocol):
    async def history(
        self, symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> list[Candle]: ...
    async def latest_book(self, symbol: str) -> OrderBookSnapshot: ...


class AIProvider(Protocol):
    async def review_regime(self, snapshot: FeatureSnapshot) -> RegimeDecision: ...
    async def review_signal(
        self, snapshot: FeatureSnapshot, signal: TradeSignal
    ) -> ReviewDecision: ...


class Strategy(Protocol):
    def evaluate(self, snapshot: FeatureSnapshot) -> TradeSignal: ...


class RiskManager(Protocol):
    def assess_entry(
        self,
        signal: TradeSignal,
        market: MarketSpec,
        equity: Decimal,
        cash: Decimal,
        positions: Sequence[Position],
        regime: RegimeDecision,
        review: ReviewDecision,
        now: datetime,
    ) -> RiskDecision: ...


class EventStore(Protocol):
    async def append_decision(self, record: DecisionRecord) -> None: ...
    async def append_fill(self, fill: Fill) -> None: ...
    async def append_equity(self, snapshot: EquitySnapshot) -> None: ...
    async def upsert_position(self, position: Position) -> None: ...
    async def delete_position(self, symbol: str) -> None: ...
    async def load_positions(self) -> list[Position]: ...
    async def persist_intent(self, intent: OrderIntent) -> None: ...
    async def persist_order(self, order: OrderRecord) -> None: ...
    async def append_ai_call(
        self,
        provider: str,
        model: str,
        cache_key: str,
        cache_hit: bool,
        fallback: bool,
        payload: dict[str, object],
    ) -> None: ...
    async def set_kill_switch(self, active: bool, reason: str, at: datetime) -> None: ...
