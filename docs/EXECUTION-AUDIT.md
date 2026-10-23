# Execution readiness — 2026-10-23

Engineering hardening implemented and validated offline. Read-only online preflight passed for Wallex markets, balances, fees, open orders and the configured global price reference. No real order was placed, changed or cancelled, and no live service was started. Read checks are not a certification of order execution or profitability.

## Implemented protections

- Unconfirmed cancellations retain order tracking. Replacement waits for terminal confirmation and accounts for cancellation-time fills.
- Cumulative fill notional is differenced for buy cost, sell proceeds and estimated sell fees. Partial sale cost updates are persisted. A 99.9% partial close no longer silently deletes the remainder.
- Live entry/exit intents, client IDs, full order snapshots, consumed fill quantities/notionals, cooldowns and observed fee floors are stored in kv.execution_state before submission. Ambiguous submissions remain UNKNOWN and are not POSTed again.
- Fill accounting, positions, trades, risk state and the consumed-fill checkpoint commit together in one SQLite transaction. SQLite uses WAL and synchronous FULL. Accounting failure stops further execution; restart reloads the last committed checkpoint.
- Startup reconciles known journal IDs before execution. Untracked bot IDs, unresolved orders, legacy positions without a journal, failed account queries or insufficient holdings block startup. External holdings are not automatically adopted; acquisition cost is not guessed.
- A process lease prevents simultaneous execution against the same local journal; an asyncio lock serializes ticks, reconciliation and shutdown. Startup validates configuration.
- Pending bids reserve exposure and position slots; local fills are included before the next account snapshot. Buy sizes leave a quote-fee buffer. Nonfinite risk inputs are rejected.
- Daily/loss pauses, consecutive losses and liquidation requests survive restart. A shorter cooldown cannot replace a longer daily pause.
- One zero-fee fill cannot waive fees globally. Observed higher rates form persistent per-market floors. Exit breakeven uses remaining actual unit cost, including all buy fills and fees.
- Stale per-market quotes prevent orders on that quote. Market-feed failures still run the risk tick so stale resting buys can be cancelled.
- Malformed submission acknowledgements and unknown exchange statuses remain uncertain. Cancellation uses the documented query parameter.

## Verification

- python -m pytest -q: offline suite including full paper simulation, restart recovery, transaction rollback on checkpoint failure, repeated fill replay, partial fills, API ambiguity, single-owner lease, feed outage, fee floors and risk persistence.
- python -m pishro preflight --online: passed all five read-only checks on 2026-10-23. Does not construct Bot or submit/cancel orders. Reports omit secrets and account balances.
- The six original recovery defects were reproduced as failing tests before correction. Earlier execution regressions were also reproduced before correction.

## Operational limits

- A crash immediately before/after send can leave an UNKNOWN intent that the exchange cannot find. It is not resubmitted or assumed rejected. An operator must reconcile against exchange records; do not erase the journal to bypass this block.
- Existing pre-journal positions/orders require migration using actual exchange executions. Unrelated holdings remain outside bot positions but are included in account valuation.
- The lease protects one data directory, not multiple machines or directories sharing an account. Use one execution deployment per account with persistent storage.
- Delayed fee corrections after an order closes are not reconstructed from a historical trade ledger. Missing fees are estimated; compare exchange statements with local accounting. A holdings shortfall blocks execution rather than silently shrinking positions.
- SQLite is authoritative. JSONL logs and notifications are not transactionally coupled to SQLite and may repeat around a crash.
- Stop/time exits are limit orders and cannot guarantee execution during gaps, outages or absent liquidity. Resting orders can remain at the exchange while private API access is unavailable.
- No real submission/cancellation, extended live shadow run, independent backtest validation or profitability validation was performed. Existing backtest results predate these execution changes.

References: [Wallex API](https://api-docs.wallex.ir/), [account wallet](https://developers.wallex.ir/basicservices/mainaccountwallet), [order details](https://developers.wallex.ir/spot/singleorderdetail).
