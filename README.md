# Wallex AI Trading Engine

A fail-closed Python 3.11 spot engine with one strategy and risk path shared by
backtest, shadow, and live modes. It targets positive return but never guarantees
it, manufactures activity, or relaxes risk controls for ranking.

## Safety status

The repository ships with an empty market allowlist and empty challenge times.
Therefore `backtest`, `shadow`, and `live` all refuse to trade until verified
values are supplied. Live mode additionally requires all credential, shadow,
and canary environment gates. No CI test submits an order.

The credential exposed before this implementation is considered compromised.
Revoke it and issue a trade-only, IP-restricted replacement without withdrawal
permission. Do not put any key in `config.yaml`.

## Quick start

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m alembic upgrade head
python -m pytest
```

After adding verified challenge values:

```powershell
python main.py backtest --config config.yaml
python main.py shadow --config config.yaml
python main.py live --config config.yaml
python main.py report --run-id RUN_ID
streamlit run dashboard.py
```

Trading terminal (TradingView-style chart, backtest/shadow/live tabs, AI model
settings) on this machine only:

```powershell
python terminal.py   # then open http://127.0.0.1:8600
```

It has no password but accepts requests only from its own page on 127.0.0.1;
the live tab still needs an explicit confirmation and every live gate.

Environment variables are read directly by the process; `.env` is an example,
not automatically loaded in production. The dashboard needs only
`DASHBOARD_DATABASE_URL` (a read-only database user) and `DASHBOARD_PASSWORD`.
Set `LIARA_AI_BASE_URL` to the per-service `baseUrl` shown by Liara; no sample
service identifier is committed to this repository.

## What is implemented

- Typed Pydantic domain models and replaceable protocols.
- Decimal-only money/quantity decisions and exchange-precision quantization.
- Official Wallex REST endpoints plus raw `wss://api.wallex.ir/ws` trade stream,
  reconnect/ping/resubscribe, freshness checks, and REST depth fallback.
- Paginated/deduplicated history, gap detection, partitioned Parquet, completed
  1-minute to 5-minute resampling, and native hourly context.
- EMA, RSI, ATR, Bollinger, session VWAP, volume z-score, spread, and five-level
  imbalance; trend and mean-reversion rules with fee/slippage edge checks.
- Liara OpenAI-compatible JSON adapter and direct OpenAI Responses structured
  adapter, strict Pydantic validation, deterministic cache, retry, fallback, and
  repeated-failure entry pause.
- Canary/full portfolio constraints, Tehran-day stop, drawdown sizing and
  persistent kill switch, cooldowns, hold time, and no-opposing-order checks.
- Durable order intents, deterministic client IDs, partial-fill protection,
  maker retry budget, reconciliation, and challenge-end flatten stages.
- SQLite/PostgreSQL event store, Alembic migration, immutable manifests,
  JSONL/CSV exports, Plotly reports, health endpoint, and Telegram alerts.
- Password-protected, read-only Persian/RTL Streamlit dashboard with centralized
  Persian number/money/percent/Jalali-time formatting and accessible chart table.

## Verification

```powershell
python -m ruff check .
python -m pytest
python -m alembic upgrade head
docker build -f Dockerfile.engine -t wallex-engine .
docker build -f Dockerfile.dashboard -t wallex-dashboard .
```

Public adapter checks are read-only. Authenticated checks must use the new
restricted key and may call balances, fees, open orders, order details, and
fills only. Do not run a create/cancel contract check in CI.

See the [strategy, AI and risk document for judges](docs/STRATEGY.md), the
[architecture](docs/ARCHITECTURE.md) and the [operational runbook](docs/RUNBOOK.md).

## Release acceptance

A GitHub tag is appropriate only after CI is green, both Docker images reproduce,
the 24-hour gate passes, deployment backups and alerts are tested, the official
allowlist/timestamps are recorded, and final TMN flattening is verified. This
repository does not claim those external gates have passed.

## Current verification status (2026-10-22)

- Local static checks pass and all 25 tests pass.
- A fresh Alembic migration succeeds from an empty SQLite database.
- Public, read-only Wallex smoke checks returned 373 markets, 187 active TMN
  spot markets, candles, a 20×20 depth snapshot, and a raw-WebSocket-triggered
  depth update. No authenticated mutation endpoint was called.
- The dashboard was exercised with representative data at desktop and 320,
  360, and 390 pixel widths: RTL/font application, keyboard focus, Persian
  digits/Jalali time, and horizontal overflow were checked.
- Dockerfiles are present, but the local Docker Desktop daemon was unavailable,
  so reproducible image builds still need to run in CI or on a Docker host.
- The official allowlist/timestamps, replacement credential, authenticated
  read-only reconciliation, Liara resources, 24-hour shadow approval, canary,
  backup/alert tests, GitHub tag, and final flatten proof remain external gates.
