# 05 — Maintenance

## Monitoring checklist (daily)

Check the daily summary JSON or the Telegram message:

```json
{
  "day_utc": "2026-04-19",
  "trades_resolved": 8,
  "wins": 5,
  "losses": 3,
  "win_rate": 0.625,
  "pnl": 1.24,
  "balance_end": 31.24,
  "consecutive_losses_end": 1
}
```

Green flags:
- `trades_resolved` steady (5–20 per day is normal — most windows gate-fail).
- `win_rate` drifting toward 0.55–0.65.
- `consecutive_losses_end` < 3.
- `balance_end` trending up over weekly windows, not any single day.

Red flags:
- `trades_resolved=0` for multiple days → gate or market data problem, see "zero trades."
- `win_rate < 0.45` with n > 20 → signal degraded vs backtest; review features.
- `consecutive_losses_end >= 4` → regime shift; check if we're in a known chop zone (news, low-vol overnight).
- `balance_end` below 80% of `STARTING_CAPITAL` → stop, review, consider backtest-driven reparametrization.

## Common failures

### "Zero trades placed today"

Diagnosis order:

1. **Bot was running?** Check `ps aux | grep bot.py` and log tails. If not running, that's it.
2. **Market data was resolving?** Grep log for `market <ts>: up_tok=set down_tok=set ...`. If all lines show `missing`, Gamma API isn't returning the slug — see "market fetch" below.
3. **Signals were being computed?** Grep for `signal | dir=`. If absent, the price feed isn't populating — check `Binance WS` connect lines.
4. **Gate was firing but failing?** Grep for `gate_fail | reason=`. If every line says `reason=edge_low`, the market is efficient — WR ≈ ask everywhere. This is the expected "working" state in quiet regimes. Not a bug.
5. **Risk halt?** Grep for `daily drawdown limit hit` or `consecutive loss streak`. If present, bot is paused until UTC midnight or restart.

If gate_fail shows `reason=ask_missing`: orderbook hasn't fetched. Bot waits, retries every reprice tick.

If gate_fail shows `reason=ask_too_low`: book crashed to extreme (e.g. side went from 0.40 to 0.03 instantly). Usually means we're on the losing side already; correct to skip.

If gate_fail shows `reason=ask_too_high`: we're trying to buy at >$0.92 implied — losing R:R. Skipping is correct.

### Market fetch failures (`market fetch error for ts=...`)

The Gamma API returns the market slug via `/events?slug=btc-updown-5m-<ts>`. Two failure modes:

1. **Slug not yet indexed.** Polymarket creates the market at T-0 but indexing lags ~30s. The bot falls back to `_fallback_recent_markets` (lists recent crypto markets and greps for the timestamp). If the fallback also misses, the window is skipped.
2. **Gamma outage.** Hit `https://gamma-api.polymarket.com/events` in a browser. If it 5xx's, wait. Polymarket's infra is mostly reliable but isolated blips happen.

No retry loop is programmed — the bot just tries again at the next window. If this is persistent for multiple consecutive windows, something upstream changed (slug format? endpoint moved?) and you need to update `market_finder.build_slug` or the Gamma URL.

### WS disconnects (`WS disconnected: ...`)

Binance aggTrade stream occasionally drops. The reconnect loop in `PriceFeed._run` is 2 seconds. You'll see a `WS disconnected` → `Connected to Binance WS` pair within seconds. Multiple consecutive disconnects without successful ticks in between = egress problem (firewall, DNS, ISP blocking WS). Confirm by running:

```bash
python price_feed.py  # standalone test — prints 30s of tick data
```

If that's broken, the bot will be too.

### Drawdown halt

`daily_pnl <= -MAX_DAILY_DRAWDOWN` sets `RiskState.max_drawdown_hit=True`. After that:

- `can_trade()` returns `False` with reason `daily drawdown limit hit`.
- Bot still ticks, fetches data, computes signals, logs `gate_fail` style — just doesn't place orders.
- Telegram `⚠️ Bot paused: ...` sent once (flag-gated to prevent spam).
- Clears on UTC midnight (`_maybe_reset_day` resets everything) OR on bot restart (in-memory state is rebuilt from `STARTING_CAPITAL`).

**Restart semantics are deliberate.** If you hit drawdown mid-day and you *have a reason* to believe the regime changed (news event passed, spike subsided), restarting clears the halt. This is an operator override. If you don't have a reason, don't restart — the halt exists because the signal is disagreeing with reality.

### Consecutive loss streak halt

Same pattern, different trigger. `MAX_CONSECUTIVE_LOSSES` consecutive losing trades → pause. Resets to 0 on any win. Clears on restart. Less sticky than drawdown — usually self-heals within 1-2 winning trades once the regime rotates.

### Stale `pending_trade`

If a trade gets placed but the resolution logic misses it (e.g. bot crashed between place and resolve), the next startup will have no memory of it. Live mode: the order is still on Polymarket; `executor.cancel_all()` on next shutdown or manual UI intervention is your recourse. Paper mode: nothing persists, no-op.

To prevent: don't Ctrl-C during the 2s resolution delay (`RESOLVE_DELAY_SECONDS`). Wait until you see `POST-RESOLVE` before stopping.

### Dashboard "fetch err"

Bot is down (or `DASHBOARD_ENABLED=0`). The JS in `dashboard.py` polls `/state` every 1s and shows the error inline. Not a bot problem per se.

### Telegram silence with creds set

Check `logs/paper_run_*.log` for `telegram HTTP` warnings. Common causes:
- Token revoked (regenerate via @BotFather).
- Bot blocked by your user (unblock in the chat).
- Wrong chat ID (verify with @userinfobot in a DM with the bot itself).

## Gate-fail triage

Gate fails are normal. Over a typical 24h you'll see 90%+ of windows fail the gate. What matters is *why*. Log pattern:

```
gate_fail | dir=UP conf=0.62 ask=0.70 reason=edge_low(-0.08)
```

- `reason=edge_low(X)` with X near zero → signal is accurate but market already priced it. Expected.
- `reason=edge_low(X)` with X strongly negative → signal is below market. Likely a late-window reversal the bot hasn't caught. Worth a grep to see if these cluster in specific hours.
- `reason=ask_missing` → orderbook empty. Polymarket markets can go dark in early-window or low-liquidity regimes. Skipping is correct.
- `reason=ask_too_high` → mostly appears on dominant-direction windows where the "winning" side went to $0.95+ immediately. We gave up the entry window.
- `reason=ask_too_low` → you're trying to trade the wrong side. Either the signal is inverted or the market already has the winning side at $0.03.

If you want quantitative triage, grep last N days:

```bash
grep gate_fail logs/paper_run_*.log | grep -oP 'reason=\K\w+' | sort | uniq -c
```

Healthy distribution is roughly 70% `edge_low`, 15% `ask_too_high`, 10% `ask_missing`, 5% other.

## Rolling back a change

All meaningful changes are feature-flagged or one-commit-revert-safe.

- **Turn off the trend feature:** set `SIGNAL_TREND_ENABLED=0`. No restart-needed? Actually yes, restart needed — config is loaded once at boot. After restart, legacy 6-feature weights take over. Unit-tested for parity at slope=0.
- **Turn off paper_hold:** set `PAPER_HOLD_ON_EDGE_LOSS=0`. Restart. Bot reverts to "cancel at 3 strikes + T-3s sweep" behavior.
- **Raise the edge bar:** `MIN_EDGE=0.03`. Cuts trade volume, should raise average WR.
- **Disable entries entirely without disabling the bot:** set `ENTRY_WINDOW_START=0`. The condition `ENTRY_WINDOW_END < seconds_remaining <= 0` is never true, so no trades, but the bot keeps ticking, logging, and serving the dashboard.
- **Revert a code change:** `git log` / `git revert <sha>` / restart. Nothing else depends on repo state beyond the `.py` files and `.env`.

## Health recovery procedures

### After a hard crash

1. `tmux ls` — is a zombie process still holding port 8787? `tmux kill-session -t bot`.
2. `lsof -i :8787` — anything listening? Kill it.
3. Start fresh: `tmux new -s bot; python bot.py`.
4. Watch for `Connected to Binance WS` and first signal within 30s.

### After multiple consecutive drawdown halts

Signal has degraded. Do NOT just raise `MAX_DAILY_DRAWDOWN` and keep going. Instead:

1. Run `python self_improver.py --days 7`.
2. Run `python backtester.py --days 7 --dist` — compare confidence distribution vs what you saw a month ago.
3. Consider `SIGNAL_TREND_ENABLED=0` or raising `MIN_EDGE` as a regime-adapting response.
4. If that doesn't help, drop to paper mode for a week; don't burn more live capital on a broken signal.

## What not to do

- **Don't edit `logs/*.json` by hand.** The JSON is atomic-write but load-on-startup; corruption breaks daily summaries.
- **Don't skip `validate()` asserts.** If `.env` loading asserts, the value is out of range — fix the value, don't catch the assert.
- **Don't run two bot instances against the same Polymarket account in live mode.** `_traded_windows` dedup is per-process; two processes = double orders.
- **Don't commit `.env`.** It's gitignored; check before every push. Contains your private key in live mode.
- **Don't set `PAPER_HOLD_ON_EDGE_LOSS=1` and `TRADING_MODE=live` simultaneously.** The code guards it (only respects `paper_hold` when mode is paper) but the intent is conflicting — clean that up in the flag before running live.
