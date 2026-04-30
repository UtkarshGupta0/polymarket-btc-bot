# Design — Contrarian-fade backtest experiment

**Date:** 2026-04-30
**Status:** Approved (awaiting plan)
**Front:** Profitability improvement — Front 2 (contrarian / fade extreme asks)

## Problem

Backtest_v2 (`docs/HANDOVER/08_backtest_v2.md`) demonstrated that the current direction-prediction signal has negative edge in real BTC 5-min Polymarket windows: average confidence 0.19 versus average ask 0.80, gate fires <1% of windows, and the few that pass at relaxed thresholds lose ~14% per trade.

Front 1 (signal redesign with book-aware fair-value residual) is the theoretically clean fix but is multi-week effort that depends on data infrastructure we do not yet have (Polymarket book snapshots are forward-only — Goldsky has fills, not book state).

Front 2 is a parallel cheap experiment that uses the **existing** Goldsky tape to test a different edge thesis: that the market systematically overprices the favourite at extreme asks, so flat-sized contrarian bets on the underdog at a price like $0.08 collect positive PnL per trade as variance harvest.

If Front 2 is positive, it produces a tradeable hypothesis without waiting for book data. If Front 2 is negative, that is strong evidence the 5-min market is too efficient at this horizon — and Front 1 likely will not save it either.

## Why this front, why now

- Same tape we already have. No new data source, no waiting.
- Single backtest pass per threshold; full sweep runs in seconds.
- Decision rule is sharp: per-trade PnL vs zero, on a sample of ≥30 trades. Cheap to disprove.
- Result either funds the next spec (live wiring) or kills the front and pivots to Front 1A or Front 5.

## Decision

Backtest-only experiment. Pure measurement. No `bot.py` wiring in this spec.

Add a new `SIGNAL_VARIANT=contrarian` path that, at each decision tick within the entry window:

1. Reads `ask_up, ask_down` from the tape (existing `ask_proxy_at`).
2. If `max(ask_up, ask_down) >= CONTRARIAN_ASK_THRESHOLD`: identify the **underdog** as the side opposite the high-ask favourite. Place a flat-sized buy on the underdog at its current ask.
3. Else: skip.

Flat-share sizing — exactly `MIN_SHARE_SIZE = 5` shares, USDC = `5 × underdog_ask`. Reasons:

- Kelly on small assumed edge × $30 bankroll yields sub-$1 sizes that get skipped by `MIN_BET_SIZE`. No measurement.
- Flat 5 shares keeps PnL per trade comparable across thresholds. Direct readout of edge.
- This is **measurement infrastructure**, not deployment. If edge proven, Kelly comes later in a separate spec.

Sweep `CONTRARIAN_ASK_THRESHOLD` over `{0.85, 0.88, 0.90, 0.92, 0.94}`. Per threshold, emit:

- `n_trades`
- `win_rate`
- `pnl_total`
- `pnl_per_trade`
- `market_implied_underdog_p` (1 − threshold, lower bound; the actual realised market average is reported separately)
- `edge_pp = win_rate − market_implied_underdog_p`

Decision rule (post-experiment):

- **If any threshold has `pnl_per_trade > 0` AND `n_trades >= 30`:** edge candidate. Move to a follow-up spec for live wiring + Kelly sizing.
- **If all thresholds negative or `n_trades < 30` everywhere:** thesis disproven. Pivot.

## Architecture

### New components

- **`signal_engine.compute_contrarian_signal(state, ask_up, ask_down) -> Optional[Signal]`** — separate function, not a branch inside `compute_signal`. Reasons: contrarian doesn't predict direction from PriceState features; it reacts to mispricing. Mixing the two would tangle responsibilities. Reuses the existing `Signal` dataclass for downstream interop (logging, executor).
- **Variant dispatch in `backtester_v2.replay`** — at the top of each decision tick, branch on `CONFIG.signal_variant`:
  - `"default" | "calibrated" | "regime_filtered" | "asymmetric"`: existing path.
  - `"contrarian"`: contrarian path (described below).

### Contrarian replay tick logic

Replaces the per-tick block in `backtester_v2.replay` (currently lines 298–382 of `backtester_v2.py`) with a variant-conditional path:

```
active = ex.pending_trade(row.window_ts)
if active is not None:
    continue                                # already placed this window — first gate-pass wins, no reprice

ask_up, fresh_up = ask_proxy_at(row.up_fills, t)
ask_down, fresh_down = ask_proxy_at(row.down_fills, t)

sig = compute_contrarian_signal(state, ask_up, ask_down)
if sig is None:
    continue                                # gate fails
if not rm.can_trade():
    continue                                # drawdown / streak / reserve halt

# Skip the existing Kelly path. Flat 5-share sizing.
target_price = sig.suggested_price
size = round(MIN_SHARE_SIZE * target_price, 2)

trade = ex.place_order(
    window_ts=row.window_ts,
    direction=sig.direction,
    confidence=sig.confidence,
    entry_price=target_price,
    size_usdc=size,
    token_id=row.token_up if sig.direction == "UP" else row.token_down,
    btc_open=row.btc_open,
    seconds_to_close=sec_remaining,
)
if trade is not None:
    rm.on_trade_placed(trade.size_usdc)
```

Resolution and PnL accounting unchanged — same `_was_fillable` + `on_trade_resolved` machinery.

### Contrarian signal semantics

`compute_contrarian_signal(state, ask_up, ask_down)` returns:

- `None` if `max(ask_up, ask_down) < CONTRARIAN_ASK_THRESHOLD` or either ask is `<= 0` (stale/missing).
- Otherwise a `Signal` with:
  - `direction`: side opposite the high ask (`"DOWN"` if `ask_up >= threshold` else `"UP"`).
  - `confidence`: `1.0 - favourite_ask` — purely the market-implied prior. We do not pretend to have a better point estimate; the *experiment itself* tests whether realised win rate exceeds this prior.
  - `suggested_price`: underdog's current ask (clamped ≥ 0.02 for placement).
  - `expected_value`: 0.0 by construction (price-implied prior). The experiment will report whether realised PnL diverges from EV=0.
  - `rationale`: e.g., `"contrarian: fav={UP|DOWN}@0.92 -> bet underdog at 0.08"`.

This intentionally collapses the directional features. Including them would conflate two hypotheses (mispricing exists *and* our directional signal helps); we want to test the first in isolation.

### Sweep script

New `scripts/run_contrarian_sweep.py`:

- Loop thresholds `[0.85, 0.88, 0.90, 0.92, 0.94]`.
- For each: monkey-patch `CONFIG.contrarian_ask_threshold` and `CONFIG.signal_variant = "contrarian"`, call `bt2.main(["--tape", ...])` or `bt2.replay(...)` directly to produce trades.
- Aggregate per threshold into a single summary JSON `logs/contrarian_sweep.json` with the schema:
  ```json
  {
    "thresholds": [
      {
        "threshold": 0.90,
        "n_trades": 42,
        "win_rate": 0.119,
        "pnl_total": -1.23,
        "pnl_per_trade": -0.029,
        "market_implied_underdog_p": 0.10,
        "edge_pp": 0.019
      }
    ],
    "tape_meta": { "windows": 238, "days": 3, "build_ts": "..." },
    "config_hash": "..."
  }
  ```

## Code changes

### `signal_engine.py`

- Add `compute_contrarian_signal(state: PriceState, ask_up: float, ask_down: float) -> Optional[Signal]` near `compute_signal`. Pure function — no module-level state.
- Update `_apply_variant`'s comment / docstring to note the contrarian variant takes a different code path entirely (does not call `_apply_variant`).

### `config.py`

- Add field `contrarian_ask_threshold: float` (default `0.90`).
- Extend `signal_variant` validator to accept `"contrarian"`.

### `.env.example`

- New entry `CONTRARIAN_ASK_THRESHOLD=0.90` documented in the variants section.

### `backtester_v2.py`

- Add variant dispatch at the top of the per-tick loop in `replay()`. Contrarian path bypasses `compute_signal` / `gate_vs_market` / `risk_manager.calculate_position_size` and uses flat 5-share sizing. Keep the existing path byte-equivalent for non-contrarian variants.
- Add `MIN_SHARE_SIZE` import (or re-export from existing import).

### `scripts/run_contrarian_sweep.py` (new)

- Loops the threshold list, runs replay per threshold, aggregates results into `logs/contrarian_sweep.json`. Prints a table to stdout for fast inspection.

### `tests/test_contrarian_signal.py` (new)

- `test_below_threshold_returns_none` — both asks below 0.90 → None.
- `test_high_up_ask_returns_down_signal` — ask_up=0.92, ask_down=0.08 → Signal(direction="DOWN", suggested_price=0.08, confidence≈0.08).
- `test_high_down_ask_returns_up_signal` — symmetric.
- `test_zero_ask_returns_none` — stale ask → None.
- `test_both_above_threshold_picks_higher_favourite` — degenerate data quirk; pick higher.

### Documentation

- New `docs/HANDOVER/09_contrarian_experiment.md`: brief description of the variant, the sweep, and how to interpret the output JSON.
- Append a section to `docs/HANDOVER/02_strategy.md` noting the contrarian variant exists for backtesting only and is not wired live yet.

## Acceptance

- `pytest tests/test_contrarian_signal.py` — 5/5 PASS.
- `python scripts/run_contrarian_sweep.py --tape data/btc_5m_tape.parquet --out logs/contrarian_sweep.json` exits 0 and produces a JSON conforming to the schema above.
- The summary JSON contains 5 threshold entries; each with `n_trades >= 0` and well-formed numeric fields.
- All existing tests still pass: `pytest tests/` shows no regressions.
- The default `signal_variant` (when `SIGNAL_VARIANT` is unset) remains `"default"` — no behavior change for non-contrarian users.
- `python backtester_v2.py --tape … --variant default` still produces byte-identical results (config_hash unchanged for non-contrarian configs).

## Risk + caveats

- **Sample size.** Tape has 238 windows × 3 days. At threshold 0.94 the contrarian gate may fire <5 times. The decision rule explicitly requires `n_trades >= 30`; if no threshold meets that, expand the tape (re-run `scripts/fetch_clob_trades.py` with more days) before deciding.
- **Survivorship.** Goldsky tape only includes markets that traded on chain. Skews liquid. Edge measured on this sample may not transfer to thinly-traded windows.
- **Fee sensitivity.** Run sweep with `--fee 0.0` (parity) and `--fee 0.002` (current taker fee). Both reported.
- **No book depth.** "Thin book" is a known risk multiplier for contrarian fades that we *cannot* measure with current data. The experiment ignores it; if positive, a follow-up spec must add a thin-book filter before live wiring.
- **One-shot per window.** First gate-pass within entry window places the trade; no repricing or cancellation logic. Different from the production direction-predict bot, but matches the experiment's intent (single bet, single outcome, clean PnL signal).
- **Variant interactions.** Setting `SIGNAL_VARIANT=contrarian` disables the existing direction-predict path entirely for the duration of that backtest run. The two variants are mutually exclusive at the variant level.

## Out of scope

- Live `bot.py` wiring of the contrarian variant.
- Kelly sizing for contrarian trades.
- Reprice / cancel logic mid-window.
- Book-depth-aware filtering.
- Combining contrarian with the existing direction-predict signal in a single trade decision.
- Forward-data tape collection (would need WebSocket book snapshots — that is Front 1A).

If the experiment passes the decision rule, those become subjects for a follow-up spec.
