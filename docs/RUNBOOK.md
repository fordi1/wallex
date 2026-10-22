# Operational runbook

## Non-negotiable live prerequisites

1. Revoke the previously exposed Wallex credential. Never reuse it.
2. Create a replacement key with trade permission only, no withdrawal permission,
   and a Liara egress IP allowlist.
3. Fill the official market allowlist and timezone-aware challenge timestamps in
   `config.yaml`.
4. Run shadow mode continuously for at least 24 hours, then run
   `python scripts/verify_shadow_gate.py` and retain the signed deployment logs.
5. Test PostgreSQL backup/restore, Telegram delivery, the Liara AI canary, NTP/UTC
   clock, and authenticated read-only reconciliation.
6. Set the live gate variables only in Liara. Keep `execution.canary: true` for
   the first live session.

## Start, observe, stop

- Engine health: `GET /health`; only `status=ready` is healthy.
- Dashboard: separate deployment, read-only database role, and
  `DASHBOARD_PASSWORD` configured.
- Graceful stop: stop the engine app only after confirming open orders and
  positions have reconciled. Restart preserves intents, positions, and the kill
  switch in PostgreSQL.
- Never place a manual order while the engine owns the account. It will create a
  reconciliation drift and block entries.

## Incident actions

| Alert | Immediate action |
|---|---|
| stale market data | Keep exits active; inspect WebSocket and REST latency. |
| reconciliation drift | Do not clear state manually; compare balances, open orders, fills, and protection. |
| rejected/ambiguous order | Search by deterministic client ID before any retry. |
| three AI failures | Entries remain paused; restore a valid regime response. |
| −3% daily P&L | Entry pause clears only at the next Tehran trading day. |
| −10% peak drawdown | Kill switch persists, buys are canceled, and positions flatten. |
| final three hours | New entries stop and maker exits become progressively aggressive. |
| final 30 minutes | Remaining tradable balances are market-sold; dust is recorded. |

The kill switch may be cleared only after incident review and an explicit
`LIVE_RESTART_ACKNOWLEDGED=true` restart. Do not edit the database row while the
engine is running.

## Liara topology

- `engine`: one replica from `Dockerfile.engine`, private health service,
  persistent disk at `/app/data`, full secret set.
- `dashboard`: separately restartable from `Dockerfile.dashboard`, read-only
  PostgreSQL credential and dashboard password only—never Wallex or AI keys.
- managed PostgreSQL: same private network, automated backups enabled.

Liara deploys these as two Docker applications from one repository; the Compose
format is not used for production deployment.

