from __future__ import annotations

from datetime import datetime, timedelta

from .models import FlattenPlan


def flatten_plan(now: datetime, challenge_end: datetime) -> FlattenPlan:
    if now.tzinfo is None or challenge_end.tzinfo is None:
        raise ValueError("flatten timestamps must be timezone-aware")
    remaining = challenge_end - now
    seconds = max(0, int(remaining.total_seconds()))
    if remaining <= timedelta(0):
        stage = "ended"
        market = True
    elif remaining <= timedelta(minutes=30):
        stage = "market_exit"
        market = True
    elif remaining <= timedelta(hours=3):
        stage = "limit_exit"
        market = False
    else:
        stage = "normal"
        market = False
    return FlattenPlan(
        stage=stage,
        stop_new_entries=stage != "normal",
        cancel_pending_buys=stage != "normal",
        use_market_exits=market,
        seconds_remaining=seconds,
    )
