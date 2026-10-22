from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from ..config import AppConfig
from ..data.market_data import candles_to_frame, resample_completed
from ..exchange.wallex_client import WallexClient
from ..features.indicators import feature_snapshots
from ..models import Candle, FeatureSnapshot, MarketSpec, OrderBookSnapshot, TradeSignal
from ..strategy.hybrid import HybridStrategy


@dataclass(frozen=True)
class MarketExplorerView:
    market: MarketSpec
    candles_5m: tuple[Candle, ...]
    candles_1h: tuple[Candle, ...]
    chart_candles: tuple[Candle, ...]
    chart_timeframe_minutes: int
    book: OrderBookSnapshot
    feature: FeatureSnapshot
    signal: TradeSignal
    fetched_at: datetime


@dataclass(frozen=True)
class WatchQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    midpoint: Decimal
    spread_bps: Decimal
    captured_at: datetime


def active_tmn_markets(markets: dict[str, MarketSpec]) -> list[MarketSpec]:
    return sorted(
        (market for market in markets.values() if market.active and market.quote_asset == "TMN"),
        key=lambda market: market.symbol,
    )


def analyze_market_data(
    config: AppConfig,
    market: MarketSpec,
    minute_candles: list[Candle],
    hourly_candles: list[Candle],
    book: OrderBookSnapshot,
    chart_candles: list[Candle] | None = None,
    chart_timeframe_minutes: int = 5,
) -> MarketExplorerView:
    five_minute = resample_completed(minute_candles, 5)
    snapshots = feature_snapshots(
        five_minute,
        hourly_candles,
        book,
        Decimal("0.002"),
        config.execution.modeled_slippage_bps,
    )
    if not snapshots:
        raise ValueError("برای محاسبه شاخص‌ها دادهٔ تکمیل‌شده کافی نیست.")
    feature = snapshots[-1]
    signal = HybridStrategy(config.strategy).evaluate(feature)
    return MarketExplorerView(
        market=market,
        candles_5m=tuple(five_minute),
        candles_1h=tuple(hourly_candles),
        chart_candles=tuple(chart_candles or five_minute),
        chart_timeframe_minutes=chart_timeframe_minutes,
        book=book,
        feature=feature,
        signal=signal,
        fetched_at=datetime.now(tz=UTC),
    )


async def fetch_active_tmn_markets(config: AppConfig) -> list[MarketSpec]:
    async with _public_client(config) as client:
        return active_tmn_markets(await client.get_markets())


async def fetch_market_view(
    config: AppConfig,
    symbol: str,
    timeframe_minutes: int = 5,
    bars: int = 240,
    market: MarketSpec | None = None,
) -> MarketExplorerView:
    if timeframe_minutes not in {1, 5, 15, 60, 240}:
        raise ValueError("بازهٔ زمانی نمودار پشتیبانی نمی‌شود.")
    bars = min(max(bars, 60), 500)
    end = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    chart_start = end - timedelta(minutes=timeframe_minutes * max(bars + 40, 120))
    async with _public_client(config) as client:
        minute_task = client.get_candles(symbol, 1, end - timedelta(hours=24), end)
        hourly_task = client.get_candles(symbol, 60, end - timedelta(days=5), end)
        chart_task = client.get_candles(symbol, timeframe_minutes, chart_start, end)
        book_task = client.get_order_book(symbol, depth=20)
        if market is None:
            markets_task = client.get_markets()
            minute, hourly, chart, book, markets = await asyncio.gather(
                minute_task, hourly_task, chart_task, book_task, markets_task
            )
            market = markets.get(symbol.upper())
        else:
            minute, hourly, chart, book = await asyncio.gather(
                minute_task, hourly_task, chart_task, book_task
            )
    if market is None or not market.active or market.quote_asset != "TMN":
        raise ValueError("بازار انتخاب‌شده فعال و با پایهٔ تومان نیست.")
    return analyze_market_data(
        config,
        market,
        minute,
        hourly,
        book,
        chart_candles=chart[-bars:],
        chart_timeframe_minutes=timeframe_minutes,
    )


async def fetch_watchlist_quotes(config: AppConfig, symbols: list[str]) -> list[WatchQuote]:
    unique_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))[:12]
    async with _public_client(config) as client:
        books = await asyncio.gather(
            *(client.get_order_book(symbol, depth=5) for symbol in unique_symbols),
            return_exceptions=True,
        )
    quotes: list[WatchQuote] = []
    for symbol, result in zip(unique_symbols, books, strict=True):
        if isinstance(result, BaseException):
            continue
        midpoint = (result.best_bid + result.best_ask) / 2
        quotes.append(
            WatchQuote(
                symbol=symbol,
                bid=result.best_bid,
                ask=result.best_ask,
                midpoint=midpoint,
                spread_bps=(result.best_ask - result.best_bid) / midpoint * Decimal("10000"),
                captured_at=result.captured_at,
            )
        )
    return quotes


def chart_indicator_frame(candles: list[Candle]) -> pd.DataFrame:
    """Build display-only indicators for any supported chart timeframe."""

    frame = candles_to_frame(candles).copy()
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    frame["ema_9"] = frame["close"].ewm(span=9, adjust=False).mean()
    frame["ema_21"] = frame["close"].ewm(span=21, adjust=False).mean()
    middle = frame["close"].rolling(20).mean()
    deviation = frame["close"].rolling(20).std(ddof=0)
    frame["bollinger_mid"] = middle
    frame["bollinger_upper"] = middle + 2 * deviation
    frame["bollinger_lower"] = middle - 2 * deviation
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    session = frame.index.tz_convert("Asia/Tehran").date
    cumulative_pv = (typical * frame["volume"]).groupby(session).cumsum()
    cumulative_volume = frame["volume"].groupby(session).cumsum().replace(0, np.nan)
    frame["vwap"] = cumulative_pv / cumulative_volume
    delta = frame["close"].diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    frame["rsi_14"] = 100 - (100 / (1 + gains / losses.replace(0, np.nan)))
    return frame


def _public_client(config: AppConfig) -> WallexClient:
    return WallexClient(
        config.exchange.rest_url,
        config.exchange.websocket_url,
        timeout_seconds=config.exchange.request_timeout_seconds,
        max_retries=config.exchange.max_retries,
    )
