# Polymarket BTC 5-min Bot — Handover

This is the operator/developer handover bundle for the Polymarket BTC 5-minute maker bot at `/home/utk/polymarket-btc-bot/`. Read the files in order if you're new; jump to a specific section if you're debugging a specific thing.

## What this bot does (one paragraph)

Every 5 minutes Polymarket opens a new binary market: "Will BTC be higher or lower at the end of this 5-min window?" The bot subscribes to Binance's BTCUSDT tick stream (sub-100ms latency), computes a directional signal from recent price action, and — only when its own confidence exceeds the Polymarket ask price by a configurable edge — places a resting *maker* limit buy on the side it believes will win. Maker orders pay zero fees on Polymarket. The bot holds the position through resolution (or cancels if the edge disappears), logs the outcome, applies fractional-Kelly position sizing, and halts itself on drawdown/loss-streak breaches. It runs in paper mode by default; flipping `TRADING_MODE=live` in `.env` switches it to real USDC orders via `py-clob-client`.

## Files in this bundle

| File | What's in it |
|---|---|
| [`01_architecture.md`](01_architecture.md) | Module boundaries, data flow, tick lifecycle, what each file does |
| [`02_strategy.md`](02_strategy.md) | The edge thesis, signal math, edge-gate, sizing, Kelly transition |
| [`03_configuration.md`](03_configuration.md) | Every `.env` variable, what it does, safe ranges, defaults |
| [`04_operations.md`](04_operations.md) | How to start/stop, tmux, dashboard, logs, Telegram alerts |
| [`05_maintenance.md`](05_maintenance.md) | Common failures, gate-fail triage, how to roll back a change |
| [`06_development.md`](06_development.md) | Tests, backtester, code conventions, how to add a feature |
| [`07_going_live.md`](07_going_live.md) | Paper→live checklist, CLOB creds, MIN_SHARE_SIZE, cancel semantics |

## The ground truth

The long-form design doc is `POLYMARKET_BOT_GAMEPLAN.md` in the repo root — the original spec. This handover is the *as-built* reality, which has drifted in specific places (weights, gate logic, cancel hysteresis, flat-bet phase, trend-slope feature). Where they disagree, this handover is correct.

## Quickstart

```bash
cd /home/utk/polymarket-btc-bot
# 1. Confirm paper mode
grep ^TRADING_MODE .env        # should show TRADING_MODE=paper
# 2. Run in tmux
tmux new -s bot
python bot.py
# 3. Detach with Ctrl+b d. Dashboard at http://127.0.0.1:8787
```

Expected first-minute output: a config summary, a Binance WS connect line, a first-tick price, then a `Window opened` line every 5 minutes at clock-aligned times. No trades until the bot is inside the entry window (`ENTRY_WINDOW_END < seconds_remaining <= ENTRY_WINDOW_START`, default T-45s to T-8s) AND `conf - ask >= MIN_EDGE`.

## The non-obvious stuff

Five things that are easy to get wrong and are load-bearing:

1. **The gate is edge-based, not confidence-only.** A 70% confidence signal does not trigger a trade unless the opposite-side ask is cheap enough that `confidence - ask >= MIN_EDGE`. In an efficient market most fires *will* fail this gate — that is working correctly, not broken. See [02_strategy.md](02_strategy.md#the-edge-gate).

2. **Daily drawdown halts are in-memory.** Hitting `MAX_DAILY_DRAWDOWN` sets `RiskState.max_drawdown_hit=True` and blocks new trades until UTC midnight *or* a bot restart (which clears the flag). This is by design — a restart is a manual override. See [05_maintenance.md](05_maintenance.md#drawdown-halt).

3. **Paper mode holds through edge-loss by default.** `PAPER_HOLD_ON_EDGE_LOSS=1` suppresses both the N-strike cancel and the T-3s sweep. This collects honest P&L at resolution instead of synthetic cancels. Live mode must NOT run with this on. See [03_configuration.md](03_configuration.md#paper_hold_on_edge_loss).

4. **All trades sized by quarter-Kelly from trade 1.** `risk_manager.calculate_position_size()` returns 0 (with a DEBUG log) when Kelly produces fewer than `MIN_SHARE_SIZE` shares — the platform would reject those orders anyway. There is no flat-bet validation phase; `backtest_v2` plays that role on historical data. See [02_strategy.md](02_strategy.md#sizing).

5. **Polymarket book-depth capture is run separately (Front 1A MVP).** The bot itself does not subscribe to book updates; the capture script `scripts/capture_polymarket_books.py` does, writing to `data/books/`. See [10_book_capture.md](10_book_capture.md).

## Current state (snapshot)

- Mode: paper
- Signal features: delta, momentum, volume imbalance, VWAP, 30s velocity, book imbalance, 2-min trend-slope (all 7 active behind `SIGNAL_TREND_ENABLED=1`)
- Gate: edge-based (`conf - ask >= MIN_EDGE`) with `GATE_ASK_MIN=0.15`, `GATE_ASK_MAX=0.92`
- Sizing: flat $1 bets for first 100 trades, fractional Kelly thereafter
- Cancel: N=3 consecutive gate-fail ticks (hysteresis); paper holds to close if `PAPER_HOLD_ON_EDGE_LOSS=1`
- Live mode: `LiveExecutor` exists and is wired, untested with real money since last session

## When to read what

- **"The bot isn't placing trades"** → [05_maintenance.md §gate-fail-triage](05_maintenance.md#gate-fail-triage).
- **"I want to tune a parameter"** → [03_configuration.md](03_configuration.md) for the knob, [02_strategy.md](02_strategy.md) for *why* it's set where it is.
- **"I want to add a signal feature"** → [06_development.md §adding-a-feature](06_development.md#adding-a-feature).
- **"I want to flip to live money"** → [07_going_live.md](07_going_live.md). Do not skim this one.
