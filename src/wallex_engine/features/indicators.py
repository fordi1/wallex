from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from ..data.market_data import candles_to_frame
from ..models import Candle, FeatureSnapshot, OrderBookSnapshot

UNKNOWN_CROSS_AGE = 10_000  # EMA 9 above EMA 21 for the whole window: the cross was not seen


def ema_cross_age(ema_9: pd.Series, ema_21: pd.Series) -> list[int | None]:
    """Bars since EMA 9 last crossed above EMA 21; None while it is not above."""
    above = (ema_9 > ema_21).to_numpy()
    ages: list[int | None] = []
    age: int | None = None
    for index, is_above in enumerate(above):
        if not is_above:
            age = None
        elif index > 0 and not above[index - 1]:
            age = 0
        elif age is None:
            age = UNKNOWN_CROSS_AGE
        else:
            age += 1
        ages.append(age)
    return ages


def indicator_frame(candles_5m: list[Candle], candles_1h: list[Candle]) -> pd.DataFrame:
    five = candles_to_frame([c for c in candles_5m if c.completed]).copy()
    hourly = candles_to_frame([c for c in candles_1h if c.completed]).copy()
    for column in ("open", "high", "low", "close", "volume"):
        five[column] = five[column].astype(float)
        hourly[column] = hourly[column].astype(float)
    five["ema_9"] = five["close"].ewm(span=9, adjust=False).mean()
    five["ema_21"] = five["close"].ewm(span=21, adjust=False).mean()
    five["ema_cross_age"] = pd.Series(
        ema_cross_age(five["ema_9"], five["ema_21"]), index=five.index, dtype=object
    )
    delta = five["close"].diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gains / losses.replace(0, np.nan)
    five["rsi_14"] = 100 - (100 / (1 + rs))
    true_range = pd.concat(
        [
            five["high"] - five["low"],
            (five["high"] - five["close"].shift()).abs(),
            (five["low"] - five["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    five["atr_14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    five["bollinger_mid"] = five["close"].rolling(20).mean()
    std = five["close"].rolling(20).std(ddof=0)
    five["bollinger_upper"] = five["bollinger_mid"] + 2 * std
    five["bollinger_lower"] = five["bollinger_mid"] - 2 * std
    typical = (five["high"] + five["low"] + five["close"]) / 3
    session = five.index.tz_convert("Asia/Tehran").date
    cumulative_pv = (typical * five["volume"]).groupby(session).cumsum()
    cumulative_volume = five["volume"].groupby(session).cumsum().replace(0, np.nan)
    five["vwap"] = cumulative_pv / cumulative_volume
    volume_mean = five["volume"].rolling(20).mean()
    volume_std = five["volume"].rolling(20).std(ddof=0).replace(0, np.nan)
    five["volume_zscore_20"] = (five["volume"] - volume_mean) / volume_std
    hourly["hourly_ema_20"] = hourly["close"].ewm(span=20, adjust=False).mean()
    hourly["hourly_ema_50"] = hourly["close"].ewm(span=50, adjust=False).mean()
    aligned = hourly[["hourly_ema_20", "hourly_ema_50"]].reindex(five.index, method="ffill")
    frame = five.join(aligned)
    # ema_cross_age is legitimately empty while EMA 9 is below EMA 21.
    return frame.dropna(subset=[c for c in frame.columns if c != "ema_cross_age"])


def feature_snapshots(
    candles_5m: list[Candle],
    candles_1h: list[Candle],
    books: dict[object, OrderBookSnapshot] | OrderBookSnapshot,
    fee_rate: Decimal,
    modeled_slippage_bps: Decimal,
) -> list[FeatureSnapshot]:
    frame = indicator_frame(candles_5m, candles_1h)
    snapshots: list[FeatureSnapshot] = []
    if len(frame) < 2:
        return snapshots
    symbol = candles_5m[0].symbol
    for idx in range(1, len(frame)):
        current = frame.iloc[idx]
        previous = frame.iloc[idx - 1]
        timestamp = frame.index[idx].to_pydatetime()
        book = books if isinstance(books, OrderBookSnapshot) else _book_at(books, timestamp)
        if book is None:
            continue
        midpoint = (book.best_ask + book.best_bid) / 2
        spread_bps = (book.best_ask - book.best_bid) / midpoint * Decimal("10000")
        bid_qty = sum(level.quantity for level in book.bids[:5])
        ask_qty = sum(level.quantity for level in book.asks[:5])
        imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty) if bid_qty + ask_qty else Decimal(0)
        snapshots.append(
            FeatureSnapshot(
                symbol=symbol,
                observed_at=timestamp,
                close=_d(current["close"]),
                previous_close=_d(previous["close"]),
                ema_9=_d(current["ema_9"]),
                ema_21=_d(current["ema_21"]),
                previous_ema_9=_d(previous["ema_9"]),
                previous_ema_21=_d(previous["ema_21"]),
                hourly_ema_20=_d(current["hourly_ema_20"]),
                hourly_ema_50=_d(current["hourly_ema_50"]),
                rsi_14=_d(current["rsi_14"]),
                previous_rsi_14=_d(previous["rsi_14"]),
                atr_14=_d(current["atr_14"]),
                bollinger_mid=_d(current["bollinger_mid"]),
                bollinger_lower=_d(current["bollinger_lower"]),
                bollinger_upper=_d(current["bollinger_upper"]),
                previous_bollinger_lower=_d(previous["bollinger_lower"]),
                vwap=_d(current["vwap"]),
                volume_zscore_20=_d(current["volume_zscore_20"]),
                spread_bps=spread_bps,
                orderbook_imbalance_5=imbalance,
                fee_rate=fee_rate,
                modeled_slippage_bps=modeled_slippage_bps,
                ema_cross_age=current["ema_cross_age"],
            )
        )
    return snapshots


def _book_at(books: dict[object, OrderBookSnapshot], timestamp: object) -> OrderBookSnapshot | None:
    candidates = [book for key, book in books.items() if key <= timestamp]
    return max(candidates, key=lambda book: book.captured_at) if candidates else None


def _d(value: object) -> Decimal:
    return Decimal(str(round(float(value), 12)))
