# 07 — Going Live

This is the checklist for flipping from paper to live USDC trading. Do not skim.

## Prerequisites

1. **Paper track record.** At least 100 resolved paper trades (i.e. past the flat-bet validation phase). WR ≥ 55% and aggregate PnL > 0 over the last 7 days.
2. **Backtest agreement.** Last `backtester.py --days 30` WR within ±5 points of paper WR. Large divergence means one of the two is lying — diagnose before risking money.
3. **USDC on Polygon.** Polymarket settles in USDC on Polygon (not Ethereum). Bridge from CEX or via a Polygon USDC swap. Minimum useful balance: $30. The bot's `MIN_SHARE_SIZE=5` and `GATE_ASK_MIN=0.15` imply `$0.75` as the smallest single trade ($5 × $0.15) — below ~$10 bankroll, you'll struggle to size meaningfully.
4. **Signature type decided.** See below.

## Signature types

Polymarket has three wallet arrangements. `POLYMARKET_SIGNATURE_TYPE` must match yours:

### Type 0 — EOA direct

Your private key is directly the trading wallet. No funder address. USDC balance lives on that EOA. Rare for retail since it means you hand-manage the key.

```env
TRADING_MODE=live
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=
```

### Type 1 — Email/Magic proxy (most web-UI users)

You logged in with email; Polymarket manages a proxy wallet for you. The private key you export (Account → Settings → Export Private Key) signs orders; the funder is the separate proxy address that actually holds USDC and trades.

```env
TRADING_MODE=live
POLYMARKET_SIGNATURE_TYPE=1
POLYMARKET_PRIVATE_KEY=0x...           # the exported signing key
POLYMARKET_FUNDER_ADDRESS=0x...        # the proxy wallet address
```

### Type 2 — MetaMask Gnosis-Safe proxy

You deposited via MetaMask. Polymarket created a Gnosis Safe you control; USDC lives in the Safe; MetaMask signs on the Safe's behalf.

```env
TRADING_MODE=live
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_PRIVATE_KEY=0x...           # MetaMask account key
POLYMARKET_FUNDER_ADDRESS=0x...        # the Safe address
```

To find your funder address: in the Polymarket UI, go to Wallet → you'll see a "Polygon address" that's *not* your MetaMask — that's the Safe/proxy.

## Connection test (no orders)

Before any real orders, verify the CLOB client connects cleanly:

```bash
python tests/test_live_connect.py
```

This imports `LiveExecutor`, constructs the client, and calls `get_usdc_balance()`. If it prints a balance, your creds are correct. If it prints `balance=None` but doesn't error, the client connected but balance read failed — usually a signature type mismatch. If it errors on construction, the private key is wrong.

## The `.env` diff from paper

Starting from a working paper `.env`:

```diff
-TRADING_MODE=paper
+TRADING_MODE=live
+POLYMARKET_PRIVATE_KEY=0x<your_key>
+POLYMARKET_FUNDER_ADDRESS=0x<your_funder>       # omit for sig_type=0
+POLYMARKET_SIGNATURE_TYPE=1                      # match your wallet type
-PAPER_HOLD_ON_EDGE_LOSS=1
+PAPER_HOLD_ON_EDGE_LOSS=0
```

Set `PAPER_HOLD_ON_EDGE_LOSS=0` explicitly for live. The code already ignores this flag when mode is live, but having it off in the env makes the intent unambiguous.

## What changes at runtime

Same orchestration, different executor. `build_executor()` returns `LiveExecutor` when `TRADING_MODE=live`. The interface is identical: `place_order`, `reprice`, `resolve_trade`, `cancel_pending_if_unfilled`, `pending_trade`, `forget_window`.

Behavioral differences:

1. **`place_order` actually submits.** Builds `OrderArgs(price, size, side=BUY, token_id)`, signs it via `client.create_order`, posts via `client.post_order(..., OrderType.GTC)`. On success, `trade.order_id` is populated and `trade.status` reflects the CLOB's response (typically `LIVE`).
2. **`resolve_trade` checks fill status first.** Calls `refresh_trade_status` which hits `client.get_order(order_id)`. Three outcomes:
   - Status in `{"MATCHED", "FILLED"}` → compare BTC close to open, mark WIN/LOSS, compute PnL.
   - Status in `{"LIVE", "PLACED"}` → we had an unfilled order at resolution; cancel it, mark `UNFILLED`/`SKIPPED`, refund the reserved size (payout = size so balance is unchanged).
   - Anything else (`CANCELLED`, `REJECTED`) → treat as skipped.
3. **Cancel actually cancels.** `cancel_order(order_id)` hits `client.cancel(order_id)`. `cancel_all()` at shutdown catches anything lingering.
4. **T-3s sweep runs.** Since `paper_hold` is effectively off in live, any still-unfilled order at T-3s is cancelled and refunded.

## First live run — minimum-money test

Don't jump to full bankroll. Transfer the smallest meaningful amount (e.g. $10 USDC) to the trading wallet. Set:

```env
STARTING_CAPITAL=10.0
MAX_BET_SIZE=1.5
MIN_BET_SIZE=1.0
MAX_DAILY_DRAWDOWN=3.0
```

Run for 24-48 hours. You want to see:

1. First order: `[live] PLACED window=... status=LIVE order_id=...` with a real hex order ID.
2. Resolution: either `[live] RESOLVED` (filled) or `[live] window=... not filled; refund $X` (unfilled GTC).
3. Cancel: if gate fails persistently, `[live] cancelled unfilled order ...`.
4. Balance reconciliation: `summary()` balance line should track with the actual on-chain USDC balance. They should match to the cent. If they don't, something isn't refunding correctly.

Once you've seen all three patterns work cleanly, scale `STARTING_CAPITAL` up to your real capital.

## Order lifecycle — `PaperExecutor` vs `LiveExecutor`

| Event | Paper | Live |
|---|---|---|
| `place_order` | assume immediate fill at entry_price. `status=FILLED`. | submit GTC buy. `status=LIVE` until filled. `order_id` populated. |
| `reprice` | drop old active, re-`place_order` at new price | cancel old via `cancel_order`, then re-submit |
| `pending_trade(window_ts)` | returns `Trade` from in-memory dict | same |
| `cancel_pending_if_unfilled` | drops from active map, sets `status=CANCELLED` | refreshes status first; if still `LIVE/PLACED`, cancels; sets `status=CANCELLED` |
| `resolve_trade` | compare BTC close, compute PnL on `size_usdc` | refresh status; if not filled, refund as SKIPPED; else compute PnL |
| Bot crash mid-window | no effect, nothing persisted | order still live on Polymarket; you must cancel manually |

## CLOB API quirks to know about

- **`MIN_SHARE_SIZE = 5.0`.** Polymarket's CLOB rejects orders for fewer than 5 shares. `executor.place_order` guards against this. At $0.90 entry, that's a $4.50 minimum size — which is why `MIN_BET_SIZE` should stay at $1.00+ (let flat-bet's `max(min_bet_size, 5*entry+0.05)` produce the real minimum).
- **Price must be in multiples of $0.01.** Rounded in `place_order`. Passing 0.905 produces a rejection.
- **Prices 0 < p < 1.** Exactly 0 and exactly 1 are rejected as degenerate.
- **`OrderType.GTC` is the only maker path.** `FOK` and `GTD` exist but make you a taker. Do not change this without understanding the fee implications.
- **Rate limits.** CLOB is lenient for single-user order flow (we're well under any limit). Gamma API is the noisier hit — mostly reads, but we fetch an orderbook per reprice tick.

## Live-mode risk gates (beyond paper)

Paper halts are real but cheap. Live halts matter. All of these are defaults; review each for your bankroll:

- `MAX_DAILY_DRAWDOWN=5.0` — with $30 capital that's 17% per day. For $100+ capital, tighten absolutely (e.g. $10 = 10%).
- `MAX_CONSECUTIVE_LOSSES=5` — 5 losses at $5 size = $25 bleed before pause. Scale down if `MAX_BET_SIZE` grows.
- `MIN_RESERVE=5.0` — can_trade() returns False below this. Don't let the bot drain to zero.
- Restart clears all of these. In live mode, restart = you have a real reason (you checked the market, you've decided a halt was a false positive). Don't restart casually.

## Watching a live run

```bash
tail -f logs/paper_run_$(ls logs/ | grep paper_run | tail -1)
```

(The file is still named `paper_run_...` in live mode because the naming was never updated — it just pipes stdout. The mode inside the log is correct.)

Red flags in live-specific logs:

- `[live] order placement failed: <exception>` — CLOB rejected. Usually MIN_SHARE_SIZE, invalid price, or insufficient allowance.
- `[live] order response suspicious: ...` — response came back but no `orderID`. Polymarket API change or auth failure.
- `[live] cancel <order_id> error` — order cancel failed. Usually means it already filled between your decision and the cancel hit. Next tick's `resolve_trade` will sort it out.
- `[live] window=X not filled; refund $Y` — GTC sat in the book through resolution. Refund keeps balance honest but that's a trade you intended to take and didn't. If this is >30% of trades, your bot is placing too low a limit or the market's moving through your price — try quoting at `ask - 0.005` instead of `ask - 0.01`.

## Stopping a live run

Ctrl-C. The SIGINT handler triggers `_shutdown()` which:

1. Closes the dashboard.
2. Calls `executor.cancel_all()` — Polymarket API-level cancel of every resting order for this account.
3. Closes WebSockets and HTTP sessions.
4. Writes daily summary.
5. Sends Telegram shutdown alert.

Don't `kill -9` unless unavoidable. That skips `cancel_all` and leaves live orders on Polymarket.

## Post-shutdown verification

Before walking away from a stopped live bot:

1. Check Polymarket UI → your positions → no resting limit orders in markets the bot was trading.
2. Check USDC balance on the funder address matches the bot's reported `balance_end`.
3. If either disagrees, investigate *before* restarting. Common cause: a fill happened between the last `refresh_trade_status` and `cancel_all` and wasn't logged.

## Never-do-in-live list

- Never `PAPER_HOLD_ON_EDGE_LOSS=1`. The code guards it but the env value being wrong is a smell.
- Never run two instances against the same funder. `_traded_windows` dedup is per-process.
- Never skip the minimum-money test. First live run = $10 max.
- Never commit `.env`. Your private key is in there.
- Never `cancel_all` via direct API while the bot is running — the bot's `_active_trades` dict won't know, and next tick it'll try to reprice/resolve a ghost.
- Never raise `MAX_BET_SIZE` mid-run. Restart with the new value; don't edit `.env` and hope.
- Never trust the first 10 live trades. The PaperExecutor assumes immediate fill; LiveExecutor reality includes queue position, partial fills, unfilled-at-close. First 10 live trades are calibration.
