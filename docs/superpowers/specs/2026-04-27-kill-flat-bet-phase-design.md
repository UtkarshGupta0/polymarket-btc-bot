# Design — Kill flat-bet validation phase

**Date:** 2026-04-27
**Status:** Approved (awaiting plan)
**Front:** Profitability improvement — Front 4 (operational fixes that bleed PnL)

## Problem

The bot's `RiskManager.calculate_position_size` has two phases:

1. **Flat-bet validation phase** — first `KELLY_ENABLE_AFTER` (default 100) trades return a hardcoded `$1.00` size, regardless of confidence or entry price.
2. **Kelly phase** — quarter-Kelly thereafter.

The flat phase has a silent-failure bug. `executor.PaperExecutor.place_order` (and the live executor) reject any order whose `shares = size_usdc / entry_price` is less than `MIN_SHARE_SIZE = 5.0` (Polymarket CLOB minimum). With `size_usdc = $1.00`, this means orders are rejected at any `entry_price > 0.20`.

Production data: BTC 5-minute markets have an average ask of $0.80 in the entry window (see `docs/HANDOVER/08_backtest_v2.md` §Findings). Therefore the flat phase silently rejects approximately every order during the first 100 trades. The bot effectively does not trade until trade 101, and the user has no signal that this is happening — `place_order` returns `None`, which the entry loop treats as "skipped."

In addition: `MIN_BET_SIZE` config exists but the flat phase ignores it (uses literal `1.0`). Dead config.

## Why this front, why now

Backtest v2 (`08_backtest_v2.md`) concluded the current direction-prediction signal has negative edge in real markets. The backtest doc recommended structural redesign (book-aware fair-value-residual signal) as the path to profitability. Before sinking effort into a v3 signal, two cheap operational fixes should ship:

- This spec — kill flat phase. Removes silent rejects so live behavior matches backtest behavior.
- Next spec — confidence calibration / asymmetric edges (already 80% scaffolded as `SIGNAL_VARIANT=calibrated|asymmetric`).

Until live and backtest sizing are identical, no live evidence about signal quality is trustworthy. Fixing this is a precondition for evaluating any subsequent strategy change.

## Decision

Kill the flat-bet phase. All trades sized via fractional Kelly from trade 1. When Kelly produces a size below the platform minimum, return 0 with an explicit log line — no silent reject at the executor.

This is option **A3** from the brainstorm dialogue:

- **A1** rejected: pure Kelly with rejects still firing silently in the executor — no improvement on observability.
- **A2** rejected: Kelly with a floor at `MIN_SHARE_SIZE * entry_price` overrides Kelly's variance control on small balances, defeating Kelly's purpose.
- **A3** chosen: pure Kelly with explicit skip-on-too-small in the risk manager. Honest, logged, respects Kelly.

Alternative approach **B** (fix flat phase, keep validation) was rejected: the validation phase's purpose was to gather identical-size data before letting Kelly compound variance. Backtest_v2 already serves that purpose, so the live phase is redundant. It also burned capital silently when the bug fired.

## Code changes

### `risk_manager.py`

1. Delete the flat-bet branch of `calculate_position_size` (the `if self.state.total_trades < CONFIG.kelly_enable_after:` block).
2. Single Kelly path: compute `kelly_size` as today. Before returning, compute `effective_shares = round(size / entry_price, 2)` (mirroring executor rounding) and skip if `effective_shares < MIN_SHARE_SIZE`.
3. Import `MIN_SHARE_SIZE` from `executor` to ensure the threshold matches the executor's check exactly. Do not duplicate the constant.
4. Log skip reason at `DEBUG` level: `"size too small: kelly=$X.XX produces Y.YY shares < MIN_SHARE_SIZE at entry $Z.ZZ"`. Choose DEBUG, not INFO, because in low-balance / high-ask conditions this fires often and would flood INFO logs.

### `config.py`

1. Remove the `kelly_enable_after: int` field from the `Config` dataclass.
2. Remove the `KELLY_ENABLE_AFTER` env loader call from `load_config`.
3. Remove any `kelly_enable_after` reference from `print_summary`.

### `.env.example`

Remove the `KELLY_ENABLE_AFTER` line.

### `bot.py`

Grep for `kelly_enable_after` references. Remove. Also remove or rephrase any log line that mentions "validation phase" or implies trade 1..100 is special.

### `backtester_v2.py`

Grep for `KELLY_ENABLE_AFTER` env override. Remove the override (no longer needed — Kelly is always active). The backtester previously set this to 0 to force Kelly; that path is now the default.

### `tests/`

1. `risk_manager._run_tests`: remove the test that depends on `total_trades = CONFIG.kelly_enable_after`. The Kelly assertion is the only path.
2. Add a new test: `RiskManager(starting_balance=30.0).calculate_position_size(0.85, 0.80)` must return `0.0` (Kelly produces ~$1.56 → ~1.95 shares < `MIN_SHARE_SIZE` → skip).
3. Add a test that asserts a normal Kelly path still fills: `RiskManager(starting_balance=200.0).calculate_position_size(0.85, 0.20)` must produce a positive size with `≥ MIN_SHARE_SIZE` shares.
4. `tests/test_backtester_v2.py`: grep for `KELLY_ENABLE_AFTER`. If referenced (e.g., as a monkey-patched env var to force Kelly), remove the override.

### Documentation

1. **`docs/HANDOVER/02_strategy.md` §Sizing**: delete the "Flat-bet validation phase" subsection. Rewrite the introduction: "All trades sized via fractional Kelly. There is no separate validation phase — `backtest_v2` is the validation surface. When Kelly produces a size below the platform's `MIN_SHARE_SIZE`, the trade is skipped explicitly (logged at DEBUG)."
2. **`docs/HANDOVER/03_configuration.md`**: remove `KELLY_ENABLE_AFTER` row from the env-var table, if present.
3. **`docs/HANDOVER/05_maintenance.md`**: grep for any `KELLY_ENABLE_AFTER` mention and remove or rewrite.

## Behavior delta

All Kelly numbers below assume current config: `KELLY_FRACTION = 0.25`, `MIN_RESERVE = $5`, `MAX_BET_SIZE = $5`.

| Scenario | Before | After |
|---|---|---|
| Trade 1, conf=0.85, ask=0.20, bal=$30 | flat $1, fills (5 shares) | Kelly raw $5.08 → cap $5.00, fills (25 shares) |
| Trade 1, conf=0.85, ask=0.80, bal=$30 | flat $1, **silent reject** (1.25 shares) | Kelly $1.56, **explicit skip with log** (1.95 shares < MIN_SHARE_SIZE) |
| Trade 1, conf=0.85, ask=0.80, bal=$200 | flat $1, **silent reject** | Kelly raw $12.19 → cap $5.00, fills (6.25 shares) |
| Trade 100, any | switch to Kelly | identical to "trade 1" — no transition |
| Kelly EV ≤ 0 (any trade) | returns 0 | returns 0 (unchanged) |

Live behavior change: bot will now produce visible "size too small" log lines on rejected windows. Operators will see in the log what was previously silent. No PnL behavior change in the rejected-window case (rejection is unchanged); the bot just becomes truthful about it.

In windows where the flat phase did fill (ask ≤ 0.20), behavior changes: Kelly produces a larger size, more shares, more capital at risk per trade. With `MAX_DAILY_DRAWDOWN = $5` and `MAX_BET_SIZE = $5`, single-trade loss is bounded; daily drawdown halts at the existing limit.

## Risk + invariants

- **Live ↔ backtest sizing parity**: after this change, live `RiskManager` and backtest `RiskManager` produce identical sizes for identical inputs. This is the precondition for trusting any future live measurement of signal changes.
- **MAX_DAILY_DRAWDOWN unchanged**: $5/day. Kelly self-limits, halt fires at the same threshold.
- **MAX_CONSECUTIVE_LOSSES, MIN_RESERVE unchanged**.
- **MAX_BET_SIZE = $5** caps Kelly size as today.
- **MIN_BET_SIZE**: still respected in Kelly path (line 113 of current `risk_manager.py`). Now finally has a single owner instead of being half-ignored.

## Acceptance criteria

- `pytest` exits 0 with all existing + new tests passing.
- `python risk_manager.py` exits 0 (built-in test runner).
- `grep -rn "kelly_enable_after\|KELLY_ENABLE_AFTER" .` returns zero matches in tracked code (`.git`, history changelogs are exempt).
- Manual sanity: `RiskManager(starting_balance=30).calculate_position_size(0.85, 0.80) == 0` and `RiskManager(starting_balance=200).calculate_position_size(0.85, 0.20) > 0`.
- `python backtester_v2.py --tape data/btc_5m_tape.parquet --variant default --out logs/bt_v2_post_flat_kill.json` produces a result with non-zero `n_trades` (Kelly active from trade 1) and the JSON shape matches the prior run.
- `docs/HANDOVER/02_strategy.md` reflects the new sizing model.

## Out of scope

- Signal redesign (Front 1, future spec).
- Confidence calibration / asymmetric edges activation (Front 3, future spec).
- Adjusting `STARTING_CAPITAL`, `MAX_BET_SIZE`, or `MAX_DAILY_DRAWDOWN` to make more windows fillable. That is a config-tuning conversation after observing live skip-rate post-fix.
- Lowering `MIN_SHARE_SIZE` below 5 (platform-enforced minimum, not adjustable from the bot).
- Changes to `executor.py` reject behavior (still rejects below MIN_SHARE_SIZE — defense in depth).

## Implementation note

Implementation plan is the next step (writing-plans skill). This spec is the contract.
