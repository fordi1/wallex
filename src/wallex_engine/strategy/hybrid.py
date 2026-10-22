from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from ..config import StrategySection
from ..models import (
    FeatureSnapshot,
    Position,
    SignalAction,
    StrategyFamily,
    TradeSignal,
)


class HybridStrategy:
    def __init__(self, config: StrategySection) -> None:
        self.config = config

    def evaluate(self, snapshot: FeatureSnapshot) -> TradeSignal:
        decision_id = decision_identifier(snapshot.symbol, snapshot.observed_at)
        if snapshot.spread_bps > self.config.max_spread_bps:
            return self._skip(decision_id, snapshot, "اسپرد بازار بیش از حد مجاز است.")

        crossed_up = (
            snapshot.previous_ema_9 <= snapshot.previous_ema_21 and snapshot.ema_9 > snapshot.ema_21
        )
        hourly_up = (
            snapshot.hourly_ema_20 > snapshot.hourly_ema_50
            and snapshot.close > snapshot.hourly_ema_50
        )
        volume_confirmed = snapshot.volume_zscore_20 > self.config.volume_z_min
        if crossed_up and hourly_up and volume_confirmed:
            stop = snapshot.close - self.config.stop_atr * snapshot.atr_14
            target = snapshot.close + self.config.trend_target_r * (snapshot.close - stop)
            expected_bps = (target - snapshot.close) / snapshot.close * Decimal("10000")
            if self._edge_is_sufficient(snapshot, expected_bps):
                return TradeSignal(
                    decision_id=decision_id,
                    symbol=snapshot.symbol,
                    created_at=snapshot.observed_at,
                    action=SignalAction.ENTER_LONG,
                    strategy=StrategyFamily.TREND,
                    entry_price=snapshot.close,
                    stop_price=stop,
                    target_price=target,
                    expected_move_bps=expected_bps,
                    reason_fa="تقاطع صعودی، روند ساعتی و حجم هم‌جهت تأیید شدند.",
                )

        ranging = abs(snapshot.hourly_ema_20 - snapshot.hourly_ema_50) / snapshot.close < Decimal(
            "0.01"
        )
        reentered = (
            snapshot.previous_close < snapshot.previous_bollinger_lower
            and snapshot.close >= snapshot.bollinger_lower
        )
        high_volatility = snapshot.atr_14 / snapshot.close > Decimal("0.03")
        if ranging and reentered and snapshot.previous_rsi_14 <= 30 and not high_volatility:
            stop = snapshot.close - self.config.stop_atr * snapshot.atr_14
            target = snapshot.bollinger_mid
            expected_bps = max(
                Decimal("0"), (target - snapshot.close) / snapshot.close * Decimal("10000")
            )
            if target > snapshot.close and self._edge_is_sufficient(snapshot, expected_bps):
                return TradeSignal(
                    decision_id=decision_id,
                    symbol=snapshot.symbol,
                    created_at=snapshot.observed_at,
                    action=SignalAction.ENTER_LONG,
                    strategy=StrategyFamily.MEAN_REVERSION,
                    entry_price=snapshot.close,
                    stop_price=stop,
                    target_price=target,
                    expected_move_bps=expected_bps,
                    reason_fa="بازگشت قیمت به باند بولینگر در بازار خنثی تأیید شد.",
                )
        return self._skip(decision_id, snapshot, "شرایط ورود کامل نیست.")

    def evaluate_exit(self, snapshot: FeatureSnapshot, position: Position) -> TradeSignal:
        decision_id = decision_identifier(snapshot.symbol, snapshot.observed_at, "exit")
        if position.quantity <= 0:
            return self._skip(decision_id, snapshot, "موقعیت بازی وجود ندارد.")
        if position.stop_price is not None and snapshot.close <= position.stop_price:
            reason = "حد ضرر فعال شد."
        elif position.trailing_stop is not None and snapshot.close <= position.trailing_stop:
            reason = "حد ضرر دنبال‌کننده فعال شد."
        elif position.target_price is not None and snapshot.close >= position.target_price:
            reason = "هدف سود محقق شد."
        elif position.bars_held >= self.config.time_stop_bars:
            reason = "مهلت نگهداری موقعیت پایان یافت."
        else:
            return self._skip(decision_id, snapshot, "شرط خروج فعال نیست.")
        return TradeSignal(
            decision_id=decision_id,
            symbol=snapshot.symbol,
            created_at=snapshot.observed_at,
            action=SignalAction.EXIT,
            strategy=StrategyFamily.NONE,
            entry_price=snapshot.close,
            reason_fa=reason,
        )

    def _edge_is_sufficient(self, snapshot: FeatureSnapshot, expected_move_bps: Decimal) -> bool:
        round_trip_fees_bps = snapshot.fee_rate * Decimal("2") * Decimal("10000")
        modeled_cost = (
            round_trip_fees_bps + snapshot.modeled_slippage_bps * Decimal("2") + snapshot.spread_bps
        )
        return expected_move_bps > modeled_cost * self.config.safety_factor

    @staticmethod
    def _skip(decision_id: str, snapshot: FeatureSnapshot, reason: str) -> TradeSignal:
        return TradeSignal(
            decision_id=decision_id,
            symbol=snapshot.symbol,
            created_at=snapshot.observed_at,
            action=SignalAction.SKIP,
            reason_fa=reason,
        )


def decision_identifier(symbol: str, observed_at: datetime, suffix: str = "entry") -> str:
    source = f"{symbol}|{observed_at.isoformat()}|{suffix}".encode()
    return hashlib.sha256(source).hexdigest()[:24]
