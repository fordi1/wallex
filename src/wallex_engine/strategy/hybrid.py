from __future__ import annotations

import hashlib
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Check:
    family: str  # "common", "trend" or "mean_reversion"
    key: str
    label_fa: str
    passed: bool
    value: float | None = None
    threshold: float | None = None
    unit: str = ""


@dataclass(frozen=True)
class Explanation:
    checks: tuple[Check, ...]
    action: SignalAction
    strategy: StrategyFamily

    def failed(self, family: str) -> int:
        return sum(1 for check in self.checks if check.family == family and not check.passed)


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

    def explain(self, snapshot: FeatureSnapshot) -> Explanation:
        """Every entry condition with its measured value; mirrors ``evaluate`` exactly."""
        s, cfg = snapshot, self.config
        stop = s.close - cfg.stop_atr * s.atr_14
        trend_target = s.close + cfg.trend_target_r * (s.close - stop)
        trend_bps = (trend_target - s.close) / s.close * Decimal("10000")
        mr_bps = max(Decimal("0"), (s.bollinger_mid - s.close) / s.close * Decimal("10000"))
        cost_bps = self._required_edge_bps(s)
        spread_ok = s.spread_bps <= cfg.max_spread_bps
        checks = (
            Check("common", "spread", "اسپرد کمتر از سقف", spread_ok,
                  float(s.spread_bps), float(cfg.max_spread_bps), "bps"),
            Check("trend", "ema_cross", "تقاطع صعودی EMA ۹ از EMA ۲۱",
                  s.previous_ema_9 <= s.previous_ema_21 and s.ema_9 > s.ema_21,
                  float(s.ema_9 - s.ema_21), 0.0),
            Check("trend", "hourly_trend", "روند ساعتی صعودی (EMA ۲۰ > EMA ۵۰)",
                  s.hourly_ema_20 > s.hourly_ema_50,
                  float(s.hourly_ema_20), float(s.hourly_ema_50)),
            Check("trend", "above_hourly", "قیمت بالای EMA ۵۰ ساعتی",
                  s.close > s.hourly_ema_50, float(s.close), float(s.hourly_ema_50)),
            Check("trend", "volume", "تأیید حجم (z-score)",
                  s.volume_zscore_20 > cfg.volume_z_min,
                  float(s.volume_zscore_20), float(cfg.volume_z_min)),
            Check("trend", "edge_trend", "حرکت مورد انتظار بیش از هزینه × ضریب اطمینان",
                  trend_bps > cost_bps, float(trend_bps), float(cost_bps), "bps"),
            Check("mean_reversion", "ranging", "بازار خنثی (فاصله EMAهای ساعتی < ۱٪)",
                  abs(s.hourly_ema_20 - s.hourly_ema_50) / s.close < Decimal("0.01"),
                  float(abs(s.hourly_ema_20 - s.hourly_ema_50) / s.close * 100), 1.0, "%"),
            Check("mean_reversion", "bollinger_reentry", "بازگشت به داخل باند پایین بولینگر",
                  s.previous_close < s.previous_bollinger_lower
                  and s.close >= s.bollinger_lower,
                  float(s.close), float(s.bollinger_lower)),
            Check("mean_reversion", "rsi_oversold", "RSI قبلی در اشباع فروش (≤ ۳۰)",
                  s.previous_rsi_14 <= 30, float(s.previous_rsi_14), 30.0),
            Check("mean_reversion", "volatility", "نوسان کنترل‌شده (ATR/قیمت ≤ ۳٪)",
                  not s.atr_14 / s.close > Decimal("0.03"),
                  float(s.atr_14 / s.close * 100), 3.0, "%"),
            Check("mean_reversion", "edge_mean_reversion",
                  "فاصله تا میانگین بولینگر بیش از هزینه × ضریب اطمینان",
                  s.bollinger_mid > s.close and mr_bps > cost_bps,
                  float(mr_bps), float(cost_bps), "bps"),
        )
        families = {c.family for c in checks if c.family != "common"}
        passed = {f: all(c.passed for c in checks if c.family == f) for f in families}
        if not spread_ok:
            verdict = (SignalAction.SKIP, StrategyFamily.NONE)
        elif passed["trend"]:
            verdict = (SignalAction.ENTER_LONG, StrategyFamily.TREND)
        elif passed["mean_reversion"]:
            verdict = (SignalAction.ENTER_LONG, StrategyFamily.MEAN_REVERSION)
        else:
            verdict = (SignalAction.SKIP, StrategyFamily.NONE)
        return Explanation(checks, *verdict)

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
        return expected_move_bps > self._required_edge_bps(snapshot)

    def _required_edge_bps(self, snapshot: FeatureSnapshot) -> Decimal:
        round_trip_fees_bps = snapshot.fee_rate * Decimal("2") * Decimal("10000")
        modeled_cost = (
            round_trip_fees_bps + snapshot.modeled_slippage_bps * Decimal("2") + snapshot.spread_bps
        )
        return modeled_cost * self.config.safety_factor

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
