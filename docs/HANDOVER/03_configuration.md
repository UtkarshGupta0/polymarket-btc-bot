# 03 — Configuration

All config lives in `.env` at the repo root. `config.py` loads it into a frozen dataclass and validates on startup; invalid values raise on `import config`.

## Loading

`config.py` uses `python-dotenv` if installed, otherwise reads the process env. Missing values fall back to defaults (defined in `load_config()`). The `validate()` method runs assertions on startup — if you see `AssertionError` on boot, it's a `.env` value out of range.

## Modes

### `TRADING_MODE`

- Values: `paper` (default) | `live`
- `live` requires `POLYMARKET_PRIVATE_KEY` and, if `POLYMARKET_SIGNATURE_TYPE != 0`, `POLYMARKET_FUNDER_ADDRESS`. Validated on startup — bot won't boot with missing creds in live mode.

### `POLYMARKET_SIGNATURE_TYPE`

- 0 = EOA direct (private key is the trading wallet, no funder needed)
- 1 = email/Magic proxy (funder = proxy wallet address)
- 2 = MetaMask Gnosis-Safe proxy (funder = Safe address)

If you deposited via the Polymarket web UI with email login, you're signature_type 1 and your funder is the proxy address shown in the account settings. If you deposited via MetaMask, it's type 2.

## Trading params (the knobs you'll actually touch)

### `MIN_CONFIDENCE` (default `0.50`)

Floor on signal confidence before the edge gate even runs. Below this, no trade, regardless of ask. Raising it reduces trades, slightly raises WR, slightly increases gate-fail frequency (because we filter signals before we even check the market). Don't drop below 0.45 — noise takes over.

### `MIN_EDGE` (default `0.02`)

Required `confidence - side_ask`. The single most load-bearing number in the whole system. Drop it to 0.01 and you'll trade 3x more, most of those incremental trades are coin-flips. Raise it to 0.05 and you may stop trading entirely in efficient regimes. See `backtester.py --ignore-ev` output to recalibrate.

### `GATE_ASK_MIN` / `GATE_ASK_MAX` (defaults `0.15` / `0.92`)

Sanity bounds on the opposite-side ask. Below `GATE_ASK_MIN` usually indicates a stale or malformed book (or we're trying to buy a token that's already 99% certain — no profit to capture). Above `GATE_ASK_MAX` the implied loss on a miss is huge; skip. Do not widen these without a specific reason.

### `ENTRY_WINDOW_START` / `ENTRY_WINDOW_END` (defaults `45` / `8`)

Seconds-remaining bounds for gate evaluation. Trade fires only when `ENTRY_WINDOW_END < seconds_remaining <= ENTRY_WINDOW_START`. The asymmetry matters: we can enter at 45s, we cannot enter after 8s. Widening END (e.g. to 5) puts you in crowded last-second competition. Narrowing START (e.g. to 30) sacrifices signal-true opportunities earlier in the window.

### `REPRICE_INTERVAL_SEC` (default `5`)

How often the entry loop re-evaluates (rechecks signal, fetches ask, decides place/reprice/cancel). Lower = more API calls to Polymarket, faster reaction to ask moves. Don't go below 3 — you'll hammer Gamma and your ask-stale race gets worse.

### `CANCEL_FAIL_THRESHOLD` (default `3`)

Consecutive gate-fail reprice ticks before cancel. With `REPRICE_INTERVAL_SEC=5`, that's 15s of sustained edge-loss before we pull the order. Prevents whipsaw cancels on single-tick noise.

### `MIN_BET_SIZE` / `MAX_BET_SIZE` (defaults `1.0` / `5.0`)

Dollar bounds on position size. `MIN_BET_SIZE` is the absolute floor (also used by flat-bet phase). `MAX_BET_SIZE` caps Kelly's suggestion. For a $30 bankroll, $5 max is ~17% per trade — already aggressive. Don't raise until bankroll is 10x this.

### `KELLY_FRACTION` (default `0.25`)

Multiplier on theoretical Kelly. 1.0 = full Kelly (volatile); 0.25 = quarter-Kelly (industry-standard conservative). Only take effect after `KELLY_ENABLE_AFTER` resolved trades.

### `KELLY_ENABLE_AFTER` (default `100`)

Number of total resolved trades before Kelly replaces flat-bet sizing. The flat-bet phase validates the signal on identical variance. Don't lower this — Kelly compounds an unvalidated signal's errors brutally.

### `MAX_DAILY_DRAWDOWN` (default `5.0`)

Dollar cap on one day's losses. Once `daily_pnl <= -5.0`, `max_drawdown_hit=True` and no new trades until next UTC day or restart. This *is* 17% of starting capital — intentionally tight for paper validation. Raise to 10% once Kelly is on and the signal is proven.

### `MAX_CONSECUTIVE_LOSSES` (default `5`)

Loss streak pause. Resets to 0 on any win. At 5 losses in a row, stop — something regime-shifted and we're bleeding. This is a circuit breaker, not a statistical limit.

### `MIN_RESERVE` (default `5.0`)

Dollar floor below which `can_trade()` returns False. Prevents the bot from spending to zero when bets don't add up to meaningful sizes.

## Feature flags

### `PAPER_HOLD_ON_EDGE_LOSS` (default `1`)

- `1` in paper mode: suppress both the N-strike cancel and the T-3s sweep on gate failures. Existing position rides to window resolution regardless of edge. Gives honest P&L stats.
- `0` in paper: mirror live cancel behavior for testing.
- **Never `1` in live.** Paper cancels are free; live cancels of winning positions via `paper_hold` would mean leaving capital at risk on an edge we no longer believe in. The bot logic already guards this (only applies in paper mode) but don't flip the flag thinking.

### `SIGNAL_TREND_ENABLED` (default `1`)

Toggles the 2-minute trend-slope feature. `1` uses the new 7-feature weight set with `W_TREND=0.15`. `0` uses the legacy 6-feature weights with `W_TREND=0` redistributed. Off-parity is unit-tested — disabling cleanly restores pre-trend behavior.

### `MIN_DELTA_PCT` (default `0.0`, i.e. off)

Minimum absolute window-delta required at entry. When >0, `gate_vs_market` rejects signals whose `abs(signal.window_delta) < MIN_DELTA_PCT`. Data-driven: live WR on <0.01% prior-move windows ≈52% (coin-flip — noise-regime). Suggested: `0.0002` (0.02%). Gate-fail reason logged as `delta_too_low(+x.xxx%)`. Bounds: `[0, 0.01)`. Leave at 0 until you've verified the deltas you're filtering out actually correlate with low WR in YOUR data.

### `TRADING_HOURS_BLOCK` (default empty)

Comma-separated UTC hours (0–23) in which the bot will NOT place new orders. Example: `TRADING_HOURS_BLOCK=0,2,3,20,21`. Existing active trades still flow through reprice/cancel normally — only new entries are blocked. Driven by per-hour WR data (`self_improver.py`): overnight/late-US liquidity regimes where signal degrades. Leave empty (default) to trade all hours. Validated on startup — out-of-range hours raise `AssertionError`.

## Infrastructure

### `DASHBOARD_ENABLED` / `DASHBOARD_HOST` / `DASHBOARD_PORT` (defaults `1` / `127.0.0.1` / `8787`)

Local aiohttp server at `http://host:port/` serving the HTML UI (polls `/state` every 1s). Localhost-only by default — bind to `0.0.0.0` only if you know what you're doing.

### `BINANCE_WS_URL` / `BINANCE_REST_URL`

Defaults are `wss://stream.binance.com:9443/ws/btcusdt@aggTrade` and `https://api.binance.com`. Change only for testnet work or if Binance blocks your egress.

### `GAMMA_API_URL` / `CLOB_API_URL`

Defaults `https://gamma-api.polymarket.com` and `https://clob.polymarket.com`. These are public endpoints; no key required for reads. Orders go through the CLOB client SDK using your private key.

## Alerts / analysis

### `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`

Both required for alerts. Create a bot via @BotFather, get the token; your chat ID via @userinfobot. If either is blank, the bot logs `Telegram alerts: disabled` at startup and sends nothing. No error, just silent.

### `ANTHROPIC_API_KEY`

Used only by `self_improver.py` CLI. Not needed for bot runtime.

## Defaults sanity table

| var | default | tested safe range |
|---|---|---|
| `MIN_CONFIDENCE` | 0.50 | 0.45–0.70 |
| `MIN_EDGE` | 0.02 | 0.01–0.05 |
| `GATE_ASK_MIN` | 0.15 | 0.10–0.20 |
| `GATE_ASK_MAX` | 0.92 | 0.88–0.95 |
| `ENTRY_WINDOW_START` | 45 | 30–60 |
| `ENTRY_WINDOW_END` | 8 | 5–15 |
| `REPRICE_INTERVAL_SEC` | 5 | 3–10 |
| `CANCEL_FAIL_THRESHOLD` | 3 | 2–5 |
| `MAX_DAILY_DRAWDOWN` | 5.0 | 3.0–10.0 (scale with capital) |
| `MAX_CONSECUTIVE_LOSSES` | 5 | 3–7 |
| `KELLY_FRACTION` | 0.25 | 0.10–0.40 |
| `KELLY_ENABLE_AFTER` | 100 | 50–200 |

Anything outside those ranges either has no effect (saturates against another gate) or enters undertested territory — run backtests first.

## Worked example: `.env` for paper validation

```env
TRADING_MODE=paper
STARTING_CAPITAL=30.0
MAX_BET_SIZE=5.0
MIN_BET_SIZE=1.0
MIN_CONFIDENCE=0.50
ENTRY_WINDOW_START=45
ENTRY_WINDOW_END=8
MAX_DAILY_DRAWDOWN=5.0
MAX_CONSECUTIVE_LOSSES=5
KELLY_FRACTION=0.25
KELLY_ENABLE_AFTER=100
MIN_RESERVE=5.0
REPRICE_INTERVAL_SEC=5
GATE_ASK_MIN=0.15
GATE_ASK_MAX=0.92
MIN_EDGE=0.02
MIN_DELTA_PCT=0.0
CANCEL_FAIL_THRESHOLD=3
PAPER_HOLD_ON_EDGE_LOSS=1
SIGNAL_TREND_ENABLED=1
TRADING_HOURS_BLOCK=
DASHBOARD_ENABLED=1
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
```

For live, flip `TRADING_MODE=live`, set `PAPER_HOLD_ON_EDGE_LOSS=0`, and fill in `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER_ADDRESS` + `POLYMARKET_SIGNATURE_TYPE`. See [07_going_live.md](07_going_live.md).
