# Polymarket BTC Bot

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Mode](https://img.shields.io/badge/mode-paper--default-orange)

An automated market-making bot for Polymarket's binary 5-minute BTC UP/DOWN markets. Posts maker-only GTC limit orders (zero taker fees), derives a directional confidence from Binance BTC/USDT trades, and sizes positions with fractional Kelly after a warmup period.

**This bot is for educational purposes.** Paper mode is the default and is safe. Live mode requires a funded Polymarket wallet and real USDC; crypto trading can and does lose money.

## Table of Contents

- [Overview](#overview)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install — Linux / WSL](#install--linux--wsl)
- [Install — macOS](#install--macos)
- [Install — Windows](#install--windows)
- [Configuration](#configuration)
- [Running (paper mode)](#running-paper-mode)
- [Running (live mode)](#running-live-mode)
- [Architecture](#architecture)
- [Features](#features)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Further reading](#further-reading)
- [License](#license)

## Overview

Polymarket lists binary 5-minute BTC markets that resolve UP or DOWN based on the BTC price change over a 5-minute window. This bot watches Binance BTC/USDT trades over a live websocket, computes a directional confidence score from recent price momentum, and places maker-only limit orders on Polymarket during the last ~45 seconds of each window. Maker-only orders pay zero taker fees, which is critical — the edge on this strategy is narrow. After 100 resolved trades, position sizing transitions from flat bets to a fractional-Kelly rule.

## How it works

1. **Signal.** `signal_engine.py` computes a 7-feature confidence score from the BTC price path in the current 5-minute window (window delta, microstructure momentum, optional 2-minute trend slope, etc.). Output is a `Signal(side, confidence, ev)` record.
2. **Gate.** Before posting, a set of gates check that confidence is high enough, edge (`confidence − side_ask`) is positive, ask is in a sane range, and optional filters pass (blocked hours, minimum window delta).
3. **Reprice loop.** If the gate passes, the bot enters a 5-second reprice loop: check the live ask, post `target = ask − 0.01` as a maker order, cancel and reprice if the ask drifts. Loop runs from T-45s to T-8s before window close.
4. **Resolution.** At window close, Polymarket resolves the market. The bot records WIN / LOSS / UNFILLED / SKIPPED to `logs/trades_YYYYMMDD.json` and updates balance via `risk_manager.py`.

See `docs/HANDOVER/02_strategy.md` for the full strategy writeup.

## Requirements

- Python 3.10 or newer
- `pip`, `venv`, `git`
- (Paper mode) Nothing else — works out of the box with defaults
- (Live mode) A funded Polymarket account with USDC on Polygon, and the wallet private key

## Install — Linux / WSL

```bash
git clone https://github.com/UtkarshGupta0/polymarket-btc-bot.git
cd polymarket-btc-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` in your preferred editor. For paper mode, the defaults are fine; you can run the bot immediately after.

## Install — macOS

Same as Linux, with two extras:

1. If `python3` is not installed, use Homebrew:
   ```bash
   brew install python@3.11
   ```
2. If `pip install aiohttp` fails with a compilation error on Apple Silicon (M1/M2/M3), install Xcode Command Line Tools first:
   ```bash
   xcode-select --install
   ```

Then follow the Linux steps.

## Install — Windows

Use PowerShell (not Command Prompt):

```powershell
git clone https://github.com/UtkarshGupta0/polymarket-btc-bot.git
cd polymarket-btc-bot
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

If `venv\Scripts\Activate.ps1` fails with a script-execution error, allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
venv\Scripts\Activate.ps1
```

Edit `.env` in Notepad or your preferred editor.

## Configuration

All settings live in `.env`. Below is a summary of the most important variables. The full list is in [`docs/HANDOVER/03_configuration.md`](docs/HANDOVER/03_configuration.md).

| Variable | Default | Purpose |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` (simulated) or `live` (real orders) |
| `STARTING_CAPITAL` | `30.0` | Starting balance in USDC |
| `MIN_BET_SIZE` / `MAX_BET_SIZE` | `1.0` / `5.0` | Per-trade size range |
| `MIN_CONFIDENCE` | `0.50` | Skip if signal confidence below this |
| `MIN_EDGE` | `0.02` | Required `confidence − ask` to enter |
| `KELLY_FRACTION` | `0.25` | Fractional-Kelly multiplier (active from trade 1) |
| `MAX_DAILY_DRAWDOWN` | `5.0` | Daily loss limit in USDC; halts trading for the day |
| `MAX_CONSECUTIVE_LOSSES` | `5` | Loss streak that pauses trading |
| `TRADING_HOURS_BLOCK` | _(empty)_ | Comma-separated UTC hours to block new entries (e.g. `0,2,3,20,21`) |
| `POLYMARKET_PRIVATE_KEY` | _(unset)_ | Required for live mode only |
| `POLYMARKET_FUNDER_ADDRESS` | _(unset)_ | Required for live mode only |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | _(unset)_ | Optional trade alerts |

## Running (paper mode)

With `.env` populated and the virtualenv active:

```bash
python bot.py
```

Paper trades append to `logs/trades_YYYYMMDD.json`. A human-readable run log is written to `logs/paper_run_<timestamp>.log`. Stop the bot with `Ctrl+C` — it cancels any open order cleanly.

Optional dashboard (displays live state at http://127.0.0.1:8787):

```bash
# Dashboard is ON by default. Disable via DASHBOARD_ENABLED=0 in .env.
```

## Running (live mode)

> **⚠️ Live mode places real orders with real money. Only enable after paper mode has been running stably for at least a week and you have read `docs/HANDOVER/07_going_live.md` end-to-end.**

1. Fund a Polymarket-compatible wallet with USDC on Polygon.
2. Get the wallet private key (never share it).
3. Edit `.env`:
   ```
   TRADING_MODE=live
   POLYMARKET_PRIVATE_KEY=0x<your 64-char key>
   POLYMARKET_FUNDER_ADDRESS=0x<your funder address>
   ```
4. Run:
   ```bash
   python bot.py
   ```

The bot validates live-mode requirements on startup and refuses to start if the private key is missing. It prints a boot summary including wallet address and signature type — confirm these before letting it place orders.

## Architecture

| Module | Purpose |
|---|---|
| `bot.py` | Main loop: window tick, signal, gate, reprice loop, resolution |
| `config.py` | Loads `.env`, validates, exposes frozen `CONFIG` dataclass |
| `price_feed.py` | Binance BTC/USDT websocket → rolling price state |
| `market_finder.py` | Polls Polymarket gamma API for current 5-min BTC market |
| `signal_engine.py` | 7-feature confidence score + gate-vs-market checks |
| `risk_manager.py` | Balance tracking, drawdown limits, sizing (flat then Kelly) |
| `executor.py` | Paper executor + live CLOB client (lazy-imported) |
| `trade_logger.py` | JSON-per-day trade records |
| `telegram_alerts.py` | Optional push notifications |
| `dashboard.py` | aiohttp web UI showing live state |
| `backtester.py` | Offline replay against historical Binance candles |
| `self_improver.py` | Advisory stats over logs/trades_*.json |
| `scripts/validate_gates.py` | Counterfactual gate analysis over paper history |

Full architecture notes: [`docs/HANDOVER/01_architecture.md`](docs/HANDOVER/01_architecture.md).

## Features

- **Maker-only orders** — posts at `ask − 0.01` to stay on the book; Polymarket charges no fees for maker fills
- **7-feature signal** — window delta, microstructure, 2-minute trend slope, and more
- **Fractional Kelly sizing** — flat bets until 100 trades, then 0.25x Kelly thereafter
- **Configurable gates** — `MIN_CONFIDENCE`, `MIN_EDGE`, `TRADING_HOURS_BLOCK`, `MIN_DELTA_PCT`
- **Reprice loop** — re-posts every 5 seconds to stay at top of book
- **Paper mode** — full simulation with realistic fill logic; safe default
- **Backtester** — `python backtester.py` replays 30 days of Binance data
- **Self-improver advisory** — `python self_improver.py` surfaces parameter suggestions from recent trade history
- **Gate validator** — `python scripts/validate_gates.py` counterfactually applies configured gates to past paper trades
- **Dashboard** — local aiohttp UI shows live bot state
- **Telegram alerts** — optional per-trade notifications

## Testing

Tests follow a plain-assert convention — no `pytest` runner required. Each file is individually executable:

```bash
python tests/test_gate_vs_market.py
python tests/test_sizing_transition.py
python tests/test_trend_slope.py
# ...etc
```

Or run all at once:

```bash
for f in tests/test_*.py; do python "$f" || break; done
```

Expected: each test file prints `PASS ✓ <name>` on success.

## Troubleshooting

**`pip install aiohttp` fails on macOS:**
Install Xcode Command Line Tools: `xcode-select --install`.

**`websockets` fails on Windows:**
Make sure you used PowerShell (not Command Prompt), and that Python is 3.10 or newer (`python --version`).

**Bot logs "no market found":**
Polymarket's 5-minute BTC market listing can lag by a few seconds at window boundaries. If it persists past a full window, check `CLOB_API_URL` and `GAMMA_API_URL` in `.env` and verify you can reach them from your network.

**Dashboard port 8787 already in use:**
Change `DASHBOARD_PORT` in `.env` or set `DASHBOARD_ENABLED=0` to disable.

**No BTC ticks showing up in logs:**
The Binance websocket URL (`BINANCE_WS_URL`) may be blocked in your region. Try a VPN or verify you can `curl https://api.binance.com/api/v3/ping` from the same machine.

## Further reading

- [Architecture overview](docs/HANDOVER/01_architecture.md)
- [Strategy writeup](docs/HANDOVER/02_strategy.md)
- [Full configuration reference](docs/HANDOVER/03_configuration.md)
- [Operations guide](docs/HANDOVER/04_operations.md)
- [Maintenance guide](docs/HANDOVER/05_maintenance.md)
- [Development guide](docs/HANDOVER/06_development.md)
- [Going live checklist](docs/HANDOVER/07_going_live.md)
- [Design specs](docs/superpowers/specs/)

## License

MIT — see [LICENSE](LICENSE).

**Disclaimer:** This software is provided "as is" for educational purposes. Cryptocurrency trading involves substantial risk of loss. Past paper-trading performance does not guarantee future live-trading results. The authors accept no responsibility for any financial loss incurred through use of this software. You are solely responsible for verifying the bot's behavior in paper mode before enabling live trading, and for the security of your wallet private keys.
