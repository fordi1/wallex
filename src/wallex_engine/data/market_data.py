from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ..errors import StaleMarketDataError
from ..models import Candle, OrderBookSnapshot
from ..protocols import ExchangeClient

QUIET_GAP_MAX_MINUTES = 60


class ParquetCandleStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, candles: list[Candle]) -> None:
        if not candles:
            return
        frame = candles_to_frame(candles)
        frame["year"] = frame.index.year
        frame["month"] = frame.index.month
        for (symbol, timeframe, year, month), part in frame.groupby(
            ["symbol", "timeframe_minutes", "year", "month"]
        ):
            folder = (
                self.root
                / f"symbol={symbol}"
                / f"timeframe={timeframe}m"
                / f"year={year}"
                / f"month={month:02d}"
            )
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "candles.parquet"
            clean = part.drop(columns=["year", "month"]).reset_index()
            if path.exists():
                clean = pd.concat([pd.read_parquet(path), clean], ignore_index=True)
            clean = clean.drop_duplicates(subset=["opened_at"], keep="last").sort_values(
                "opened_at"
            )
            clean.to_parquet(path, index=False)

    def read(
        self,
        symbol: str,
        timeframe_minutes: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        folder = self.root / f"symbol={symbol}" / f"timeframe={timeframe_minutes}m"
        paths = list(folder.glob("year=*/month=*/candles.parquet"))
        if not paths:
            return []
        frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
        frame["opened_at"] = pd.to_datetime(frame["opened_at"], utc=True)
        if start:
            frame = frame[frame["opened_at"] >= pd.Timestamp(start)]
        if end:
            frame = frame[frame["opened_at"] < pd.Timestamp(end)]
        frame = frame.drop_duplicates("opened_at").sort_values("opened_at")
        return frame_to_candles(frame.set_index("opened_at"))


class WallexMarketDataSource:
    def __init__(self, client: ExchangeClient, store: ParquetCandleStore) -> None:
        self.client = client
        self.store = store

    async def history(
        self, symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> list[Candle]:
        candles = await self.client.get_candles(symbol, timeframe_minutes, start, end)
        candles = deduplicate_and_validate(fill_quiet_gaps(candles, QUIET_GAP_MAX_MINUTES))
        self.store.write(candles)
        return candles

    async def latest_book(self, symbol: str) -> OrderBookSnapshot:
        book = await self.client.get_order_book(symbol)
        if datetime.now(tz=UTC) - book.captured_at > timedelta(seconds=15):
            raise StaleMarketDataError(f"order book is stale for {symbol}")
        return book


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    frame = pd.DataFrame([candle.model_dump(mode="python") for candle in candles])
    frame["opened_at"] = pd.to_datetime(frame["opened_at"], utc=True)
    return frame.set_index("opened_at").sort_index()


def frame_to_candles(frame: pd.DataFrame) -> list[Candle]:
    rows = frame.reset_index().to_dict(orient="records")
    return [Candle.model_validate(row) for row in rows]


def deduplicate_and_validate(
    candles: list[Candle], *, allow_trailing_gap: bool = False
) -> list[Candle]:
    if not candles:
        return []
    ordered = sorted(
        {candle.opened_at: candle for candle in candles}.values(), key=lambda x: x.opened_at
    )
    step = timedelta(minutes=ordered[0].timeframe_minutes)
    gaps: list[datetime] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.opened_at - previous.opened_at != step:
            gaps.append(previous.opened_at + step)
    if gaps and not allow_trailing_gap:
        raise StaleMarketDataError(f"candle history contains {len(gaps)} internal gap(s)")
    return ordered


def fill_quiet_gaps(candles: list[Candle], max_gap_minutes: int) -> list[Candle]:
    """Wallex omits candles for minutes without trades; fill short gaps with flat bars.

    A missing bar means no trade, so price stays at the previous close with zero volume.
    Gaps longer than ``max_gap_minutes`` are treated as an outage and still fail closed.
    """
    if not candles:
        return []
    ordered = sorted(
        {candle.opened_at: candle for candle in candles}.values(), key=lambda x: x.opened_at
    )
    step = timedelta(minutes=ordered[0].timeframe_minutes)
    filled: list[Candle] = [ordered[0]]
    for current in ordered[1:]:
        previous = filled[-1]
        missing = current.opened_at - previous.opened_at - step
        if missing > timedelta(minutes=max_gap_minutes):
            raise StaleMarketDataError(
                f"candle history outage of {missing} after {previous.opened_at.isoformat()}"
            )
        at = previous.opened_at + step
        while at < current.opened_at:
            price = previous.close
            filled.append(
                previous.model_copy(
                    update={
                        "opened_at": at,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": Decimal("0"),
                        "completed": True,
                    }
                )
            )
            at += step
        filled.append(current)
    return filled


def resample_completed(candles: list[Candle], target_minutes: int) -> list[Candle]:
    if not candles:
        return []
    source_minutes = candles[0].timeframe_minutes
    if target_minutes % source_minutes:
        raise ValueError("target timeframe must be a multiple of source timeframe")
    frame = candles_to_frame([c for c in candles if c.completed])
    rule = f"{target_minutes}min"
    aggregated = frame.resample(rule, label="left", closed="left").agg(
        {
            "symbol": "first",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    counts = frame["close"].resample(rule, label="left", closed="left").count()
    aggregated = aggregated[counts == target_minutes // source_minutes].dropna()
    aggregated["timeframe_minutes"] = target_minutes
    aggregated["completed"] = True
    return frame_to_candles(aggregated)
