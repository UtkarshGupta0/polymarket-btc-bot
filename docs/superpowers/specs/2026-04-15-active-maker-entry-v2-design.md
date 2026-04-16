# Active Maker Entry v2 — Design

**Date:** 2026-04-15
**Status:** Approved, pending implementation plan

## Problem

The current bot places a single limit order at T-45s with an entry price derived from the `confidence_to_price` curve ($0.88–$0.95). That price is disconnected from the live Polymarket orderbook. In practice:

- Early in the window, the market ask is $0.55–$0.80 — our $0.88 order crosses the spread and either executes as a taker or misses.
- When the signal is genuinely strong, market ask rises to $0.95+ — our order rests far below and never fills.
- The static EV gate (`conf > entry_price`) blocks ~100% of trades in backtest because conf rarely exceeds $0.88.

Net: the bot almost never trades, and when it does, entry pricing is arbitrary.

## Goal

Maximize monthly P&L at **$1 flat bet** for the first 100 resolved trades, then switch to Kelly 0.25 sizing. Keep existing risk halts (daily drawdown $3, consecutive loss 5, reserve $5).

## Design

### Entry loop (per 5-min window)

```
T-45s:
  fetch orderbook (reuse market_finder)
  compute signal (reuse signal_engine)
  if conf > ask_of_predicted_side:
    place maker BUY at (ask - 0.01), size = $1
    record active_order
  else:
    skip window

T-40s, T-35s, ..., T-10s  (every REPRICE_INTERVAL_SEC = 5s):
  recompute signal, refetch orderbook
  if predicted direction flipped:
    cancel active_order, skip remainder of window
  elif conf <= current ask_of_side:
    cancel active_order, skip remainder of window
  elif active_order unfilled AND (current_ask - 0.01) differs from order price by >= 1c:
    cancel active_order, wait for ack, place new order at (current_ask - 0.01)

T-3s:
  cancel any unfilled active_order

T-0 to T+5min:
  await market resolution, log WIN/LOSS
```

### Gate change

Replace the static EV gate with a dynamic "bot beats market" gate:

```
trade_if: signal.confidence > current_ask_of_chosen_side
```

Interpretation: Polymarket prices are market-implied probabilities. If our conf is higher than the ask for our side, the market is underpricing our direction relative to our estimate → positive expected edge.

### Sizing

- Trades 1–100 (resolved): fixed $1 per trade, ignore Kelly.
- Trade 101+: existing Kelly 0.25 sizing kicks in.
- `MAX_BET_SIZE` remains $5 as hard cap regardless of Kelly output.

### Risk gates (unchanged)

- Daily drawdown halt at -$3 (`MAX_DAILY_DRAWDOWN`)
- Consecutive loss halt at 5 (`MAX_CONSECUTIVE_LOSSES`)
- Reserve $5 minimum capital (`MIN_RESERVE`)

## Architecture changes

| File | Change |
|---|---|
| `config.py` | Add `REPRICE_INTERVAL_SEC=5`, `KELLY_ENABLE_AFTER=100` |
| `bot.py` | Replace single-shot entry block with re-quote loop. Track `active_order` state per window. |
| `executor.py` | Add `reprice(trade, new_price)` method — cancels existing order, waits for ack, places new. Paper and Live implementations. |
| `signal_engine.py` | New function `gate_vs_market(signal, ask) -> bool`. Replaces EV gate at call site. Leave original `should_trade` in place for backward compat / test. |
| `risk_manager.py` | Add resolved-trade counter. `bet_size()` returns `$1` while counter < `KELLY_ENABLE_AFTER`, Kelly thereafter. |

## Data flow

```
bot.py main loop
  │
  ├─ price_feed (WS, unchanged) ──► signal_engine.compute_signal()
  │                                       │
  ├─ market_finder.get_orderbook() ──────┤
  │                                       ▼
  │                              gate_vs_market(sig, ask)
  │                                       │
  │                              ┌────────┴────────┐
  │                              ▼                 ▼
  │                        execute order      skip window
  │                              │
  ▼                              ▼
executor.place()          executor.reprice()
                                 │
                                 ▼
                          CLOB (live) or in-mem (paper)
```

## Error handling

| Failure | Behavior |
|---|---|
| Orderbook fetch fails | Skip this 5s tick, keep any active order alive. Retry next tick. |
| Cancel fails | Log error. Do not attempt to place new order until cancel confirms (avoid double-order). |
| Place fails | Log error. Retry next 5s tick. |
| WS disconnect mid-window | Existing reconnect logic handles. Active order remains on CLOB; will be canceled at T-3s. |
| Price drift during cancel→place race | Mitigated by waiting for cancel ack before place. |

## Testing

**Unit tests:**
- `test_reprice_logic.py` — simulate 10 orderbook snapshots through a window, assert correct cancel/replace decisions.
- `test_gate_vs_market.py` — boundary cases (conf == ask, conf < ask, conf > ask).
- `test_sizing_transition.py` — bet size at trade 99, 100, 101 verifies switchover.

**Integration test:**
- Paper-mode run with injected synthetic orderbook progression (ask drifting from 0.55 → 0.95 over 45s). Verify: one order placed, multiple reprices, final cancel at T-3s.

**Live validation:**
- 48h paper-mode after deploy. Measure:
  - Trades per day (target: 5–15)
  - Paper fill rate (target: >30%)
  - Win rate by conf bucket (target: conf 70-80 → ≥70% win rate)

## Non-goals

- **Orderbook depth-based sizing:** skip size adjustment based on depth. YAGNI at $1 flat bets.
- **Multi-level ladder orders:** rejected during brainstorming; partial fills split edge.
- **Cross-window position carry:** each window is independent, always cancel at T-3s.
- **Taker fallback:** if maker never fills, skip the window. Do not cross the spread.

## Risks

- **Polymarket rate limits:** Worst case 9 reprices × 288 windows = 2592 cancel+place pairs/day + orderbook polls. Stays under documented limits (10 req/s). Monitor in first 48h.
- **Cancel race condition:** if cancel ack delayed, we may briefly hold two orders. Mitigated by waiting for ack before placing replacement.
- **Gate too tight:** if market is efficient, `conf > ask` rarely triggers. 48h paper run will reveal. Fallback: loosen gate to `conf > ask - 0.02`.
- **Flat-bet phase too long:** 100 trades at 10/day = 10 days before Kelly activates. Acceptable trade-off for signal validation.

## Success criteria

After 48h paper run post-implementation:
1. ≥10 resolved trades (vs ~0 today)
2. Win rate ≥60% at conf bucket 70-80
3. Positive P&L even at paper fill rates
4. Zero double-order incidents in logs
5. Zero crashes in reprice loop
