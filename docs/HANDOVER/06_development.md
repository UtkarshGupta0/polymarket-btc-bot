# 06 — Development

## Code conventions

Python 3.12+. No `async def` in the signal path — signal math is cheap and synchronous, which keeps backtests trivial. `asyncio` is used only where there's actual I/O (WebSocket, HTTP, time-based waits).

- Everything at the repo root is importable by bare module name (`from signal_engine import compute_signal`).
- Tests in `tests/` prepend the parent dir to `sys.path` — they run standalone.
- `from __future__ import annotations` at the top of every file — forward refs and PEP 604 union syntax.
- Dataclasses for all stateful objects: `PriceState`, `RiskState`, `Trade`, `Signal`, `MarketWindow`.
- Logging via stdlib `logging.getLogger(__name__)`. No `print` in runtime code (OK in CLI test harnesses and dashboard).

## File ownership map

| File | Owns |
|---|---|
| `config.py` | All env variable defaults, validation, `CONFIG` singleton. |
| `price_feed.py` | Binance stream, `PriceState` mutation. No other file writes `PriceState`. |
| `market_finder.py` | Polymarket Gamma + CLOB book API. Returns `MarketWindow`. |
| `signal_engine.py` | `compute_signal`, `should_trade`, `gate_vs_market`, all confidence math. |
| `risk_manager.py` | `can_trade`, `calculate_position_size`, `RiskState` mutation. |
| `executor.py` | `PaperExecutor` + `LiveExecutor`. `build_executor()` picks by mode. |
| `bot.py` | Orchestration only — no business logic. Composes everything else. |
| `trade_logger.py` | JSON writes. Atomic via temp-and-rename. |
| `telegram_alerts.py` | HTTP sends. Fire-and-forget. |
| `dashboard.py` | Read-only snapshot of `Bot` state into HTML/JSON. |
| `backtester.py` | Offline replay. Calls `compute_signal` with synthesized `PriceState`. |
| `self_improver.py` | Claude API call with aggregated log stats. |

## Adding a feature

Worked example: adding a new signal feature called `foo`.

1. **Compute it in `PriceFeed`.** Add field to `PriceState` dataclass with a sensible default. Compute it inside `_on_tick` after the existing features. Trim to the existing buffer — reuse `self._ticks` if time-bucketed, or `self._prices` if tick-indexed.

2. **Consume it in `signal_engine`.** Add a normalization constant (e.g. `FOO_SCALE = ...`). In `compute_signal`, after the existing alignment blocks, add:
   ```python
   foo_raw = getattr(state, "foo", 0.0)
   foo_norm = _clamp(foo_raw / FOO_SCALE, -1.0, 1.0)
   foo_alignment = foo_norm * delta_dir if delta_dir != 0 else foo_norm
   ```
   Add a weight constant (e.g. `W_FOO = 0.10`) and renormalize the other weights so `sum == 1.0`. Add to the composite sum in both branches of `if delta_dir == 0 / else`.

3. **Flag-gate it.** Add `signal_foo_enabled: bool` to `Config` dataclass + `load_config`. In `compute_signal`, use the flag to select between new weights and legacy weights (see `SIGNAL_TREND_ENABLED` as template).

4. **Extend the rationale string.** Add a `foo=...` fragment — it ends up in every `signal | ...` log line and in the trade record's pre-place confidence breakdown.

5. **Synthesize in the backtester.** In `backtester.build_state_at_signal`, compute `foo` from the available kline data (or set to 0 if klines can't produce it). Comment the approximation.

6. **Write a unit test.** `tests/test_foo.py` — call `PriceFeed._on_tick` directly with synthetic ticks, assert the computed field is what you expect.

7. **Write a signal test.** Add a case to `signal_engine._run_tests()` — two `_mk_state` calls, one with aligned `foo`, one with contradicting, assert confidence ordering.

8. **Backtest.** Run `python backtester.py --days 30` with flag on vs off. Ship flag defaulted ON only if held-out WR is clearly better.

9. **Document.** Add the feature to [02_strategy.md §the-signal](02_strategy.md#the-signal) (weight table + why it exists). Add the flag to [03_configuration.md](03_configuration.md).

## Tests

All tests are standalone runnables. No `pytest` required (though `pytest tests/` works).

```bash
python tests/test_trend_slope.py         # PriceFeed slope computation
python tests/test_entry_loop_integration.py  # place/reprice/cancel
python tests/test_reprice_logic.py       # PaperExecutor reprice
python tests/test_sizing_transition.py   # flat→Kelly transition
python tests/test_gate_vs_market.py      # edge gate boundary cases
python tests/test_ws_reconnect.py        # WS reconnect behavior
python tests/test_live_connect.py        # CLOB client connect (needs creds)
python tests/test_self_improver.py       # Claude payload shape
```

Each file ends with `PASS ✓` on success or raises an `AssertionError`. The signal engine's own test suite is `python signal_engine.py` (ten cases including the trend-alignment and flag-off parity checks). The executor's is `python executor.py`. The risk manager's is `python risk_manager.py`. The trade logger's is `python trade_logger.py`.

## Running a single test during development

```bash
python signal_engine.py       # all signal cases
python risk_manager.py        # all risk cases
python executor.py            # all executor cases
python tests/test_trend_slope.py
```

No teardown, no fixtures — each test builds fresh state in-function. If a test writes to `logs/`, it cleans up after itself (see `test_trade_logger.py`).

## Backtester internals

`backtester.py` fetches BTCUSDT 1m klines from Binance REST, aligns to 5-min windows (`open_ts % 300000 == 0`), and for each window:

1. Computes signal at end of minute 4 (≈T-60s, earliest the live bot would enter).
2. Resolves against `close_of_minute_5 > open_of_minute_1`.
3. Assumes entry fill at `confidence_to_price(conf)` — no real orderbook.

Limitations the tool knows about:
- Volume imbalance is unavailable (klines have no buy/sell split). Set to 0 in synthesized state.
- Book imbalance unavailable. Set to 0.
- Trend-slope synthesized from 4 kline closes (per-minute), converted to per-second. Approximates live tick-derived slope but is noisier.
- Entry fills assumed at exactly `confidence_to_price` — real fills may differ 1-3c due to queue position or cancellation.
- Ties treated as LOSS (conservative).

This means backtest *levels* (absolute WR, PnL) are not directly comparable to live. Use it for **directional** comparisons: does changing weight X make WR go up or down? Does feature Y help more in high-vol or low-vol buckets?

The `--ignore-ev` flag is critical when using `confidence_to_price` as the synthetic entry — by construction, `EV` at that exact price is zero or negative, so the default EV gate blocks all backtest trades. `--ignore-ev` lets you see the signal's bare win rate.

The `--dist` flag prints the confidence distribution of all signals fired (not just trades). Useful for calibrating `MIN_CONFIDENCE`.

## Self-improver (Claude advisory)

`self_improver.py`:

1. Collects resolved trades from last N days of `logs/trades_*.json`.
2. Builds aggregated stats (by confidence bucket, hour, delta magnitude, direction, seconds-to-close).
3. Sends aggregates + current config + a sample of 10 recent trades to Claude (Sonnet 4.6).
4. Prints Claude's response.

The Claude response is advisory only — no code mutations. You read it and decide what to change.

Prompt caching: the static schema block uses `cache_control: ephemeral` so repeat runs within the 5-min window are cheaper.

## Git hygiene

- `.env` gitignored. Check `git status` before every commit.
- `logs/*` gitignored (trade outputs, paper run logs, backtest artifacts).
- `__pycache__/` gitignored.
- Commit messages follow `{verb} {short subject}` (`Fix edge gate off-by-one`, `Add trend-slope feature`). See `git log` for style.

## Dependencies

Runtime:
- `aiohttp` — HTTP and the dashboard server
- `websockets` — Binance streams
- `python-dotenv` — `.env` loading (soft dep; works without)
- `requests` — backtester REST (could migrate to aiohttp; not worth it)
- `py-clob-client==0.34.6` — Polymarket live orders
- `anthropic` — self_improver only

No Rust/C compilation needed. All pure Python.

Install (Arch):
```bash
pip install py-clob-client==0.34.6 --break-system-packages
pip install python-dotenv websockets aiohttp requests anthropic --break-system-packages
```

## Known tech debt

1. **BTC is hardcoded.** `market_finder.py:47` builds `btc-updown-5m-{ts}`; `price_feed.py` defaults to `BTCUSDT`. Refactoring to an `AssetConfig` struct is a half-day of work and unlocks ETH/SOL deployments.
2. **`refresh_trade_status` is live-only.** The paper executor has no equivalent — not a bug, but means `paper_hold` semantics don't test the fill-tracking logic.
3. **No executor cancellation on crash.** Live mode: if the bot crashes mid-window, the order stays live. Manual `cancel_all()` via Polymarket UI or a recovery script would be cleaner.
4. **Self-improver is one-shot.** No history of past recommendations, no diff vs prior, no follow-up on whether recommendation was applied. Could become a real automation loop but isn't yet.
5. **`build_state_at_signal` in backtester doesn't synthesize `book_imbalance`.** Signal_engine reads it; backtester sets 0. Slight mismatch to live behavior — backtest signal is a strict subset of live signal.
