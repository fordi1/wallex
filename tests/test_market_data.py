from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wallex_engine.data.market_data import (
    deduplicate_and_validate,
    fill_quiet_gaps,
    resample_completed,
)
from wallex_engine.errors import StaleMarketDataError
from wallex_engine.models import Candle


def candle(at: datetime, completed: bool = True) -> Candle:
    return Candle(
        symbol="BTCTMN",
        timeframe_minutes=1,
        opened_at=at,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("2"),
        completed=completed,
    )


def test_gap_detection_and_deduplication() -> None:
    start = datetime(2026, 10, 22, tzinfo=UTC)
    with pytest.raises(StaleMarketDataError):
        deduplicate_and_validate([candle(start), candle(start + timedelta(minutes=2))])
    result = deduplicate_and_validate([candle(start), candle(start)])
    assert len(result) == 1


def test_quiet_minutes_are_filled_flat_but_long_outages_fail() -> None:
    start = datetime(2026, 10, 22, tzinfo=UTC)
    filled = fill_quiet_gaps([candle(start), candle(start + timedelta(minutes=3))], 60)
    assert [c.opened_at for c in filled] == [start + timedelta(minutes=i) for i in range(4)]
    assert filled[1].volume == 0
    assert filled[1].open == filled[1].high == filled[1].low == filled[1].close == Decimal("101")
    deduplicate_and_validate(filled)
    with pytest.raises(StaleMarketDataError, match="outage"):
        fill_quiet_gaps([candle(start), candle(start + timedelta(minutes=90))], 60)


def test_resampling_uses_only_completed_candles() -> None:
    start = datetime(2026, 10, 22, tzinfo=UTC)
    candles = [candle(start + timedelta(minutes=i), completed=i < 5) for i in range(6)]
    result = resample_completed(candles, 5)
    assert len(result) == 1
    assert result[0].volume == Decimal("10")


def test_parquet_store_round_trip(tmp_path) -> None:
    from wallex_engine.data.market_data import ParquetCandleStore

    start = datetime(2026, 10, 22, tzinfo=UTC)
    store = ParquetCandleStore(tmp_path)
    store.write([candle(start), candle(start + timedelta(minutes=1))])
    loaded = store.read("BTCTMN", 1, start, start + timedelta(minutes=1))
    assert [c.opened_at for c in loaded] == [start]
