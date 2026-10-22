"""Walk-forward parameter check on cached Wallex candles (offline, deterministic).

Parameters are ranked on the in-sample window only; the out-of-sample window is
reported untouched so the chosen configuration is not fitted to it.

    python scripts/param_sweep.py --config config.yaml --split 2026-10-19T00:00:00+03:30
    python scripts/param_sweep.py --split ... --timeframes 5 --safety 1,1.25,1.5 --out s5.json
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wallex_engine.ai.providers import AIReviewService, DeterministicAIProvider  # noqa: E402
from wallex_engine.backtest.engine import BacktestEngine  # noqa: E402
from wallex_engine.config import load_config  # noqa: E402
from wallex_engine.data.market_data import ParquetCandleStore, resample_completed  # noqa: E402
from wallex_engine.exchange.wallex_client import WallexClient  # noqa: E402
from wallex_engine.models import WALLEX_FEES  # noqa: E402
from wallex_engine.reporting.generator import performance_metrics  # noqa: E402
from wallex_engine.storage.database import SqlEventStore  # noqa: E402

DEFAULT_GRID = {
    "signal_timeframe_minutes": [5, 15, 30],
    "stop_atr": [Decimal("1.5")],
    "trend_target_r": [Decimal("2"), Decimal("3")],
}
FEES = WALLEX_FEES


async def evaluate(config, store, spec, minute, hourly, split) -> dict[str, float]:
    """Run once over the whole window, then score the in- and out-of-sample halves."""
    signal = resample_completed(minute, config.strategy.signal_timeframe_minutes)
    run_id = f"sweep-{spec.symbol}-{config.strategy.signal_timeframe_minutes}"
    result = await BacktestEngine(config, store, AIReviewService(DeterministicAIProvider())).run(
        run_id, spec, signal, hourly, FEES
    )
    baseline = float(config.challenge.initial_capital_tmn)
    equity, trades = result.equity, result.trades
    scores: dict[str, float] = {}
    for label, inside in (("in", lambda t: t < split), ("out", lambda t: t >= split)):
        part = equity[equity["captured_at"].map(inside)] if not equity.empty else equity
        values = part["equity_tmn"].astype(float) if not part.empty else None
        closed = trades[trades["opened_at"].map(inside)] if not trades.empty else trades
        metrics = performance_metrics(part, closed, baseline)
        change = values.iloc[-1] - values.iloc[0] if values is not None else 0.0
        scores[f"{label}_ret"] = round(change / baseline * 100, 3)
        scores[f"{label}_trades"] = len(closed)
        scores[f"{label}_dd"] = round(metrics["max_drawdown"] * 100, 3)
    return scores


async def main() -> None:
    args = argparse.ArgumentParser()
    args.add_argument("--config", default="config.yaml")
    args.add_argument("--split", required=True)
    args.add_argument("--timeframes", help="comma list, e.g. 5,15")
    args.add_argument("--safety", help="comma list of safety_factor values, e.g. 1,1.25")
    args.add_argument("--cross", help="comma list of cross_window_bars values, e.g. 1,3,5")
    args.add_argument("--out", default="param_sweep.json")
    ns = args.parse_args()
    grid = dict(DEFAULT_GRID)
    if ns.timeframes:
        grid["signal_timeframe_minutes"] = [int(v) for v in ns.timeframes.split(",")]
    if ns.safety:
        grid["safety_factor"] = [Decimal(v) for v in ns.safety.split(",")]
    base = load_config(ns.config)
    if ns.safety:
        grid["trend_target_r"] = [base.strategy.trend_target_r]
    if ns.cross:
        grid["cross_window_bars"] = [int(v) for v in ns.cross.split(",")]
    split = datetime.fromisoformat(ns.split)
    start, end = base.backtest.start, base.backtest.end
    candles = ParquetCandleStore(base.app.data_dir / "candles")
    client = WallexClient(base.exchange.rest_url, base.exchange.websocket_url, None)
    markets = await client.get_markets()
    await client.close()
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = SqlEventStore(f"sqlite+aiosqlite:///{Path(tmp) / 'sweep.db'}", Path(tmp))
        await store.initialize()
        for values in itertools.product(*grid.values()):
            params = dict(zip(grid, values, strict=True))
            config = base.model_copy(
                update={"strategy": base.strategy.model_copy(update=params)}
            )
            row = {**params}
            for symbol in base.challenge.allowed_markets:
                minute = candles.read(symbol, 1, start, end)
                hourly = candles.read(symbol, 60, None, end)
                if not minute:
                    continue
                scores = await evaluate(config, store, markets[symbol], minute, hourly, split)
                row.update({f"{symbol}_{key}": value for key, value in scores.items()})
            row["in_ret_sum"] = sum(v for k, v in row.items() if k.endswith("_in_ret"))
            row["out_ret_sum"] = sum(v for k, v in row.items() if k.endswith("_out_ret"))
            rows.append(row)
            print(json.dumps(row, default=str), flush=True)
        await store.close()
    best = max(rows, key=lambda r: r["in_ret_sum"])
    print("BEST_IN_SAMPLE", json.dumps(best, default=str))
    out = base.app.reports_dir / ns.out
    payload = {"split": ns.split, "grid": grid, "rows": rows, "best": best}
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Saved {out}")


if __name__ == "__main__":
    asyncio.run(main())
