# 08 · Backtest v2

Faithful historical replay of the live bot against real Polymarket fills, plus
a parameter sweep + new signal variants. Replaces the (broken) `backtester.py`.

## Why backtester.py was untrustworthy

`backtester.py` synthesised the entry price from `confidence_to_price(conf)`
(0.88–0.95). It never consulted the real Polymarket ask, so it never exercised
the load-bearing live gate `confidence - ask >= min_edge`. Its 99 % win rate
was an artefact of bidding into a fictional ask that always sat at the bot's
preferred price; in the live market the ask is set by other participants and
is usually too tight for that gate to clear.

It also skipped `RiskManager` (so no Kelly sizing, drawdown, streak halts),
ignored `MIN_SHARE_SIZE`, and didn't model fees.

## What backtester_v2 does differently

| Concern | backtester.py | **backtester_v2.py** |
|---|---|---|
| Ask | `confidence_to_price(conf)` (synthetic) | last on-chain taker-BUY fill within 30 s of the decision tick |
| `gate_vs_market` | not exercised | enforced (`confidence - ask >= min_edge`, `min_delta_pct`, hours block) |
| Sizing | flat $1 | live `RiskManager.calculate_position_size` (flat → quarter-Kelly) |
| Risk halts | none | drawdown / loss-streak / reserve, daily-resetting via `_FrozenClock` |
| `MIN_SHARE_SIZE` | not checked | `executor.MIN_SHARE_SIZE = 5` enforced |
| Fee model | none | optional `--fee 0.002` deducts from payout |
| Fillability | every signal trades | optional: requires a taker fill ≤ our limit during the entry window |
| Reprice | one-shot at T-60 s | every 5 s in T-45 s..T-8 s, 3-tick cancel hysteresis (mirrors live) |
| Trade timestamps | not modelled | frozen-clock shim patches `risk_manager.datetime`, `executor.time`, `executor.datetime` so `Trade.placed_at`, `Trade.time_iso`, and daily-reset all reflect replay date |

## Verified data schema (Phase A2.5)

`~/poly_data/markets.csv` (47,203 rows; produced by `update_markets`):

| Column | Type | Notes |
|---|---|---|
| createdAt | ISO-8601 UTC | populated for every row |
| id | int (decimal string) | also called `market_id` downstream |
| question | str | e.g. `Bitcoin Up or Down - December 19, 11:35AM-11:40AM ET` |
| answer1 | str | `Up` for the BTC 5-min markets |
| answer2 | str | `Down` |
| neg_risk | bool | False for BTC 5-min |
| market_slug | str | `btc-updown-5m-<unix_ts>` for the markets we want |
| token1 | str | 256-bit ERC-1155 outcome token id (decimal) |
| token2 | str | the other side |
| condition_id | str | hex |
| volume | float | USD lifetime volume |
| ticker | str | event grouping (e.g. `bitcoin-up-or-down-1` for the 5-min event) |
| closedTime | ISO-8601 UTC, **null in practice** | Polymarket leaves this null until the market resolves on-chain; do **not** rely on it for the 5-min markets |

**Slug regex**: `^btc-updown-5m-\d{10}$` (verified empirically — see `scripts/fetch_poly_data.py --inspect`). Other shapes (`btc-updown-15m`, `btc-updown-4h`) must NOT match.

Window timestamp comes from the slug suffix, not `closedTime`.

`~/poly_data/goldsky/orderFilled.csv` (raw events, 39 GB uncompressed; from the published snapshot):

| Column | Type | Notes |
|---|---|---|
| timestamp | int unix-sec | |
| maker | hex address | |
| makerAssetId | decimal string | `"0"` = USDC, otherwise outcome token id |
| makerAmountFilled | int | scaled by `10**6` |
| taker | hex address | |
| takerAssetId | decimal string | same convention |
| takerAmountFilled | int | scaled by `10**6` |
| transactionHash | hex | |

`~/poly_data/processed/trades.csv` (built by `process_live`; not used by default — we stream `orderFilled.csv` directly to skip the 39 GB intermediate).

## Coverage limitation (important)

The Polymarket markets API only returns markets that are currently active or
upcoming. As of 2026-04-26 the snapshot we built carries:

| Date | BTC 5-min markets in `markets.csv` |
|---|---:|
| 2025-12-19 | 30 |
| 2026-01-20 | 24 |
| 2026-02-24 | 1 |
| 2026-04-25 | 285 |
| 2026-04-26 | 2,450 (today; mostly future) |

**Total: 2,790 across 5 distinct days, not the 90 days the plan called for.**
The historical archive of 5-minute markets is not exposed by the API. To get
90 days we would need to scrape Polymarket's archive endpoint or rely on
`process_live`'s "missing market discovery" path to recover markets implied by
on-chain trades.

The backtester + sweep + variants are all functional regardless; what they
operate on is whatever windows the tape builder emits. Any backtest result we
report must include the `_meta.json` from the tape build so the date coverage
is auditable.

## How to run the pipeline

```
# 1. Clone + pull data
git clone https://github.com/warproxxx/poly_data ~/poly_data
cd ~/poly_data && uv sync
curl -L -o orderFilled_complete.csv.xz \
  https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz
xz -dk orderFilled_complete.csv.xz
mkdir -p goldsky && mv orderFilled_complete.csv goldsky/orderFilled.csv
uv run python -c "from update_utils.update_markets import update_markets; update_markets()"

# 2. Filter + tape (back in this repo)
cd /home/utk/polymarket-btc-bot
python scripts/fetch_poly_data.py --out data/btc_5m_markets.csv
python scripts/build_market_tape.py --markets data/btc_5m_markets.csv --days 0 \
       --out data/btc_5m_tape.parquet

# 3. Smoke run + baseline
python backtester_v2.py --tape data/btc_5m_tape.parquet --variant default \
       --out logs/bt_v2_default.json

# 4. Sweep
python scripts/param_sweep.py --tape data/btc_5m_tape.parquet \
       --out logs/sweep_top20.json

# 5. Calibration variant
python scripts/calibrate_confidence.py --in logs/bt_v2_default.json \
       --out data/confidence_remap.json
SIGNAL_VARIANT=calibrated CONFIDENCE_REMAP_PATH=$PWD/data/confidence_remap.json \
  python backtester_v2.py --tape data/btc_5m_tape.parquet --variant calibrated \
         --out logs/bt_v2_calibrated.json

# 6. Apply winner (dry-run first)
python scripts/apply_bt_config.py --dry-run
python scripts/apply_bt_config.py
```

## Signal variants (Phase E)

Set `SIGNAL_VARIANT=<x>` in `.env`:

| Variant | Effect |
|---|---|
| `default` | Current production behaviour. |
| `calibrated` | Apply `data/confidence_remap.json` to confidence before `gate_vs_market`. Buckets the raw signal vs realised win-rate (isotonic fit) — useful when the 80–90 % bucket is miscalibrated downward. |
| `regime_filtered` | Compute realised vol from the last 5×1-minute closes; only trade when `VOL_REGIME_MIN <= rv <= VOL_REGIME_MAX`. Default range `[0, 1]` is a no-op. |
| `asymmetric` | `gate_vs_market` uses `MIN_EDGE_UP` for UP signals and `MIN_EDGE_DOWN` for DOWN signals — turn on if backtest shows side asymmetry. |

The variant code lives in `signal_engine.py`. `compute_signal()` calls
`_apply_variant()` after the time-boost clamp; `gate_vs_market()` calls
`_edge_threshold(direction)`. Default behaviour is byte-identical to the prior
implementation (existing tests stay green).

## Open assumptions

* **Ask proxy**: last taker-BUY fill within 30 s. Sensitivity to that window is reported by re-running with a stricter (10 s) and looser (60 s) freshness; document the spread.
* **Fillability**: a fill at-or-below our limit during the entry window is the proxy. Without orderbook depth we cannot model queue position or partial fills.
* **Fees**: default `--fee 0` (parity with prior backtester). Run `--fee 0.002` to estimate the impact of Polymarket's current taker fee on payout.
* **Survivorship**: the trades feed only contains markets that traded on chain. Markets with zero fills are dropped from the tape, biasing toward liquid windows.
* **Volume imbalance**: still 0 in the synthesised PriceState (klines lack trade-direction). Live signal will diverge slightly.
* **Bug found and fixed during testing**: `MIN_SHARE_SIZE = 5` × flat $1 sizing × entry prices > $0.20 caused the flat-bet phase to silently reject most orders. Fixed by removing the flat-bet phase entirely; see `docs/superpowers/specs/2026-04-27-kill-flat-bet-phase-design.md`. Backtest behavior is now identical to live (Kelly from trade 1).

## Findings + recommendation

Tape: 238 BTC 5-min windows (3 distinct days: 2025-12-19 / 2026-01-20 / 2026-04-25→26). Built from `scripts/fetch_clob_trades.py` (615k taker-BUY fills via the data-api endpoint, since the published Goldsky snapshot ends in 2025-10).

### Headline: the current signal has no edge in real markets

| MIN_CONFIDENCE | MIN_EDGE | MIN_DELTA_PCT | trades | WR | PnL | per-trade |
|---:|---:|---:|---:|---:|---:|---:|
| **0.60 (prod)** | 0.02 (prod) | 0 | **0** | — | — | — |
| 0.45 | 0.01 | 0 | 0 | — | — | — |
| 0.30 | 0.01 | 0 | 1 | 0.0 | −$5.12 | — |
| 0.10 | 0.00 | 0 | 7 | 14% | −$9.84 | −$1.41 |

The Polymarket BTC 5-minute order book prices the favoured side at an average ask of **0.80** during the 8-tick decision window. The bot's confidence over the same windows averages **0.19** with a maximum of **0.70**. That gap means the live `gate_vs_market(confidence - ask >= min_edge)` test essentially never fires at production thresholds. When it does fire (forced by relaxing thresholds), the trades lose: the market's pricing is more informed than the bot's signal.

This is consistent with the structural disadvantage the bot has — it reads a slightly delayed BTC tape from Binance and never sees the Polymarket book depth or queue position.

### What would need to change for the bot to be profitable

1. **Add Polymarket book-depth signals.** Currently the bot only computes BTC-price features. A v3 signal that reads the resting bid/ask sizes and recent maker placements is likely required.
2. **Bet against extreme mispricings rather than for direction.** When the implied probability (ask) sits at 0.92 but realised vol over the last 30 s suggests the move could revert, a bet on the OTHER side at 0.08 is a different kind of edge — the current bot doesn't model this.
3. **Stop trying to predict the direction directly.** A "fair-value vs market" residual signal is more aligned with how prediction markets actually pay; the current "BTC delta + momentum + VWAP + volume" composite is producing low-confidence, market-confirmed signals (which is exactly when the market is least beatable).
4. **Or — give up on this market entirely.** The data-driven recommendation from this backtest is **do not run the bot live in its current form**; the realised edge is negative.

### Operational fixes worth doing regardless

* **Flat-bet silent-reject bug — fixed.** `MIN_SHARE_SIZE = 5` × flat $1 × ask > 0.20 was silently rejecting every order in the flat-bet phase. Resolved by removing the flat-bet phase; sizing is now pure quarter-Kelly with an explicit too-small skip. See `docs/superpowers/specs/2026-04-27-kill-flat-bet-phase-design.md`.
* **`apply_bt_config.py` is wired but the sweep winner from this run is the trivial config that produces no trades.** Re-run after a strategy redesign that actually beats the market.

### Per-bucket detail (loose-gate run, 7 trades)

| Bucket | n | WR | PnL |
|---|---:|---:|---:|
| direction=UP | 2 | 0.00 | −$4.67 |
| direction=DOWN | 5 | 0.20 | −$5.17 |
| delta <0.01% | 2 | 0.50 | +$10.36 |
| delta 0.01–0.05% | 5 | 0.00 | −$20.20 |
| hour 13 UTC | 2 | 0.00 | −$5.20 |
| hour 21 UTC | 1 | 0.00 | −$8.07 |
| hour 14 UTC | 1 | 0.00 | −$3.87 |

(Sample too small for side / hour conclusions — the headline is the −14% per-trade loss, not the bucket distribution.)
