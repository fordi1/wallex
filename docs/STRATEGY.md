# Strategy, AI, Risk and Reproducibility

This is the judging document requested by the Wallex challenge brief. Every
claim below points at code or at a generated artifact that a judge can re-run.

## 1. Architecture and main logic

One pipeline is shared by `backtest`, `shadow` and `live`; only the broker
differs (see [ARCHITECTURE.md](ARCHITECTURE.md)).

```
Wallex REST/WebSocket -> candles (1m -> signal timeframe, plus 1h context)
  -> features (EMA 9/21, hourly EMA 20/50, RSI 14, ATR 14, Bollinger, VWAP,
               volume z-score, spread, 5-level book imbalance)
  -> HybridStrategy (rules propose)        src/wallex_engine/strategy/hybrid.py
  -> AI regime + signal review (AI disposes) src/wallex_engine/ai/providers.py
  -> PortfolioRiskManager (hard limits)     src/wallex_engine/risk/manager.py
  -> Broker: Backtest | Shadow | Live       src/wallex_engine/execution/brokers.py
  -> SQL event store + logs/decisions.jsonl src/wallex_engine/storage/database.py
```

## 2. Buy and sell conditions

Spot, long-only, TMN-quoted markets. An entry needs **all** of:

| Family | Entry rule |
|---|---|
| Trend | EMA9 crosses above EMA21 on the signal timeframe, hourly EMA20 > EMA50 and close > hourly EMA50, volume z-score > `volume_z_min` |
| Mean reversion | Hourly EMAs within 1% (ranging), previous close below lower Bollinger and current close back inside, previous RSI <= 30, ATR/close <= 3% |
| Both | Spread <= `max_spread_bps`, and **expected move > (round-trip fees + 2x slippage + spread) x `safety_factor`** |

The last line is the core economic filter: the Wallex account API reports a
0.25% maker / 0.30% taker fee per side for BTCTMN and ETHTMN, so a trade must
clear roughly 0.6-1% of modeled cost before it is allowed. This is why
the signal timeframe is chosen by the walk-forward check in section 6 rather
than by hand.

Exits (first that fires): ATR stop (`stop_atr` x ATR), trailing stop, target
(`trend_target_r` x risk, or Bollinger mid for mean reversion), time stop
(`time_stop_bars`). At challenge end every position is flattened to TMN so the
result is **realized in Toman**, as the brief requires.

## 3. Real use of AI (not a label on if/else)

The rules only *propose*. A language model (BoxAPI / Liara / OpenAI,
`ai.provider` in `config.yaml`) makes two structured decisions per candidate,
validated by strict Pydantic schemas:

1. **Regime** (`RegimeDecision`): classifies the market from the feature
   snapshot, may block all entries and sets a size multiplier 0..1.
2. **Signal review** (`ReviewDecision`): approves or vetoes the specific trade
   and may shrink its size 0..1, with a Persian reason that is logged.

The AI can only reduce risk, never enlarge a position or bypass a limit.
Calls are deterministic (`temperature: 0`, `seed: 42`, versioned prompt and
schema) and cached by input hash in `data/ai-cache/`, so a re-run replays the
same answers. After 3 consecutive AI failures new entries pause; a single
failure falls back to the rules at `fallback_size_multiplier` (0.5).

**Measured impact:** `python main.py backtest` runs every market twice, AI on
and AI off, and writes the difference in return, drawdown, realized PnL and
trade count into the report. This is the evidence that the AI changes decisions.

## 4. Capital and risk management

All limits live in `config.yaml` -> `risk` / `execution` and are enforced in
`PortfolioRiskManager` before any order is created:

- Never all-in: at least 40% of equity stays in TMN, max 60% total exposure,
  max 20% per symbol, max 3 positions.
- Risk per trade 1% of equity, sized from the ATR stop distance.
- Daily loss stop 3% (Tehran day); size is halved at 6% drawdown; a persistent
  kill switch stops trading at 10% drawdown.
- Cooldowns (15 min, 60 min after a loss), minimum hold, no opposing orders.
- Canary mode for the first live phase: 1 position, 5% per symbol, 0.25% risk.
- Durable order intents with deterministic client IDs, partial-fill handling
  and reconciliation every 60 s keep execution correct after restarts.

## 5. Decision log (required deliverable)

Every evaluated bar, traded or not, appends one JSON line to
`logs/decisions.jsonl` (and the `decisions` SQL table) containing the full
feature snapshot, the rule signal, the AI regime/review result, the risk
decision, the final outcome and a Persian `reason_fa`. Every AI call is stored
separately with its input hash. Manual trades are therefore distinguishable:
an order without a matching decision line was not placed by the algorithm.

## 6. Testing: backtest, walk-forward and shadow

| Test | Command | Output |
|---|---|---|
| Unit tests | `python -m pytest` | 45 tests |
| Backtest (real Wallex history, AI on vs off) | `python main.py backtest` | `reports/bt-*.md/.html`, `logs/decisions.jsonl` |
| Walk-forward parameter check (offline) | `python scripts/param_sweep.py --split <iso>` | `reports/param_sweep.json` |
| Shadow (live data, no orders) | `python main.py shadow` | decisions + simulated fills in the event store |
| Shadow gate before live | `python scripts/verify_shadow_gate.py` | pass/fail |

Backtest realism: completed candles only, fees 0.2% per side, 8 bps slippage
each way, and a pessimistic synthetic spread equal to `max_spread_bps`.
Minutes with no Wallex trades are filled as flat zero-volume bars; gaps longer
than 60 minutes are treated as an outage and the run fails closed.

### Final configuration (real fees and spreads)

Measured on 2026-10-22: fees from `/v1/account/fee` (maker 0.25%, taker 0.30%),
top-of-book spreads from `/v1/depth` (BTCTMN ~7 bps, ETHTMN ~33 bps), stored as
`backtest.spread_bps`. Same window and split as below.
Source: `reports/sweep_15m_realfees.json`, `reports/sweep_15m.json`, `reports/sweep_5m.json`.

| Signal TF | Safety factor | In-sample (BTC+ETH) | Out-of-sample | Trades | Max drawdown |
|---|---|---|---|---|---|
| 15m | 1.0 | +0.005% | +0.045% | 3 | 0.07% |
| **15m** | **1.25** (chosen) | **+0.005%** | **+0.045%** | 3 | 0.07% |
| 15m | 1.5 | -0.014% | 0.000% | 1 | 0.05% |

With the earlier 0.2% fee assumption, 15m/1.25 was also the in-sample winner
over 5m (1.0, 1.25, 1.5) and 15m (1.0, 1.5); lowering the safety factor added
trades but they were net losers. ETHTMN never passed the cost filter because
its spread alone is ~0.33%, which is the intended behaviour.

### AI on vs off (BTCTMN, live BoxAPI model, 15m)

Source: `reports/bt-BTCTMN-20261022T194935Z-e15822.md` and its `-ai-off` twin.

| | AI on | AI off |
|---|---|---|
| Return | -0.004% | +0.046% |
| Max drawdown | 0.035% | 0.059% |
| Trades | 1 | 3 |

The AI vetoed two rule signals and shrank one, each with a logged reason
(for example: round-trip fee too large relative to the target while the hourly
trend is down). In this week the two vetoed trades would have been small
winners, so the AI lowered return slightly while lowering drawdown. With three
trades this is within noise; the claim is that the AI has a real, measurable
and auditable effect on decisions, not that it improves return.

### First round (0.2% fee and 25 bps spread assumptions)

Real Wallex history, 2026-10-15 to 2026-10-22 (Tehran), BTCTMN and ETHTMN,
AI off (deterministic baseline, no `BOX_API_KEY` in the test environment).
Parameters were ranked on 10-15..10-19 only; 10-19..10-22 is out-of-sample.
Source: `reports/param_sweep.json`, `reports/bt-*.md`, `reports/backtest-run.log`.

| Signal TF | Target R | In-sample return (BTC+ETH) | In-sample trades | Out-of-sample return | Out-of-sample trades |
|---|---|---|---|---|---|
| 5m | 2 | -0.033% | 1 | 0.000% | 0 |
| **5m** | **3** | **-0.021%** (best) | 2 | 0.000% | 0 |
| 15m | 2 | -0.087% | 2 | 0.000% | 0 |
| 15m | 3 | -0.184% | 4 | 0.000% | 0 |
| 30m | 2 | -0.069% | 2 | +0.038% | 1 |
| 30m | 3 | -0.069% | 2 | +0.083% | 2 |

Honest reading:

- Maximum drawdown in every configuration stayed below 0.2% of capital:
  the risk layer works and capital is preserved.
- The strategy is very selective. With ~0.2% fee per side, the cost filter
  rejects most setups, so a week produces only 0-4 trades. Results are within
  noise of zero; no configuration is claimed to be reliably profitable.
- This first round fixed `trend_target_r: 3`; the timeframe and safety factor
  were then re-selected above with measured fees and spreads. The 30m
  variants did better out-of-sample here, but adopting them would be fitting to
  the test window, so that is recorded rather than adopted.
- USDTTMN was rejected by the data-quality gate (a 10-hour hole in Wallex
  candle history on 2026-10-19), which is the intended fail-closed behaviour.

## 7. Re-running (for judges)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python main.py backtest --config config.yaml
python scripts/param_sweep.py --split 2026-10-19T00:00:00+03:30
```

Set `BOX_API_KEY` to enable the AI comparison; without it the backtest runs the
deterministic AI-off baseline and says so. Each run writes an immutable
manifest of the exact configuration to `data/manifests/`.

## 8. Operator control panel

`streamlit run dashboard.py` -> tab "کنترل موتور" starts or stops exactly one
engine process in backtest, shadow or live mode. It is disabled unless
`DASHBOARD_CONTROL_ENABLED=true`. Before starting, it lists every gate that
would refuse the mode (missing challenge times, API key, shadow approval, ...)
and keeps the start button disabled; live also needs an explicit confirmation.
The panel never places orders itself: it only runs `main.py <mode>`, so every
trade still comes from the algorithm and has a decision-log line.
Code: `src/wallex_engine/runtime/control.py`.

### Trading terminal (`python terminal.py`)

A TradingView-style page on 127.0.0.1 that makes every judging item visible:

- **Signal analysis:** for any logged decision, every entry condition with its
  measured value and threshold (EMA cross, hourly trend, volume, Bollinger
  re-entry, RSI, volatility, spread, fee-adjusted edge), a count such as
  "trend: 2 of 5 conditions failed", then the AI regime/review and the risk
  constraints. `HybridStrategy.explain()` mirrors `evaluate()`; a property test
  checks that their verdicts agree on 300 random market states.
- **Risk and P&L:** each limit next to its current usage, per-trade rules,
  open positions with stop/target, realized and unrealized P&L.
- **Scorecard:** number of trades, volume, average trade size, net return,
  max drawdown, fees, execution latency (intent to fill), errors, decisions,
  AI vetoes and AI calls, each labelled with its rubric row.
- Each mode writes its own SQLite file (`data/shadow.db`, `data/live.db`), so
  simulated shadow positions can never be loaded by the live engine.

## 9. Wallex tools

Market data, balances, fees and orders use the official Wallex REST API and the
raw `wss://api.wallex.ir/ws` stream through one narrow adapter,
`src/wallex_engine/exchange/wallex_client.py`. The trading decision path does
not depend on Wallex MCP: a long-running engine needs deterministic, testable
calls with retries and idempotent client order IDs, which the REST adapter
provides. Wallex MCP is suitable for an operator agent that inspects the
account interactively.
