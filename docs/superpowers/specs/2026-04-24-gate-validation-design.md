# Gate Validation Analysis — Design Spec

**Date:** 2026-04-24
**Author:** operator + Claude
**Status:** design approved, awaiting implementation plan

## Problem

Three new gates shipped today, informed by `self_improver.py` analysis of 166 paper trades (Apr 18–23):

1. `MIN_CONFIDENCE = 0.60` (was 0.50)
2. `MIN_EDGE = 0.03` (was 0.02)
3. `TRADING_HOURS_BLOCK = 0,2,3,20,21` (new)
4. `MIN_DELTA_PCT = 0.0002` (new)

The recommendations were derived from the same 166 trades we'd be retrodicting against. Pure retro-simulation confirms self-consistency but cannot prove the gates generalize. We need a one-shot validation analysis that combines three complementary views to estimate whether the gates will help, hurt, or overfit.

## Goal

Produce a single throwaway script that reads existing `logs/trades_*.json`, applies the new gates counterfactually, and prints three analyses. The operator uses the output to decide whether to trust the gates on the running paper bot, or to back off any of them before the next regime.

## Non-goals

- Persisting the tool in the repo long-term (if we want persistence, we promote to `validate_gates.py` at the repo root later — not now).
- Modifying bot behavior. Analysis only.
- Claiming statistical rigor over n=166 with known regime non-stationarity. This is operator decision support, not a paper.

## Scope

One file: `scripts/validate_gates.py`. Standalone. No imports of the bot's runtime modules except `config.py` for gate thresholds (for a single source of truth).

Runs as:

```
python scripts/validate_gates.py
```

Prints tables to stdout. No file output, no JSON, no plots.

## Input

All files matching `logs/trades_2026*.json` under the repo root. Each file has shape:

```
{"session": {...}, "trades": [ {trade_record}, ... ]}
```

Only trades with `outcome ∈ {WIN, LOSS}` are included. `SKIPPED` and `UNFILLED` outcomes are excluded (they carry no win/loss signal).

## Per-trade field mapping

| Gate | Field(s) used | Exact computation |
|---|---|---|
| `MIN_CONFIDENCE` | `confidence` | `confidence >= 0.60` |
| `MIN_EDGE` | `confidence`, `entry_price` | `confidence - (entry_price + 0.01) >= 0.03` (reconstruct `side_ask = entry_price + 0.01` since live code does `target = ask - 0.01`) |
| `TRADING_HOURS_BLOCK` | `time_iso` | `datetime.fromisoformat(time_iso).astimezone(timezone.utc).hour NOT IN {0,2,3,20,21}` |
| `MIN_DELTA_PCT` | `delta_pct` | `abs(delta_pct) >= 0.0002` — **proxy** (see caveats) |

All thresholds read from `config.CONFIG` at script start, not hardcoded — so the script reflects whatever the operator has set in `.env`.

## Analyses

### A) Counterfactual stack (all 4 gates applied together)

Before/after table:

```
              n     WR       PnL      $/trade
----------------------------------------------
BEFORE      166   65.1%  +$126.33     $+0.76
AFTER        XX   YY.Y%    $+ZZZ.ZZ   $+A.AA
REMOVED      XX   YY.Y%    $-WWW.WW
```

The **REMOVED** row is the critical diagnostic: if WR(removed) < 50% and PnL(removed) negative, gates are killing losers → good. If WR(removed) ≈ WR(kept), gates are killing at random → no signal, possibly harmful (you trade less without accuracy improvement).

### B) Regime-split generalization test

Partition trades by day into two buckets:
- **train** = Apr 19–21 (n≈113, WR 71%, strong regime)
- **test** = Apr 22–23 (n≈52, WR 54%, degraded regime)

Apply the 4-gate stack to each slice independently. Report a 2×3 table:

```
               BEFORE              AFTER              DELTA
slice      n    WR    PnL      n    WR    PnL      Δn    ΔWR    ΔPnL
-------------------------------------------------------------------------
train    113  71.7% +$132.78   ...  ...   ...     ...   ...    ...
test      52  53.8%   -$4.15   ...  ...   ...     ...   ...    ...
```

Hypothesis check: if `ΔWR(train) >> ΔWR(test)` or `ΔPnL(test) ≤ 0`, the gates are overfitting to the easy regime. If the test slice shows meaningful improvement (even small n), the gates have a shot.

Apr 18 (n=1) is excluded from both slices (too small, and pre-regime).

### C) Per-gate marginal attribution

For each gate alone and each meaningful combination (4 individual + 4 combinations to cover all-but-one = 8 rows), report:

```
gate                     removed_n  removed_WR  kept_WR  WR_delta   signal_quality
-----------------------------------------------------------------------------------
MIN_CONFIDENCE≥0.60          58        56.9%     69.3%   +12.4pp    ✅ kills losers
MIN_EDGE≥0.03                XX         Y%        Z%      ...
TRADING_HOURS_BLOCK=...      XX         Y%        Z%      ...
MIN_DELTA_PCT≥0.0002         XX         Y%        Z%      ...
all 4 stacked                XX         Y%        Z%      ...
MIN_CONFIDENCE+MIN_EDGE      XX         Y%        Z%      ...
MIN_CONFIDENCE+HOURS         XX         Y%        Z%      ...
HOURS+MIN_DELTA              XX         Y%        Z%      ...
```

`signal_quality` heuristic column:
- `✅ kills losers` if `removed_WR < kept_WR - 5pp` AND `removed_n ≥ 5`
- `⚠️ noisy` if `|removed_WR - kept_WR| < 5pp`
- `❌ kills winners` if `removed_WR > kept_WR + 5pp`
- `?` if `removed_n < 5` (too thin to judge)

## Output format

Plain UTF-8 text to stdout. Fixed-width columns using Python f-string padding. No dependencies beyond stdlib. No color codes, no emojis (except the 3 signal_quality markers above which are allowed because the user runs this script — not auto-generated docs).

## Caveats (must be printed in the script output footer)

1. **MIN_DELTA_PCT uses resolution delta, not entry-time delta.** The trade record has `btc_open` (window open) and `btc_close` (window close), but no BTC price at the moment of entry. The live gate filters on `state.delta_from_open` at T-N seconds, which is strictly smaller in magnitude than the final `delta_pct`. So this retrodiction **over-estimates** how many trades the gate would have let through. To fix precisely, parse `signal | delta=X%` log lines near each trade's `placed_at`. Not in scope for this throwaway.
2. **MIN_EDGE reconstructs ask as `entry_price + 0.01`.** Live code does `target = ask - 0.01`, so inverting gives that. Error: ±1 cent, since reprice loop can land on older asks. For aggregate analysis this is fine.
3. **Gates were derived from the same 166 trades.** Analysis A is self-consistent by construction. Analysis B is the only real out-of-sample check, and its "test" slice is n=52 — one regime, possibly one bad news event. Treat results as directional, not conclusive.
4. **Apr 22–23 regime contamination.** The recent 2 days may be a transient (news, low liquidity) or a permanent shift. Analysis B cannot distinguish. Re-run the script after 1 more week of paper data.

## Success criteria for the operator decision

After running this script, the operator has enough to answer:

- **Keep all 4 gates?** Yes if Analysis A shows REMOVED rows losing money AND Analysis B test slice improves.
- **Drop a specific gate?** Yes for any gate flagged `❌ kills winners` or `⚠️ noisy` in Analysis C.
- **Hold and observe?** If Analysis B test slice is flat or ambiguous (small n effect). Run bot with current gates, re-validate in 7 days.

## Out of scope

- Statistical significance tests (sample size is too small; pointless ceremony).
- Backtester integration (backtester can't synthesize book/volume imbalance — already confirmed blind spot for conf > 0.57).
- Comparison against a "no-gates" control running in parallel (requires forward data, not retrodiction).
- Persisting as a repo tool. If we find ourselves rerunning this script weekly, we promote it later.

## Files created

- `scripts/validate_gates.py` (new, ~150 LOC)
- (No edits to existing code.)

## Rollback

`rm scripts/validate_gates.py`. Nothing else depends on it.
