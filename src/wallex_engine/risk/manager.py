from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from ..config import ExecutionSection, RiskSection
from ..models import (
    MarketSpec,
    Position,
    RegimeDecision,
    ReviewDecision,
    RiskDecision,
    SignalAction,
    TradeSignal,
)


@dataclass
class RiskState:
    peak_equity: Decimal
    day_start_equity: Decimal
    trading_day: object
    kill_switch_active: bool = False
    entries_paused_for_day: bool = False
    size_multiplier: Decimal = Decimal("1")
    ai_entries_paused: bool = False
    last_exit_at: dict[str, datetime] = field(default_factory=dict)
    last_loss_at: dict[str, datetime] = field(default_factory=dict)


class PortfolioRiskManager:
    def __init__(
        self,
        config: RiskSection,
        execution: ExecutionSection,
        initial_equity: Decimal,
        timezone: str = "Asia/Tehran",
    ) -> None:
        self.config = config
        self.execution = execution
        self.timezone = ZoneInfo(timezone)
        now = datetime.now(tz=self.timezone)
        self.state = RiskState(initial_equity, initial_equity, now.date())

    def update_equity(self, equity: Decimal, now: datetime) -> tuple[str, ...]:
        local = now.astimezone(self.timezone)
        transitions: list[str] = []
        if local.date() != self.state.trading_day:
            self.state.trading_day = local.date()
            self.state.day_start_equity = equity
            self.state.entries_paused_for_day = False
            transitions.append("daily_reset")
        self.state.peak_equity = max(self.state.peak_equity, equity)
        daily_loss = (self.state.day_start_equity - equity) / self.state.day_start_equity
        drawdown = (self.state.peak_equity - equity) / self.state.peak_equity
        if daily_loss >= self.config.daily_loss_stop:
            self.state.entries_paused_for_day = True
            transitions.append("daily_loss_stop")
        if drawdown >= self.config.kill_switch_drawdown:
            self.state.kill_switch_active = True
            transitions.append("kill_switch")
        elif drawdown >= self.config.halve_size_drawdown:
            self.state.size_multiplier = Decimal("0.5")
            transitions.append("half_size")
        else:
            self.state.size_multiplier = Decimal("1")
        return tuple(dict.fromkeys(transitions))

    def assess_entry(
        self,
        signal: TradeSignal,
        market: MarketSpec,
        equity: Decimal,
        cash: Decimal,
        positions: list[Position] | tuple[Position, ...],
        regime: RegimeDecision,
        review: ReviewDecision,
        now: datetime,
    ) -> RiskDecision:
        reasons: list[str] = []
        if signal.action != SignalAction.ENTER_LONG:
            reasons.append("سیگنال ورود صادر نشده است.")
        if self.state.kill_switch_active:
            reasons.append("توقف اضطراری فعال است.")
        if self.state.entries_paused_for_day:
            reasons.append("سقف زیان روزانه فعال است.")
        if self.state.ai_entries_paused:
            reasons.append("ورودها تا بازیابی بررسی هوش مصنوعی متوقف است.")
        if not regime.allow_entries or not review.approve:
            reasons.append("بررسی رژیم یا سیگنال، ورود را رد کرد.")
        existing = next(
            (position for position in positions if position.symbol == signal.symbol), None
        )
        if existing and existing.quantity > 0:
            reasons.append("برای این بازار موقعیت باز وجود دارد.")
        max_positions = (
            self.execution.canary_max_positions
            if self.execution.canary
            else self.config.max_positions
        )
        if sum(position.quantity > 0 for position in positions) >= max_positions:
            reasons.append("سقف تعداد موقعیت‌های هم‌زمان پر شده است.")
        last_loss = self.state.last_loss_at.get(signal.symbol)
        last_exit = self.state.last_exit_at.get(signal.symbol)
        if last_loss and now < last_loss + timedelta(minutes=self.config.loss_cooldown_minutes):
            reasons.append("دوره توقف پس از زیان هنوز پایان نیافته است.")
        elif last_exit and now < last_exit + timedelta(minutes=self.config.cooldown_minutes):
            reasons.append("دوره توقف عادی هنوز پایان نیافته است.")
        if signal.entry_price is None or signal.stop_price is None:
            reasons.append("قیمت ورود یا حد ضرر مشخص نیست.")
        if reasons:
            return RiskDecision(
                approved=False,
                quantity=Decimal(0),
                notional_tmn=Decimal(0),
                risk_tmn=Decimal(0),
                reason_fa=" ".join(reasons),
                constraints=tuple(reasons),
            )

        entry = signal.entry_price
        stop_distance = entry - signal.stop_price
        if stop_distance <= 0:
            return self._reject("فاصله حد ضرر معتبر نیست.")
        risk_fraction = (
            self.execution.canary_risk_per_trade
            if self.execution.canary
            else self.config.risk_per_trade
        )
        symbol_cap = (
            self.execution.canary_symbol_exposure
            if self.execution.canary
            else self.config.max_symbol_exposure
        )
        total_cap = (
            self.execution.canary_total_exposure
            if self.execution.canary
            else self.config.max_total_exposure
        )
        risk_budget = equity * risk_fraction * self.state.size_multiplier
        raw_quantity = risk_budget / stop_distance
        current_exposure = sum(position.quantity * position.average_entry for position in positions)
        symbol_notional_limit = equity * symbol_cap
        total_notional_available = max(Decimal(0), equity * total_cap - current_exposure)
        reserve_available = max(Decimal(0), cash - equity * self.config.min_tmn_reserve)
        notional_cap = min(symbol_notional_limit, total_notional_available, reserve_available)
        raw_quantity = min(raw_quantity, notional_cap / entry)
        multiplier = min(Decimal("1"), regime.size_multiplier, review.size_adjustment)
        quantity = quantize_down(raw_quantity * multiplier, market.step_size)
        notional = quantity * entry
        actual_risk = quantity * stop_distance
        if quantity < market.min_quantity or notional < market.min_notional:
            return self._reject("اندازه سفارش از حداقل معتبر بازار کمتر است.")
        return RiskDecision(
            approved=True,
            quantity=quantity,
            notional_tmn=notional,
            risk_tmn=actual_risk,
            reason_fa="اندازه سفارش در همه سقف‌های ریسک قرار دارد.",
            constraints=("risk", "symbol_exposure", "total_exposure", "tmn_reserve"),
        )

    def record_exit(self, symbol: str, pnl_tmn: Decimal, at: datetime) -> None:
        self.state.last_exit_at[symbol] = at
        if pnl_tmn < 0:
            self.state.last_loss_at[symbol] = at

    @staticmethod
    def _reject(reason: str) -> RiskDecision:
        return RiskDecision(
            approved=False,
            quantity=Decimal(0),
            notional_tmn=Decimal(0),
            risk_tmn=Decimal(0),
            reason_fa=reason,
            constraints=(reason,),
        )


def quantize_down(value: Decimal, quantum: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    units = (value / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum
