# 01 — Architecture

## Module map

All files are at the repo root. Everything is single-process, asyncio-based, no database, no external queue. State lives in memory; persistence is JSON files under `logs/`.

```
bot.py                 Main orchestrator. Owns the asyncio loop, composes everything.
 ├── price_feed.py     Binance aggTrade + depth5 WebSockets. Exposes PriceState.
 ├── market_finder.py  Polymarket Gamma API lookup. Resolves slug → token IDs → orderbook.
 ├── signal_engine.py  PriceState → Signal (direction, confidence, suggested_price, EV).
 ├── risk_manager.py   Can we trade? Sizing. Drawdown + loss-streak gates.
 ├── executor.py       PaperExecutor + LiveExecutor. Same interface, different backends.
 ├── trade_logger.py   JSON append to logs/trades_YYYYMMDD.json + daily summary.
 ├── telegram_alerts.py  Direct Bot API send. Trade/daily/paused/startup/shutdown.
 └── dashboard.py      aiohttp web server on 127.0.0.1:8787. HTML + /state JSON poll.

config.py              Loads .env into frozen Config dataclass. Validates on startup.
backtester.py          Offline replay of signal_engine over Binance klines. CLI.
self_improver.py       Sends N-day aggregates to Claude for parameter suggestions. CLI.
```

`tests/` has runnable unit + integration tests (standalone, no pytest config required — each file has `if __name__ == "__main__": main()`).

## The tick lifecycle

The bot's control flow is one function: `Bot._tick()` (in `bot.py`). It's called in a `while self._running` loop with adaptive sleeps. One invocation does:

```
_tick():
  1. _maybe_rollover_day()           # new UTC day → write daily summary
  2. if window_ts changed:
       _resolve_previous_trade()     # wait 2s, resolve at BTC close
       _on_new_window()              # snapshot window_open_price, kick off market fetch
  3. if in entry window AND reprice interval elapsed:
       _evaluate_entry()             # place / reprice / cancel / hold
  4. if pending trade AND seconds_remaining <= 3 AND not paper_hold:
       cancel + refund (T-3s sweep)
  5. sleep 0.5s (in entry window) or 2.0s (otherwise)
```

The 5-second reprice cadence (`REPRICE_INTERVAL_SEC`) is what paces actual gate evaluations. The outer `_tick` runs more often to keep the dashboard snappy, but `_evaluate_entry` is rate-limited.

## Data flow, end to end

```
Binance aggTrade WS  ─┐
                      ├─►  PriceState  ──►  compute_signal()  ──►  Signal
Binance depth5 WS  ───┘                                              │
                                                                     ▼
                              Polymarket Gamma API                 gate_vs_market()
                                    │                                   │
                                    ▼                                   ▼
                               MarketWindow                         decision
                              (token IDs, asks)  ──────────────────┐    │
                                                                    ▼    ▼
                                                   RiskManager.calculate_position_size()
                                                                    │
                                                                    ▼
                                                   PaperExecutor / LiveExecutor .place_order()
                                                                    │
                                                                    ▼
                                                   Trade (dataclass)  ──►  TradeLogger  ──►  logs/*.json
                                                                                  │
                                                                                  ▼
                                                                          TelegramAlerts
                                                                                  │
                                                                                  ▼
                                                                          Dashboard /state
```

The bot never asks Polymarket "what is BTC price" — that goes through Binance. The bot asks Polymarket only "what is the ask on this token" and "did my order fill."

## `PriceState` — the shared observation

One object, one writer (`PriceFeed._on_tick`), many readers. Fields:

| Field | Source | Used by |
|---|---|---|
| `current_price` | last aggTrade | everything |
| `window_open_price` | set by `bot._on_new_window()` at each 5-min boundary | delta, trend-slope normalization |
| `vwap` | rolling 120s vol-weighted mean of all ticks | `signal_engine.vwap_alignment` |
| `momentum` | `mean(last 10 prices) - mean(prev 10)`, updated every 10 ticks | `signal_engine.momentum_alignment` |
| `delta_from_open` | `(price - open) / open` | dominant signal feature |
| `delta_30s` | price velocity: `(price - price_30s_ago) / open` | late-window acceleration feature |
| `realized_vol` | stdev of 1s log-returns over 120s | regime multiplier on final confidence |
| `trend_slope_2m` | least-squares slope of per-second bucketed prices, normalized by open | longer-horizon trend context |
| `book_imbalance` | Binance depth5 top-of-book `(bid_qty - ask_qty)/total` | order-flow pressure feature |
| `buy_volume` / `sell_volume` | cumulative per window, from `is_buyer_maker` flag | volume imbalance feature |
| `volume_imbalance` | `(buy_volume - sell_volume) / total_vol` | signal feature |
| `tick_count` | count per window | diagnostics |
| `last_update` | ts of last tick | staleness check |

`window_open_price` is the only field that is *set* externally (by `bot._on_new_window`). Everything else is computed by `PriceFeed._on_tick`.

## Window ↔ market mapping

Polymarket's BTC 5-min market slug is `btc-updown-5m-{start_ts}` where `start_ts` is the Unix timestamp aligned to 5-minute boundaries (`n - (n % 300)`). The bot computes this locally and calls `https://gamma-api.polymarket.com/events?slug=<slug>` to resolve it to the `clobTokenIds[0]` (UP) and `clobTokenIds[1]` (DOWN). Order book is fetched per-token from `https://clob.polymarket.com/book?token_id=<id>`.

There's a fallback (`_fallback_recent_markets`) that lists crypto markets and greps for the timestamp — used when the slug endpoint 404s early in a window's lifetime.

## Why asyncio, not threads

Three concurrent I/O streams share one observation (`PriceState`): Binance aggTrade WS, Binance depth5 WS, and the periodic HTTP fetches to Polymarket. Asyncio gives us single-threaded access (no locks needed) with sub-millisecond context switching. The only CPU-bound work is signal math (microseconds).

## What isn't here

- No database. Daily JSON logs are the source of truth.
- No message queue. Everything is in-process.
- No Kubernetes / systemd unit. Run under `tmux`. Failure recovery = `python bot.py` again.
- No separate paper and live deployment environments. The `TRADING_MODE` env var is the switch.
- No multi-asset support. BTC is hardcoded: market_finder.py:47 builds the slug, price_feed.py references `BTCUSDT` in the default WS URL and REST fallback. Adding ETH/SOL is a day of work.
