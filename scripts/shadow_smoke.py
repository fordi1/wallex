"""Bounded live-price/paper-money observation. Exchange mutations are blocked."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import datetime as dt
import json
import logging
from pathlib import Path
import time

from pishro.bot import Bot
from pishro.config import ROOT, Secrets, load_config
from pishro.exchange.wallex import WallexClient


async def run(minutes: float):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "reports" / f"shadow-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=output / "run.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", encoding="utf-8")
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Safety at both configuration and transport boundaries. No account API calls.
    cfg = load_config(overrides={"mode": "paper", "data_dir": str(output / "data"),
                                 "ai.enabled": False, "ml.retrain_hours": 0})
    cfg.secrets = Secrets()
    request = WallexClient._request
    calls = Counter()

    async def public_only(client, method, path, signed=False, **kw):
        if signed or method.upper() != "GET" or "account" in path:
            raise AssertionError("shadow test blocks all private/mutating Wallex requests")
        calls[path] += 1
        return await request(client, method, path, signed=False, **kw)

    WallexClient._request = public_only
    bot = Bot(cfg)
    # Notifier also reads optional Telegram destinations directly from environment.
    bot.notifier.targets.clear()
    assert not bot.live and bot.broker.name == "paper" and not bot.notifier.targets
    event_counts = Counter()
    bot.dlog.listeners.append(lambda rec: event_counts.update([rec["kind"]]))
    stages = {"warmup_started_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    print(json.dumps({"phase": "warmup", "output": str(output), "mode": bot.mode,
                      "capital_tmn": bot.capital, "ai": False, "ml": bot.ml_enabled if hasattr(bot, "ml_enabled")
                      else bool(cfg.get("ml.enabled")), "observation_seconds": minutes * 60}), flush=True)
    ticks = errors = candidates_total = 0
    blocked = Counter()
    samples = []
    error_types = Counter()
    tasks = []
    try:
        try:
            await asyncio.wait_for(bot.market.warmup(float(cfg.get("strategy.warmup_history_days", 3))), 180)
            stages["warmup_complete"] = True
        except TimeoutError:
            stages["warmup_complete"] = False
            stages["warmup_error"] = "timeout at 180 seconds"
        await bot.market.refresh_markets()
        await bot.market.refresh_refs()
        bot.market.update()
        bot.broker.zero_fee = {s for s, info in bot.market.infos.items() if info.zero_fee}
        await bot.broker.start()
        await bot._update_account()
        initial_equity = bot.engine.account.equity
        bot.risk.peak = initial_equity
        bot.status_text = "shadow smoke"
        if bot.stream:
            tasks.append(asyncio.create_task(bot.stream.run(bot.stop_event, bot.market.on_depth, bot.market.on_trade)))
        if cfg.get("references.binance_ws_enabled", True):
            bases = sorted({bot.market.infos[s].base for s in bot.market.universe.tradable})
            tasks.append(asyncio.create_task(bot.binance.stream(bases, bot.stop_event)))
        start, wall_start = time.monotonic(), time.time()
        deadline = start + minutes * 60
        stages["observation_started_utc"] = dt.datetime.fromtimestamp(wall_start, dt.timezone.utc).isoformat()
        print(json.dumps({"phase": "observing", "start_utc": stages["observation_started_utc"],
                          "markets": len(bot.market.universe.tradable),
                          "warm_states": sum(s.ready for s in bot.market.states.values()), **stages}), flush=True)
        next_report = start
        interval = float(cfg.get("exchange.poll_interval_sec", 2))
        with open(output / "samples.jsonl", "w", encoding="utf-8") as out:
            while time.monotonic() < deadline:
                tick_start = time.monotonic()
                try:
                    await asyncio.wait_for(asyncio.gather(bot.market.refresh_markets(), bot.market.refresh_refs()),
                                           timeout=min(20, max(.1, deadline - time.monotonic())))
                    bot.market.update()
                    if bot.stream:
                        bot.stream.set_symbols(set(bot.market.watch))
                except Exception as exc:
                    errors += 1
                    error_types[type(exc).__name__] += 1
                    logging.exception("market refresh failed")
                try:
                    await bot._on_tick()
                    ticks += 1
                except Exception as exc:
                    errors += 1
                    error_types[type(exc).__name__] += 1
                    logging.exception("execution tick failed")
                candidates_total += len(bot.engine.candidates)
                blocked.update(bot.engine.blocked.values())
                now = time.monotonic()
                if now >= next_report or now >= deadline:
                    await bot._update_account()
                    sample = {"elapsed_sec": round(now - start, 1), "ticks": ticks, "errors": errors,
                              "equity_tmn": round(bot.engine.account.equity, 2),
                              "free_tmn": round(bot.engine.account.free_tmn, 2),
                              "open_positions": len(bot.portfolio.positions), "working_buys": len(bot.engine.entries),
                              "working_sells": len(bot.engine.exits), "candidates": len(bot.engine.candidates),
                              "data_age_sec": round(bot.market.data_age(), 2),
                              "warm_states": sum(s.ready for s in bot.market.states.values()),
                              "events": dict(event_counts), "top_blocks": blocked.most_common(5),
                              "halted": bool(bot.risk.halted), "paused": bot.risk.pause_reason}
                    samples.append(sample)
                    out.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    out.flush()
                    print(json.dumps(sample, ensure_ascii=True), flush=True)
                    next_report = now + 30
                await asyncio.sleep(max(0, min(interval - (time.monotonic() - tick_start), deadline - time.monotonic())))
        elapsed = time.monotonic() - start
        # Stop input events, cancel only simulated working orders, and retain open
        # simulated positions for honest mark-to-market (no forced closing trade).
        bot.stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.engine.cancel_all("پایان تست سایه ده دقیقه‌ای")
        await bot._update_account()
        trades = bot.store.query("SELECT * FROM trades")
        position_values = []
        estimated_exit_fees = 0.0
        for p in bot.portfolio.positions.values():
            q = bot.market.quotes.get(p.symbol)
            value = p.qty * q.bid if q and q.valid else 0.0
            fee = value * max(bot.broker.fee(p.symbol))
            estimated_exit_fees += fee
            position_values.append({"symbol": p.symbol, "qty": p.qty, "cost_tmn": p.qty * p.entry_price,
                                    "bid_value_tmn": value, "estimated_exit_fee_tmn": fee})
        final_equity = bot.engine.account.equity
        summary = {"mode": "paper_live_prices", "private_requests": 0, "real_orders": 0,
                   "duration_seconds": round(elapsed, 2), "stages": stages,
                   "initial_equity_tmn": initial_equity, "final_marked_equity_tmn": final_equity,
                   "marked_pnl_tmn": final_equity - initial_equity,
                   "estimated_liquidation_pnl_tmn": final_equity - initial_equity - estimated_exit_fees,
                   "realized_pnl_tmn": sum(t["pnl_tmn"] for t in trades), "closed_trades": len(trades),
                   "winning_trades": sum(t["pnl_tmn"] > 0 for t in trades),
                   "positions": position_values, "ticks": ticks, "errors": errors,
                   "error_types": dict(error_types), "events": dict(event_counts),
                   "candidate_tick_observations": candidates_total, "blocked_tick_observations": dict(blocked),
                   "markets": len(bot.market.universe.tradable),
                   "warm_states": sum(s.ready for s in bot.market.states.values()),
                   "fee_fallback": bot.broker.fallback, "ai_enabled": False,
                   "ml_enabled": bool(cfg.get("ml.enabled")), "public_requests": dict(calls),
                   "limits": "Paper fills omit real queue position and exchange latency. Ten minutes cannot establish profitability."}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"phase": "complete", "report": str(output / "summary.json"), **summary}, ensure_ascii=True), flush=True)
    finally:
        bot.stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.shutdown()
        bot.store.conn.close()
        WallexClient._request = request


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10)
    args = parser.parse_args()
    if not 0 < args.minutes <= 60:
        parser.error("minutes must be in (0, 60]")
    asyncio.run(run(args.minutes))
