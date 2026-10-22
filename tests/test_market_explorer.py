from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wallex_engine.models import BookLevel, Candle, MarketSpec, OrderBookSnapshot
from wallex_engine.ui.market_explorer import (
    active_tmn_markets,
    analyze_market_data,
    chart_indicator_frame,
)


def _market(symbol: str, quote: str, active: bool = True) -> MarketSpec:
    return MarketSpec(
        symbol=symbol,
        base_asset=symbol.removesuffix(quote),
        quote_asset=quote,
        active=active,
        tick_size=Decimal("1"),
        step_size=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"),
        min_notional=Decimal("10000"),
    )


def _candles(symbol: str, minutes: int, count: int) -> list[Candle]:
    start = datetime(2026, 10, 20, tzinfo=UTC)
    result: list[Candle] = []
    for index in range(count):
        price = Decimal("100000") + Decimal(index + (index % 20) * 100)
        result.append(
            Candle(
                symbol=symbol,
                timeframe_minutes=minutes,
                opened_at=start + timedelta(minutes=minutes * index),
                open=price,
                high=price + Decimal("100"),
                low=price - Decimal("100"),
                close=price + Decimal("20"),
                volume=Decimal("10") + Decimal(index % 7),
            )
        )
    return result


def test_active_tmn_markets_filters_and_sorts() -> None:
    markets = {
        "BTCTMN": _market("BTCTMN", "TMN"),
        "ATMTMN": _market("ATMTMN", "TMN"),
        "BTCUSDT": _market("BTCUSDT", "USDT"),
        "OLDTMN": _market("OLDTMN", "TMN", active=False),
    }

    result = active_tmn_markets(markets)

    assert [market.symbol for market in result] == ["ATMTMN", "BTCTMN"]


def test_market_explorer_builds_features_and_read_only_signal(config, market) -> None:
    minute = _candles(market.symbol, 1, 720)
    hourly = _candles(market.symbol, 60, 120)
    book = OrderBookSnapshot(
        symbol=market.symbol,
        captured_at=datetime.now(tz=UTC),
        bids=(BookLevel(price=Decimal("107190"), quantity=Decimal("2")),),
        asks=(BookLevel(price=Decimal("107210"), quantity=Decimal("1")),),
    )

    view = analyze_market_data(config, market, minute, hourly, book)

    assert view.market.symbol == market.symbol
    assert view.candles_5m
    assert view.chart_timeframe_minutes == 5
    assert view.chart_candles == view.candles_5m
    assert view.feature.symbol == market.symbol
    assert view.signal.decision_id


def test_chart_indicators_support_arbitrary_timeframe(market) -> None:
    candles = _candles(market.symbol, 15, 80)

    frame = chart_indicator_frame(candles)

    assert len(frame) == 80
    assert {"ema_9", "ema_21", "bollinger_upper", "vwap", "rsi_14"} <= set(
        frame.columns
    )
    assert frame["ema_9"].iloc[-1] > 0
