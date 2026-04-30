# 02 — Strategy

## The edge thesis

There are three stacked advantages, in descending order of importance:

1. **Information latency.** Binance tick stream is sub-100ms. Polymarket's price discovery has to wait for traders to react, and in the last 30-60 seconds of a 5-min window BTC direction is largely determined even while Polymarket's odds still sit at $0.88–0.94 instead of their fair $0.97+. The bot exploits the gap between "what Binance already tells us" and "what the market has priced in."

2. **Maker fee structure.** On Polymarket, maker orders pay zero fees; takers pay up to 1.8% at 50% probability bins. A taker needs ~51.5% win rate to break even. A maker filling at $0.90 needs only ~47% (plus the daily maker rebate). Every order the bot places is a resting GTC limit buy (`OrderType.GTC`) priced one cent below the current ask, so it sits in the book waiting for someone else to cross — never lifts the offer.

3. **Time decay.** As window close approaches, token prices should converge to $0.00 or $1.00 but don't, because liquidity thins. Orders placed at T-15s to T-30s capture this convergence inefficiency.

## The signal

`signal_engine.compute_signal()` reads `PriceState` and returns a `Signal(direction, confidence, suggested_price, EV, rationale)`. The composite is a weighted sum of **a base delta-confidence term** and **six alignment terms**, each in the range [-1, +1] meaning "does this feature confirm or contradict the dominant direction from delta."

| Feature | Weight (flag ON) | What it measures |
|---|---|---|
| `base_delta_conf * delta_dir` | 0.25 | Magnitude of price move from window open, mapped via `DELTA_CURVE` (signed by delta direction) |
| `momentum_alignment` | 0.10 | `momentum / 5.0` clamped to [-1, +1], signed by delta direction |
| `volume_alignment` | 0.10 | Buy vs sell volume imbalance, signed |
| `vwap_alignment` | 0.10 | Price above/below 120s VWAP, normalized by 0.05%, signed |
| `velocity_alignment` | 0.12 | 30-second price velocity, normalized by 0.05%, signed |
| `book_alignment` | 0.18 | Binance depth5 `(bid - ask)/total`, signed |
| `trend_alignment` | 0.15 | 2-min least-squares slope, normalized by 1e-6, signed |

Weights sum to 1.0. The `SIGNAL_TREND_ENABLED=0` path uses a parallel legacy weight set (`_W_*_OFF`) that excludes `W_TREND` and redistributes to the other six — preserving pre-trend behavior bit-for-bit when toggled.

### The delta curve

The raw `|delta_from_open|` → confidence map is a piecewise-linear interp:

| `|delta|` | confidence |
|---|---|
| <0.005% | 0.10 (noise floor) |
| 0.01% | 0.20 |
| 0.02% | 0.35 |
| 0.05% | 0.50 |
| 0.10% | 0.65 |
| 0.20% | 0.75 |
| 0.50% | 0.85 |
| 1.00% | 0.92 |

Extends flat beyond the endpoints. Deltas above 1% don't produce higher confidence than 0.92 — they just hit the cap. This keeps tail events from blowing up sizing.

### Multipliers on top

After the composite is taken (absolute value) as `confidence`:

- **Time boost** — confidence *= 1.08 at T-30s, 1.15 at T-20s, 1.25 at T-10s. Reflects the increasing deterministic-ness of direction as close approaches.
- **Volatility regime** — `vol_mult = clamp(REALIZED_VOL_BASELINE / realized_vol, 0.85, 1.15)`. Low vol = sticky trend = boost; high vol = chop = damp. Damping range is deliberately narrow; this is a second-order correction, not a regime filter.
- **Cap** — `CONFIDENCE_CAP = 0.95`. No signal ever reads as "certain."

### From confidence to order price

`confidence_to_price()` maps confidence to a target limit price via a piecewise-linear curve: 0.55→$0.88, 0.65→$0.90, 0.75→$0.92, 0.85→$0.93, 0.95→$0.95. That's the *theoretical* maker price if we weren't edge-gating. In practice the bot quotes one cent *below the current ask* regardless of `suggested_price`, because that's where a maker actually sits and gets filled.

## The edge gate

`signal_engine.gate_vs_market(sig, ask_up, ask_down)` is the *real* decision point. Four conditions must all hold:

1. `seconds_to_close >= 5` — don't chase stubs at the edge of resolution.
2. `signal.confidence >= MIN_CONFIDENCE` (default 0.50).
3. Side-specific ask in `[GATE_ASK_MIN, GATE_ASK_MAX]` = [0.15, 0.92]. Below 0.15 is usually a stale/malformed book; above 0.92 the risk/reward is garbage.
4. **`signal.confidence - side_ask >= MIN_EDGE`** (default 0.02).

That last one is the whole game. Without it, the bot traded every `conf >= 0.50` fire and lost money to taker-priced quotes. With it, most fires skip — and that is the market being efficient. The bot takes fewer trades at higher average edge.

When you see `gate_fail` in the logs with `reason=edge_low(+0.01)` that means: we had a real signal but the market's ask already priced it in. Correct behavior. Do not "fix" this by lowering `MIN_EDGE`.

## The entry loop

Once per `REPRICE_INTERVAL_SEC` (default 5s) while inside the entry window:

```
sig = compute_signal(...)                     # fresh
if gate_vs_market fails:
    if no active trade: return                # just pass
    fails += 1
    if paper_hold_on_edge_loss: hold to close  # keep position
    elif fails >= CANCEL_FAIL_THRESHOLD (3):
        cancel + refund                        # real edge gone
    else: hold                                 # tolerate 1-2 tick noise

gate passed → clear fail counter
target_price = round(side_ask - 0.01, 2)       # one cent inside
if direction flipped vs active trade: cancel, return
size = risk_manager.calculate_position_size(...)
if no active: place
elif |active.entry_price - target| >= 0.01: reprice
else: leave it alone
```

This is in `bot.py::_evaluate_entry`. The hysteresis (N=3 fails before cancel) was added because single-tick gate failures were whipsawing paper cancels on signal noise.

## Sizing

All trades sized via fractional Kelly from trade 1. There is no separate flat-bet validation phase — `backtest_v2` is the validation surface.

```
b = (1 / entry_price) - 1         # profit/cost ratio at this price
kelly_pct = (b*p - q) / b         # p=confidence, q=1-p
size = (balance - MIN_RESERVE) * kelly_pct * KELLY_FRACTION
size = max(MIN_BET_SIZE, min(size, MAX_BET_SIZE))
```

`KELLY_FRACTION=0.25` is a conservative multiplier on the theoretical Kelly optimal — full Kelly is over-aggressive in any real game because `p` is estimated, not known.

After Kelly, the function checks `effective_shares = round(size / entry_price, 2)`. If `effective_shares < MIN_SHARE_SIZE` (Polymarket CLOB minimum, currently 5.0), the function returns 0 and emits a DEBUG log line. This makes the previously-silent executor reject visible: at small balances or high asks Kelly often produces fewer than 5 shares, and operators should see that explicitly rather than wonder why no trades are placing. Raise `STARTING_CAPITAL` or wait for cheaper asks to clear the threshold.

Why no flat-bet phase: the original purpose was to validate the signal on identical-size bets before letting Kelly compound variance. `backtest_v2` now plays that role on historical data, and the live flat phase had a silent-failure bug (flat $1 × ask > 0.20 produced fewer than 5 shares and was rejected by the executor). See `docs/superpowers/specs/2026-04-27-kill-flat-bet-phase-design.md`.

## Risk gates

`risk_manager.can_trade()` returns `False` on any of:

- `max_drawdown_hit` flag is set (persists until next UTC day or restart).
- `daily_pnl <= -MAX_DAILY_DRAWDOWN` (default: -$5.00). Sets `max_drawdown_hit=True`.
- `consecutive_losses >= MAX_CONSECUTIVE_LOSSES` (default: 5).
- `current_balance <= MIN_RESERVE` (default: $5.00).

All four are in-memory. Restart clears them — see [05_maintenance.md §drawdown-halt](05_maintenance.md#drawdown-halt) for why that's not a bug.

## EV check

`signal_engine.should_trade()` also requires `expected_value > 0`, where `EV = p * (1 - price) - (1 - p) * price`. This *should* be redundant with the edge gate (any positive edge gives positive EV), but it's cheap insurance against pricing-curve edge cases. Backtests use `--ignore-ev` because the `confidence_to_price` curve produces zero-edge quotes by construction.

## Why these specific choices

- **Why maker-only:** taker fees alone would eat a ~3% edge. Not viable at our win-rate range.
- **Why 5-min windows:** BTC direction is *predictable enough* over 5 min (noise-dominated at 1 min, mean-reverting over 15 min). 5 min is the sweet spot.
- **Why ENTRY_WINDOW_START=45s, END=8s:** before 45s, signal is too noisy; after 8s, queue position competition dominates — we'd be chasing last-second fills by bigger bots.
- **Why `trend_slope_2m`:** short-horizon features (delta, momentum, velocity) all see the same recent move and are highly correlated. Live logs showed the signal whipsawing near T-20s on 30s noise that looked directional short-term but was actually end-of-trend mean-reversion. The 2-min slope adds longer-horizon context. Behind `SIGNAL_TREND_ENABLED` so it can be disabled if paper P&L disagrees.
- **Why `PAPER_HOLD_ON_EDGE_LOSS=1` in paper:** real P&L is the only valid feedback on signal quality. Canceling on tick-noise gate-fails creates synthetic "SKIPPED" outcomes that tell us nothing. In paper we eat the occasional bad bet to get truthful statistics.

## What doesn't work (evidence)

From the backtest suite and 33-trade paper sample:

- **Higher confidence ≠ higher win rate.** The 80-90 bucket had 33% WR vs the 60-70 bucket at ~62%. There's a miscalibration cliff — likely overfitting on aligned features. A post-hoc derate on the 80+ bucket is a candidate improvement but not shipped.
- **Post-filtering by `trend_alignment` magnitude isn't predictive.** Paper data showed `aln>+0.5` at 65% WR (good) but `aln<-0.5` at 57% WR (above chance) — the signal correctly catches some end-of-trend mean-reversion, so dropping contradicting-aln trades would cost trades without improving WR. Trend stays as a composite feature, not a filter.
- **Kelly sizing on a signal with WR < 55% kills the bankroll.** The flat-bet validation phase exists because of this.
