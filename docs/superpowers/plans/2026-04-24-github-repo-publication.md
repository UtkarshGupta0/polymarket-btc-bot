# GitHub Repo Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare `/home/utk/polymarket-btc-bot/` for first public push to `github.com/UtkarshGupta0/polymarket-btc-bot` — add `README.md`, `LICENSE`, `.gitignore`, `requirements.txt`, verify `.env.example`, purge cached/log artifacts from tracking, verify fresh-install works.

**Architecture:** Additive-only. All changes are new files at the repo root plus one `.gitignore`-driven untrack step. No edits to any existing `.py` file, test, or existing doc. Every task ends with a commit so progress is durable and any single task is individually rollback-able.

**Tech Stack:** Python 3.10+ (per `from __future__ import annotations` usage + `|` union syntax). Third-party deps: `aiohttp`, `websockets`, `requests`, `python-dotenv`, `py-clob-client` (live mode only, imported lazily in `executor.py`).

**Note on TDD:** Most tasks write plain text files (README, LICENSE, .gitignore, requirements.txt) — no runtime code, no unit tests. Task 8 is the end-to-end verification (fresh venv + `pip install` + `python -c "import bot"`). That is the test for this plan.

**Permission note for operator:** `.env.example` is in a path that Claude Code tools are blocked from reading (alongside `.env`). Task 6 requires the operator to either share `.env.example` contents or perform the audit comparison themselves.

---

## File Structure

| Path | Responsibility |
|---|---|
| `README.md` | Single-file landing: overview, per-OS install, config, run, architecture, features, troubleshooting, license |
| `LICENSE` | MIT, copyright 2026 UtkarshGupta0 |
| `.gitignore` | Secrets, Python build artifacts, venvs, logs, IDE/OS noise, internal planning docs |
| `requirements.txt` | Loose-ranged pins for aiohttp, websockets, requests, python-dotenv, py-clob-client |
| `.env.example` | (Possibly modified) Every env var in `config.py` appears as a commented placeholder |

All other files (`.py`, `docs/HANDOVER/`, `docs/superpowers/specs/`, `tests/`, `scripts/`, `POLYMARKET_BOT_GAMEPLAN.md`, `dashboard.py`) are untouched.

---

## Task 1: Pre-flight secret scan

**Goal:** Before adding any file, verify nothing already tracked contains a real private key or wallet address that would be exposed on first push.

**Files:**
- No file creation/modification.
- Read-only: every tracked file under repo root.

- [ ] **Step 1: Scan tracked files for private-key-shaped strings**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git ls-files | xargs grep -l -iE 'private_key|privatekey' 2>/dev/null
```

Expected: hits in `config.py`, `POLYMARKET_BOT_GAMEPLAN.md`, `docs/HANDOVER/03_configuration.md` (these reference the env var **name**, not values — OK).

Fail if any hit is a literal hex key (64 chars of `[a-f0-9]` prefixed by `0x`).

- [ ] **Step 2: Scan for hex wallet addresses and private keys**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git ls-files | xargs grep -E '0x[a-fA-F0-9]{40}|0x[a-fA-F0-9]{64}' 2>/dev/null
```

Expected: either no output, or hits that are clearly placeholder examples like `0x0000...0000` or `0xYourFunderAddress`. Any real-looking 40- or 64-char hex must be replaced or the file added to .gitignore before proceeding.

- [ ] **Step 3: Scan docs for any accidental credential paste**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git grep -iE '(api[_-]?key|secret|password|token)' -- '*.md' | grep -v -iE 'your_|example|placeholder|<|\[|TELEGRAM_BOT_TOKEN|ANTHROPIC_API_KEY' | head -30
```

Review hits. A line like `TELEGRAM_BOT_TOKEN=<your token here>` is fine. A line containing actual Bot API token digits (e.g., `123456789:ABC-XYZ...`) is not. If found, escalate to operator for removal before proceeding.

- [ ] **Step 4: Commit a note so subsequent tasks know this was done**

No file change. Just record in commit message that the scan was run.

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore: pre-flight secret scan clean

Scanned tracked files for private keys, wallet addresses, API tokens.
No real credentials found in tracked content.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Create `.gitignore`

**Goal:** Prevent secrets, caches, logs, and internal planning docs from ever being staged.

**Files:**
- Create: `/home/utk/polymarket-btc-bot/.gitignore`

- [ ] **Step 1: Write the `.gitignore` file**

Create `/home/utk/polymarket-btc-bot/.gitignore` with exactly this content:

```gitignore
# Secrets — NEVER commit
.env
.env.save
.env.save.*
*.key
*.pem

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/

# Virtual environments
venv/
.venv/
env/
ENV/

# Runtime data
logs/
*.log

# Internal planning
docs/superpowers/plans/

# IDE / OS
.vscode/
.idea/
*.swp
*.swo
.DS_Store
Thumbs.db
desktop.ini
```

- [ ] **Step 2: Verify pattern coverage**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git check-ignore -v .env logs/paper_run_20260424_0220.log __pycache__/config.cpython-314.pyc docs/superpowers/plans/2026-04-24-gate-validation.md
```

Expected: every path listed, each with the matching .gitignore line number. If any path is not ignored, the pattern is wrong — fix it.

- [ ] **Step 3: Commit**

```bash
cd /home/utk/polymarket-btc-bot
git add .gitignore
git commit -m "$(cat <<'EOF'
chore: add .gitignore

Blocks secrets (.env*, *.key, *.pem), Python build artifacts,
virtualenvs, logs, and internal planning docs from being committed.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Purge cached and log artifacts from tracking

**Goal:** Remove items that are currently tracked (or will be on next `git add`) but should be `.gitignore`-d going forward. Files stay on disk; only the git-tracked copy is removed.

**Files:**
- `git rm --cached` against: `__pycache__/*.pyc`, `.pytest_cache/`, `logs/`, `docs/superpowers/plans/`
- No source code changes.

- [ ] **Step 1: List currently tracked items that should be ignored**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git ls-files | grep -E '^(__pycache__|\.pytest_cache|logs|docs/superpowers/plans)/' | head -30
```

Expected: list of .pyc files, any cached logs/, any committed plan files. Record which paths exist — the next step uses this.

- [ ] **Step 2: Remove cached Python bytecode from tracking**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git rm -r --cached __pycache__/ 2>/dev/null || echo "not tracked, skipping"
git rm -r --cached .pytest_cache/ 2>/dev/null || echo "not tracked, skipping"
```

Expected: either `rm '__pycache__/bot.cpython-314.pyc'` style output for each file, or "not tracked, skipping".

- [ ] **Step 3: Remove tracked logs from tracking**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git rm -r --cached logs/ 2>/dev/null || echo "not tracked, skipping"
```

Expected: list of `rm 'logs/xxx'` lines, or "not tracked, skipping". The log files remain on disk — only git stops tracking them.

- [ ] **Step 4: Remove tracked planning docs from tracking**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git rm -r --cached docs/superpowers/plans/ 2>/dev/null || echo "not tracked, skipping"
```

Expected: list of `rm 'docs/superpowers/plans/xxx.md'` lines, or "not tracked, skipping".

- [ ] **Step 5: Confirm no tracked file remains that matches ignore patterns**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git ls-files | grep -E '^(__pycache__|\.pytest_cache|logs|docs/superpowers/plans)/'
```

Expected: no output. If anything prints, the earlier step missed it — rerun the matching `git rm --cached` step.

- [ ] **Step 6: Commit**

```bash
cd /home/utk/polymarket-btc-bot
git commit -m "$(cat <<'EOF'
chore: untrack cached bytecode, logs, and internal plans

git rm --cached for __pycache__, .pytest_cache, logs/, and
docs/superpowers/plans/. Files remain on disk but are now honored
by .gitignore patterns added in the previous commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Create `LICENSE`

**Goal:** MIT license with operator's copyright line — enables others to legally clone/use.

**Files:**
- Create: `/home/utk/polymarket-btc-bot/LICENSE`

- [ ] **Step 1: Write the LICENSE file**

Create `/home/utk/polymarket-btc-bot/LICENSE` with exactly this content:

```
MIT License

Copyright (c) 2026 UtkarshGupta0

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: Verify**

Run:
```bash
cd /home/utk/polymarket-btc-bot
head -3 LICENSE
wc -l LICENSE
```

Expected: header shows `MIT License`, then blank, then `Copyright (c) 2026 UtkarshGupta0`. Line count is 21.

- [ ] **Step 3: Commit**

```bash
cd /home/utk/polymarket-btc-bot
git add LICENSE
git commit -m "$(cat <<'EOF'
chore: add MIT LICENSE

Copyright 2026 UtkarshGupta0.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Create `requirements.txt`

**Goal:** Capture third-party deps with loose version ranges so `pip install` works on Mac/Windows/Linux + Python 3.10–3.14.

**Files:**
- Create: `/home/utk/polymarket-btc-bot/requirements.txt`

Background — imports surveyed via `grep '^import\|^from' *.py`:
- `aiohttp` — `price_feed.py`, `market_finder.py`, `dashboard.py`
- `websockets` — `price_feed.py`
- `requests` — `backtester.py`
- `dotenv` — `config.py` (optional via try/except, but include for reliability)
- `py_clob_client` — `executor.py` line 183 (lazy import inside live-mode branch)

- [ ] **Step 1: Write the requirements.txt file**

Create `/home/utk/polymarket-btc-bot/requirements.txt` with exactly this content:

```
# Core runtime
aiohttp>=3.9,<4.0
websockets>=12.0,<14.0
requests>=2.31,<3.0
python-dotenv>=1.0,<2.0

# Live trading (Polymarket CLOB client)
# Lazily imported in executor.py — only required when TRADING_MODE=live.
py-clob-client>=0.17,<1.0
```

- [ ] **Step 2: Verify deps resolve in a fresh venv**

Run:
```bash
python3 -m venv /tmp/test_req_venv
source /tmp/test_req_venv/bin/activate
pip install --quiet -r /home/utk/polymarket-btc-bot/requirements.txt
python -c "import aiohttp, websockets, requests, dotenv, py_clob_client; print('deps OK')"
deactivate
rm -rf /tmp/test_req_venv
```

Expected: `deps OK`. If `pip install` fails, the version ranges need adjustment (usually the upper bound on websockets or aiohttp on macOS M1 / Windows).

- [ ] **Step 3: Commit**

```bash
cd /home/utk/polymarket-btc-bot
git add requirements.txt
git commit -m "$(cat <<'EOF'
chore: add requirements.txt

Loose-ranged pins for aiohttp, websockets, requests, python-dotenv.
py-clob-client is only needed for live trading (lazily imported).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Audit and update `.env.example`

**Goal:** Ensure every env var read by `config.py` has a commented placeholder in `.env.example`. Missing entries mean new users hit `AssertionError` at startup without knowing which var to set.

**Files:**
- Possibly modify: `/home/utk/polymarket-btc-bot/.env.example`

**Permission note:** Claude Code tools cannot read `.env*` files in this repo. The operator must run the comparison manually or paste `.env.example` contents into the conversation so the agent can compare.

**Env vars from `config.py` (full list — source of truth):**

```
TRADING_MODE                (default: "paper")
POLYMARKET_PRIVATE_KEY      (no default)
POLYMARKET_FUNDER_ADDRESS   (no default)
POLYMARKET_SIGNATURE_TYPE   (default: 1)
STARTING_CAPITAL            (default: 30.0)
MAX_BET_SIZE                (default: 5.0)
MIN_BET_SIZE                (default: 1.0)
MIN_CONFIDENCE              (default: 0.50)
ENTRY_WINDOW_START          (default: 45)
ENTRY_WINDOW_END            (default: 8)
MAX_DAILY_DRAWDOWN          (default: 5.0)
MAX_CONSECUTIVE_LOSSES      (default: 5)
MIN_RESERVE                 (default: 5.0)
KELLY_FRACTION              (default: 0.25)
REPRICE_INTERVAL_SEC        (default: 5)
KELLY_ENABLE_AFTER          (default: 100)
GATE_ASK_MIN                (default: 0.15)
GATE_ASK_MAX                (default: 0.92)
MIN_EDGE                    (default: 0.02)
MIN_DELTA_PCT               (default: 0.0)
CANCEL_FAIL_THRESHOLD       (default: 3)
PAPER_HOLD_ON_EDGE_LOSS     (default: 1)
SIGNAL_TREND_ENABLED        (default: 1)
TRADING_HOURS_BLOCK         (default: empty)
DASHBOARD_ENABLED           (default: 1)
DASHBOARD_HOST              (default: "127.0.0.1")
DASHBOARD_PORT              (default: 8787)
TELEGRAM_BOT_TOKEN          (no default)
TELEGRAM_CHAT_ID            (no default)
ANTHROPIC_API_KEY           (no default)
BINANCE_WS_URL              (default: "wss://stream.binance.com:9443/ws/btcusdt@aggTrade")
BINANCE_REST_URL            (default: "https://api.binance.com")
GAMMA_API_URL               (default: "https://gamma-api.polymarket.com")
CLOB_API_URL                (default: "https://clob.polymarket.com")
```

- [ ] **Step 1: Operator reads `.env.example` and lists which vars appear**

Operator runs:
```bash
cd /home/utk/polymarket-btc-bot
grep -oE '^[A-Z_]+=' .env.example | sort -u
```

Output captured. Compare to the 34-var list above.

- [ ] **Step 2: Identify missing entries**

For any var in the 34-var list that is not in the `.env.example` output, it is missing and must be added.

- [ ] **Step 3: Append missing vars to `.env.example`**

If Step 2 found missing vars, append a block to `.env.example`. Example entry format:

```
# Minimum confidence (0-1) required to post an entry
MIN_CONFIDENCE=0.55

# Minimum edge (confidence - ask) required to enter. Disabled if 0.
MIN_EDGE=0.02
```

Use the default from the list above as the example value. For secrets (`POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, etc.), use an empty value or a placeholder like `<your-key-here>`.

If Step 2 found no gaps, skip this step.

- [ ] **Step 4: Verify `.env.example` does not contain real secrets**

Run:
```bash
grep -E '0x[a-fA-F0-9]{40}|0x[a-fA-F0-9]{64}' /home/utk/polymarket-btc-bot/.env.example
```

Expected: no output. If anything prints, replace with a placeholder like `0x0000000000000000000000000000000000000000` immediately.

- [ ] **Step 5: Commit (if changes were made)**

Only if Step 3 modified `.env.example`:

```bash
cd /home/utk/polymarket-btc-bot
git add .env.example
git commit -m "$(cat <<'EOF'
chore: audit .env.example against config.py

Add missing env var placeholders so a fresh clone can populate .env
without guessing which vars are required.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

If Step 3 made no changes, skip the commit.

---

## Task 7: Write `README.md`

**Goal:** Single-file landing page that takes a reader from "what is this?" to running paper mode locally in ~10 minutes, with separate clear instructions for Linux, macOS, and Windows.

**Files:**
- Create: `/home/utk/polymarket-btc-bot/README.md`

- [ ] **Step 1: Write the README file**

Create `/home/utk/polymarket-btc-bot/README.md` with exactly this content:

````markdown
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
| `KELLY_FRACTION` | `0.25` | Fractional-Kelly multiplier (applies after warmup) |
| `KELLY_ENABLE_AFTER` | `100` | Trades required before Kelly sizing kicks in |
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
````

- [ ] **Step 2: Verify markdown renders and links resolve**

Run:
```bash
cd /home/utk/polymarket-btc-bot
head -5 README.md
wc -l README.md
# Verify linked docs exist
for f in docs/HANDOVER/01_architecture.md docs/HANDOVER/02_strategy.md docs/HANDOVER/03_configuration.md docs/HANDOVER/04_operations.md docs/HANDOVER/05_maintenance.md docs/HANDOVER/06_development.md docs/HANDOVER/07_going_live.md LICENSE; do
  test -f "$f" && echo "OK: $f" || echo "MISSING: $f"
done
```

Expected: all "OK" lines, no "MISSING". Line count roughly 200-250.

- [ ] **Step 3: Commit**

```bash
cd /home/utk/polymarket-btc-bot
git add README.md
git commit -m "$(cat <<'EOF'
docs: add README.md

Single-file landing page: overview, per-OS install (Linux/macOS/Windows),
configuration reference, run instructions for paper and live modes,
architecture, features, testing, troubleshooting, links to HANDOVER docs.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Fresh-install verification

**Goal:** Prove a clean checkout from the current working tree can actually be installed and imported. This is the end-to-end test for the plan.

**Files:**
- No repo changes.
- Temp venv at `/tmp/polybtc_verify_venv`.

- [ ] **Step 1: Create a throwaway clean checkout**

Run:
```bash
rm -rf /tmp/polybtc_verify
git clone /home/utk/polymarket-btc-bot /tmp/polybtc_verify
ls /tmp/polybtc_verify/
```

Expected: tree matches the source. In particular: `README.md`, `LICENSE`, `.gitignore`, `requirements.txt`, `.env.example` all present. No `logs/`, no `__pycache__/`, no `docs/superpowers/plans/`.

- [ ] **Step 2: Create a fresh venv and install deps**

Run:
```bash
python3 -m venv /tmp/polybtc_verify_venv
source /tmp/polybtc_verify_venv/bin/activate
pip install --quiet -r /tmp/polybtc_verify/requirements.txt
```

Expected: no errors. `pip` reports successful installs of aiohttp, websockets, requests, python-dotenv, py-clob-client and their transitive deps.

- [ ] **Step 3: Copy the example env and import all bot modules**

Run:
```bash
cd /tmp/polybtc_verify
cp .env.example .env
python -c "
import bot
import config
import price_feed
import signal_engine
import executor
import risk_manager
import market_finder
import trade_logger
import telegram_alerts
import dashboard
import backtester
import self_improver
print('all modules import OK')
"
```

Expected: `all modules import OK`. No ImportError, no ModuleNotFoundError.

- [ ] **Step 4: Run the test suite**

Run:
```bash
cd /tmp/polybtc_verify
for f in tests/test_*.py; do
  echo "=== $f ==="
  python "$f" || { echo "FAIL: $f"; break; }
done
```

Expected: every test file prints a `PASS` line and no `FAIL`. If any test fails in this fresh-install environment but passes in the source tree, there is a missing dep or path assumption — fix in the source tree and re-run this task.

- [ ] **Step 5: Cleanup**

Run:
```bash
deactivate
rm -rf /tmp/polybtc_verify /tmp/polybtc_verify_venv
```

- [ ] **Step 6: Record verification as an empty commit**

No file change. Record that verification passed.

```bash
cd /home/utk/polymarket-btc-bot
git commit --allow-empty -m "$(cat <<'EOF'
chore: fresh-install verification passed

Cloned current working tree into a temp directory, created a fresh
venv, installed requirements.txt, imported all runtime modules, and
ran the test suite. All green.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final secret scan and push-readiness check

**Goal:** One last sweep right before pushing. Catches any leakage introduced during the previous tasks.

**Files:**
- No changes. Read-only verification.

- [ ] **Step 1: Secret scan across tracked content**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git grep -iE '0x[a-fA-F0-9]{40}|0x[a-fA-F0-9]{64}' -- '*.py' '*.md' '*.txt' '*.example' '*.json' 2>/dev/null
```

Review every hit. Allowed: placeholder-looking values (`0x0000...`, `0x1111...`, `0xYourFunderAddress`). Not allowed: anything that looks like a real wallet (mixed case hex, non-trivial digits).

- [ ] **Step 2: Confirm .env and logs are not tracked**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git ls-files | grep -E '^(\.env$|logs/|__pycache__|\.pytest_cache|docs/superpowers/plans/)'
```

Expected: no output. If `.env` or any ignored path is still tracked, go back to Task 3.

- [ ] **Step 3: Confirm required files are tracked**

Run:
```bash
cd /home/utk/polymarket-btc-bot
for f in README.md LICENSE .gitignore requirements.txt .env.example; do
  git ls-files --error-unmatch "$f" >/dev/null 2>&1 && echo "OK: $f" || echo "MISSING: $f"
done
```

Expected: all `OK`, no `MISSING`.

- [ ] **Step 4: Confirm clean working tree**

Run:
```bash
cd /home/utk/polymarket-btc-bot
git status --short
```

Expected: output is empty (or contains only untracked files that are in `.gitignore`, which git will suppress anyway). Any `M`/`A`/`D` line means uncommitted work — commit or reset before pushing.

- [ ] **Step 5: Print push-ready summary for the operator**

Run:
```bash
cd /home/utk/polymarket-btc-bot
echo "=== Ready to push ==="
echo "Remote target: git@github.com:UtkarshGupta0/polymarket-btc-bot.git"
echo "Current branch: $(git branch --show-current)"
echo "Commits ahead: $(git rev-list --count HEAD)"
echo ""
echo "Operator next steps (run manually):"
echo "  1. Create empty public repo at https://github.com/new (name: polymarket-btc-bot)"
echo "  2. git remote add origin git@github.com:UtkarshGupta0/polymarket-btc-bot.git"
echo "  3. git push -u origin master   # or 'main' if you renamed the branch"
```

This is the final task. After this, the operator creates the GitHub repo and pushes. Do NOT push automatically — creating a remote and pushing to someone's account is an action the operator must take themselves.

---

## Self-review against spec

Cross-checked the spec sections against this plan:

| Spec section | Plan task(s) |
|---|---|
| Create `README.md` | Task 7 |
| Create `LICENSE` | Task 4 |
| Create `.gitignore` | Task 2 |
| Create `requirements.txt` | Task 5 |
| Audit `.env.example` | Task 6 |
| Remove caches/logs/plans from tracking | Task 3 |
| Verification workflow (secret scan, purge, fresh install, test suite, push) | Tasks 1, 3, 8, 9 |
| README 18-section structure | Task 7 Step 1 |
| `.gitignore` content | Task 2 Step 1 |
| LICENSE content | Task 4 Step 1 |
| `requirements.txt` content (aiohttp, websockets, requests) | Task 5 Step 1 (plus python-dotenv and py-clob-client surfaced by config/executor imports) |
| Success criterion: stranger can clone and run | Task 8 (fresh-install verification tests exactly this) |
| Risk: missing env var in example | Task 6 (audit) |
| Risk: wheel missing on platform | Task 5 Step 2 (fresh venv resolve) + README troubleshooting section |
| Risk: leaked secret in docs | Tasks 1, 9 (secret scans before and after) |

No gaps. No placeholders in the plan. Type/name consistency: `UtkarshGupta0` used in Task 4 matches spec; all file paths absolute or git-root-relative as called for.

---

## Rollback

Each task is a standalone commit. To rollback any task N, `git revert <sha-of-task-N-commit>`. To rollback the whole publication prep, `git reset --hard <sha-before-task-1>` in a fresh session (destructive — confirm no uncommitted work first).
