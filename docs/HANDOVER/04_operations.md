# 04 — Operations

## Start / stop

### Basic

```bash
cd /home/utk/polymarket-btc-bot
python bot.py
```

That's the whole thing. Ctrl-C triggers a clean shutdown (SIGINT handler is installed in `_install_signal_handlers`): it writes the daily summary, closes Binance WSs, sends a Telegram shutdown message, prints the session report to stdout.

### Under tmux (recommended for long runs)

```bash
tmux new -s bot
cd /home/utk/polymarket-btc-bot
python bot.py
# Ctrl+b d to detach; bot keeps running
tmux attach -t bot      # to reattach
```

If you're logging somewhere durable and don't need live tailing, pipe:

```bash
python bot.py 2>&1 | tee logs/paper_run_$(date +%Y%m%d_%H%M).log
```

### Logs

- `logs/trades_YYYYMMDD.json` — resolved trades, one file per UTC day, appended atomically.
- `logs/daily_summary_YYYYMMDD.json` — rollup written on shutdown and on UTC-day rollover.
- `logs/paper_run_YYYYMMDD_HHMM.log` — if you piped stdout yourself (nothing auto-writes these).
- `logs/bt_trend_*.{json,log}` — backtest artifacts (see `backtester.py`).

## The dashboard

With `DASHBOARD_ENABLED=1` (default), open `http://127.0.0.1:8787/` in a browser. It polls `/state` every second and renders:

- BTC: current price, window open, delta, VWAP, momentum, vol imbalance, book imbalance, realized vol, tick count.
- Signal: direction, confidence bar, suggested price, EV, per-feature alignment bars, time boost, vol multiplier.
- Market: slug, token IDs (truncated), best bid/ask each side, gate pass/fail.
- Pending trade: if any.
- Risk: balance, daily PnL, W/L, streak, drawdown flag, session totals.
- Recent trades: last 20, with timestamps, outcomes, PnL.

No auth. Localhost only by default.

## Telegram alerts

If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, you get:

- **Startup:** `🤖 Bot started — mode=PAPER capital=$30.00`
- **Trade placed:** `🎯 BTC 5m | UP @ $0.91 | $1.00 | conf 65% | T-22s`
- **Trade resolved:** `✅ WON +$0.10 | bal $30.10 | WR 100% (1/1) | streak 0L`
- **Risk paused:** `⚠️ Bot paused: daily drawdown limit hit ($-5.23)`
- **Daily summary:** sent at UTC-midnight rollover with the day's rollup.
- **Shutdown:** `🛑 Bot stopped` + the final session report text.

All sent via direct Bot API `sendMessage`. Silent failures logged at WARNING — no retry.

Test with `python telegram_alerts.py` — sends a "test message" to the configured chat.

## Scheduled tasks (none automated)

There is no cron job, no timer. You run the bot; it does everything inside its own loop. The "daily summary" is written on UTC-midnight rollover *while the bot is running*, or on shutdown. If the bot is not running at midnight UTC, that day's summary is not written — the next run will fold those trades into whatever day its next `_maybe_rollover_day` triggers on.

## Network / egress requirements

- Outbound to `stream.binance.com:9443` (WS, aggTrade + depth5). Kept alive via 20s ping.
- Outbound to `api.binance.com:443` (HTTPS, REST fallback for first-tick price).
- Outbound to `gamma-api.polymarket.com:443` (market lookup).
- Outbound to `clob.polymarket.com:443` (orderbook + in live mode, order submission).
- Outbound to `api.telegram.org:443` if alerts are on.
- Outbound to `api.anthropic.com:443` only when you run `self_improver.py`.

Campus networks that block WebSockets will silently brick the bot — you'll see `WS disconnected` warnings and no tick processing. Mobile hotspot or a tunnel (tailscale, ssh -L) is the escape hatch.

## Checking it's healthy

### Via log

```bash
tail -f logs/paper_run_*.log
```

Healthy patterns:
- `Connected to Binance WS` + `Connected to Binance depth WS` on startup.
- `Window opened ts=<ts> open_price=$<p>` every 5 min at `:00`, `:05`, `:10`, etc.
- `signal | dir=... conf=...` every ~5s while inside the entry window.
- Either `gate_fail | reason=...` (most common in efficient markets) or `PLACED ... conf=X size=$Y` followed by `POST-RESOLVE | bal=... daily: pnl=... WR=...`.

Unhealthy:
- No `Window opened` for >5 min → loop hung; check for an uncaught exception near the top.
- `WS disconnected` repeating with no tick processing between → check egress.
- `market fetch error for ts=...` repeating → Gamma API rate-limit or your token-resolution is broken (see [05_maintenance.md](05_maintenance.md)).
- `already traded window X, skipping` → dedup working; not an error unless you see it for every window.

### Via dashboard

Best single view. `Market` card shows `gate_vs_market: PASS` (green) or `fail` (grey) in real time — tells you instantly whether the bot *could* trade if it wanted.

### Via Telegram

If alerts are on, the startup message confirms mode + capital. Subsequent silence is normal (most windows skip). A daily summary at UTC midnight confirms the bot was alive through the whole day.

## Restarting

`python bot.py` again. The bot has no persistent risk state between runs — restart clears `consecutive_losses`, `max_drawdown_hit`, `daily_pnl`, `daily_trades`, and rebuilds `current_balance` from `STARTING_CAPITAL`. Historical trades in `logs/trades_*.json` persist.

This means: **restarting is a valid operator override for a drawdown halt**. It is not a bug. It is also the reason you should not restart casually — you lose the day's running P&L accounting.

## Upgrading / deploying changes

No build step. Edit `.py`, restart. There's no hot reload.

Standard flow:

```bash
# 1. make your change
# 2. run relevant test file
python tests/test_reprice_logic.py
# 3. in another terminal / tmux pane, stop current bot (Ctrl-C)
# 4. restart
python bot.py
```

For config-only changes (`.env` edits), the same — there's no reload, restart is required.

## Backtest workflow

See `backtester.py`. Typical:

```bash
python backtester.py --days 30              # default gate
python backtester.py --days 30 --ignore-ev  # trade on conf alone
python backtester.py --days 7 --dist        # show confidence distribution
python backtester.py --days 30 --save out.json
```

Caveats baked in by the tool: no real orderbook (entry at `confidence_to_price` curve), no volume imbalance (klines lack buy/sell split), 1m resolution so trend-slope is an approximation. It's good for directional A/B of parameter changes; trust the *ordering* of results, not the absolute WR/PnL.

## Weekly review

```bash
python self_improver.py --dry-run           # see the payload
python self_improver.py --days 7            # call Claude, get suggestions
```

Prints aggregate stats and an advisory analysis. It does NOT mutate any config — the output is for you to read and manually edit `.env` if you agree.
