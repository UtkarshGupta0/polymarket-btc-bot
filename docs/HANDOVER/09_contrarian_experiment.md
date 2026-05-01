# 09 · Contrarian-Fade Experiment

A backtest-only experiment that tests whether the BTC 5-min market
systematically overprices the favourite at extreme asks. The thesis is
that buying the underdog at, e.g., $0.08 when the favourite is priced at
$0.92 captures positive PnL per trade as variance harvest — even though
the underdog loses most individual bets. Each percentage point of
overconfidence at the extreme is real money because the payout asymmetry
is huge (b ≈ 11.5 at ask = $0.08).

## How it works

`SIGNAL_VARIANT=contrarian` activates a separate code path in
`backtester_v2.replay`:

1. At each decision tick within the entry window, read `ask_up` and
   `ask_down` from the tape.
2. If neither exceeds `CONTRARIAN_ASK_THRESHOLD` (default 0.90), skip.
3. Otherwise, identify the favourite (higher ask) and place a flat
   5-share buy on the underdog at the underdog's current ask.
4. One trade per window. Once placed, the rest of the entry window is a
   no-op for that window.

The Kelly path is bypassed entirely — sizing is fixed at
`MIN_SHARE_SIZE × underdog_ask` (≈ $0.40 at $0.08 ask). The point is
clean per-trade PnL measurement, not deployment.

## Sweep

`scripts/run_contrarian_sweep.py` runs the backtester at thresholds
`[0.85, 0.88, 0.90, 0.92, 0.94]` and produces `logs/contrarian_sweep.json`:

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
  ]
}
```

`edge_pp = win_rate − market_implied_underdog_p`. Positive means the
underdog won more often than the market priced it.

## Decision rule

- If any threshold has `pnl_per_trade > 0` AND `n_trades >= 30`, treat
  this as an edge candidate and write a follow-up spec for live wiring
  (Kelly sizing, thin-book filter, bot integration).
- Otherwise, the thesis is disproven on the available tape. Pivot to
  Front 1 (book-aware fair-value residual) or Front 5 (different market
  horizon).

## Caveats

- The Goldsky tape only contains markets that traded on chain. Bias
  toward liquid windows.
- "Thin book" is a known risk multiplier we cannot measure with this
  data. If the experiment is positive, a thin-book filter is the next
  must-have before going live.
- One-shot per window; no reprice / cancel logic. Different from the
  production direction-predict path.

## Spec / plan

- Spec: `docs/superpowers/specs/2026-04-30-contrarian-fade-experiment-design.md`
- Plan: `docs/superpowers/plans/2026-04-30-contrarian-fade-experiment.md` (gitignored)
