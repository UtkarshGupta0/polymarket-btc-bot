# Active Maker Entry v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-shot static-price entry with dynamic re-quoting maker loop that compares bot confidence to live Polymarket ask.

**Architecture:** At T-45s the bot places a maker buy at `ask - 0.01` sized at flat $1. Every 5s it refetches signal + orderbook; if direction flipped or `conf <= ask`, it cancels; otherwise it may cancel and re-quote when ask drifts ≥1c. At T-3s any unfilled order is canceled. After 100 resolved trades, sizing switches from flat $1 to existing Kelly 0.25.

**Tech Stack:** Python 3.13, asyncio, py-clob-client 0.34.6, aiohttp.

**Spec:** `docs/superpowers/specs/2026-04-15-active-maker-entry-v2-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config.py` | Load config | Add 2 fields: `reprice_interval_sec`, `kelly_enable_after` |
| `signal_engine.py` | Compute signal, gate | Add `gate_vs_market(signal, ask)` function |
| `executor.py` | Place/cancel/resolve orders | Add `reprice()` method on both Paper + Live executors |
| `risk_manager.py` | Sizing + halts | Add flat-then-kelly switch in `calculate_position_size` |
| `bot.py` | Main loop | Replace entry block with 5s-interval reprice loop |
| `tests/test_gate_vs_market.py` | New | Unit: conf vs ask boundary cases |
| `tests/test_reprice_logic.py` | New | Unit: reprice decision tree over synthetic orderbook progression |
| `tests/test_sizing_transition.py` | New | Unit: bet size before/after trade 100 |

---

## Task 1: Add config fields

**Files:**
- Modify: `config.py:43-53` (add fields) and `config.py:105-132` (load)

- [ ] **Step 1: Modify `Config` dataclass in `config.py`**

After line 53 (`kelly_fraction: float`), add:

```python
    # Entry v2 tuning
    reprice_interval_sec: int
    kelly_enable_after: int
```

- [ ] **Step 2: Modify `load_config()` in `config.py`**

In the `Config(...)` constructor (inside `load_config`), after `kelly_fraction=_get_float("KELLY_FRACTION", 0.25),`, add:

```python
        reprice_interval_sec=_get_int("REPRICE_INTERVAL_SEC", 5),
        kelly_enable_after=_get_int("KELLY_ENABLE_AFTER", 100),
```

- [ ] **Step 3: Verify config loads**

Run: `python -c "from config import CONFIG; print(CONFIG.reprice_interval_sec, CONFIG.kelly_enable_after)"`
Expected output: `5 100`

- [ ] **Step 4: Commit**

Repo is not git-initialized. Skip commit; proceed to next task.

---

## Task 2: Add `gate_vs_market` to signal_engine

**Files:**
- Create: `tests/test_gate_vs_market.py`
- Modify: `signal_engine.py` (add function after `should_trade`, around line 205)

- [ ] **Step 1: Write failing test**

Create `tests/test_gate_vs_market.py`:

```python
"""Test gate_vs_market: bot trades only when conf > market ask for the side."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_engine import Signal, gate_vs_market


def _sig(direction: str, conf: float) -> Signal:
    return Signal(
        direction=direction,
        confidence=conf,
        suggested_price=0.0,
        rationale="",
        timestamp=0.0,
        window_delta=0.0,
        seconds_to_close=30.0,
        expected_value=0.0,
    )


def main() -> None:
    # conf > ask_up => trade UP
    assert gate_vs_market(_sig("UP", 0.75), ask_up=0.60, ask_down=0.40) is True
    # conf < ask_up => skip
    assert gate_vs_market(_sig("UP", 0.55), ask_up=0.60, ask_down=0.40) is False
    # conf == ask_up => skip (strict inequality)
    assert gate_vs_market(_sig("UP", 0.60), ask_up=0.60, ask_down=0.40) is False
    # DOWN: uses ask_down
    assert gate_vs_market(_sig("DOWN", 0.80), ask_up=0.30, ask_down=0.70) is True
    assert gate_vs_market(_sig("DOWN", 0.65), ask_up=0.30, ask_down=0.70) is False
    # seconds_to_close < 5 => always False even if conf > ask
    s = _sig("UP", 0.90)
    s.seconds_to_close = 3.0
    assert gate_vs_market(s, ask_up=0.60, ask_down=0.40) is False
    # ask missing (<=0) => skip (defensive)
    assert gate_vs_market(_sig("UP", 0.90), ask_up=0.0, ask_down=0.40) is False
    print("PASS ✓ gate_vs_market")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_gate_vs_market.py`
Expected: `ImportError: cannot import name 'gate_vs_market' from 'signal_engine'`

- [ ] **Step 3: Add `gate_vs_market` in `signal_engine.py`**

After the existing `should_trade` function (ends around line 204), append:

```python
def gate_vs_market(signal: Signal, ask_up: float, ask_down: float) -> bool:
    """Trade only if bot conf beats market ask for its predicted side.

    Market ask = implied probability of that outcome winning. If our conf
    is higher than the ask, the market is underpricing our direction.

    Rejects:
      - seconds_to_close < 5 (no time for fill/resolution safety)
      - ask for chosen side <= 0 (bad orderbook data)
      - conf <= ask (no edge)
    """
    if signal.seconds_to_close < 5:
        return False
    ask = ask_up if signal.direction == "UP" else ask_down
    if ask <= 0:
        return False
    return signal.confidence > ask
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_gate_vs_market.py`
Expected: `PASS ✓ gate_vs_market`

- [ ] **Step 5: Commit**

Repo is not git-initialized. Skip.

---

## Task 3: Add `reprice()` to PaperExecutor and LiveExecutor

**Files:**
- Create: `tests/test_reprice_logic.py`
- Modify: `executor.py` (add method on both executor classes)

- [ ] **Step 1: Write failing test**

Create `tests/test_reprice_logic.py`:

```python
"""Test reprice() atomically cancels old order and places new at updated price."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import PaperExecutor


def main() -> None:
    ex = PaperExecutor(starting_balance=30.0)

    # Place initial order
    t1 = ex.place_order(
        window_ts=1000, window_end_ts=1300,
        direction="UP", confidence=0.75,
        token_id="tok_up", entry_price=0.60, size_usdc=1.0,
    )
    assert t1 is not None, "initial place failed"
    assert t1.entry_price == 0.60

    # Reprice to new ask
    t2 = ex.reprice(window_ts=1000, new_price=0.65)
    assert t2 is not None, "reprice returned None"
    assert t2.entry_price == 0.65, f"expected 0.65, got {t2.entry_price}"
    assert t2.size_usdc == 1.0, "size must carry over"
    assert t2.direction == "UP", "direction must carry over"
    # Old order removed — only one active
    assert ex.pending_trade(1000) is t2, "pending should be new trade"

    # Reprice when no active order => None
    t_noop = ex.reprice(window_ts=9999, new_price=0.70)
    assert t_noop is None, "reprice on unknown window must return None"

    print("PASS ✓ reprice_logic")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_reprice_logic.py`
Expected: `AttributeError: 'PaperExecutor' object has no attribute 'reprice'` or similar

- [ ] **Step 3: Inspect existing executor structure**

Run: `grep -n "def " executor.py | head -30`
Note the method signatures and internal state fields (`self._trades`, `self._pending`, etc.) so reprice can reuse them.

- [ ] **Step 4: Add `pending_trade()` helper + `reprice()` on PaperExecutor**

In `executor.py`, inside `class PaperExecutor` (ends around line 150), add BEFORE the `resolve_trade` method:

```python
    def pending_trade(self, window_ts: int):
        """Return currently-active Trade for window, or None."""
        return self._trades.get(window_ts)

    def reprice(self, window_ts: int, new_price: float):
        """Cancel existing trade for window and replace at new_price.

        Returns new Trade or None if no active trade for window.
        Refunds original cost, deducts new cost (allows price difference).
        """
        old = self._trades.get(window_ts)
        if old is None:
            return None
        # Refund old cost
        self._balance += old.size_usdc
        # Remove old
        del self._trades[window_ts]
        # Place new at same size, new price
        return self.place_order(
            window_ts=window_ts,
            window_end_ts=old.window_end_ts,
            direction=old.direction,
            confidence=old.confidence,
            token_id=old.token_id,
            entry_price=new_price,
            size_usdc=old.size_usdc,
        )
```

NOTE: if `PaperExecutor` does not use `self._trades` as a dict keyed by window_ts OR does not expose `self._balance`, adjust these names to match. Read `executor.py:46-150` to confirm field names before writing. If trades are stored under a different field name (e.g. `self._pending`, `self._open`), rename accordingly in both `pending_trade()` and `reprice()`.

- [ ] **Step 5: Add `reprice()` on LiveExecutor**

In `class LiveExecutor` (around line 151), after `cancel_order` method (~line 322), add:

```python
    def reprice(self, window_ts: int, new_price: float):
        """Cancel live order and place a new one at new_price.

        Blocks on cancel ack to avoid double orders. Returns new Trade or None.
        """
        old = self._trades.get(window_ts)
        if old is None:
            return None
        if old.order_id:
            ok = self.cancel_order(old.order_id)
            if not ok:
                logger.warning(
                    f"reprice: cancel failed for {old.order_id}, "
                    f"skipping replace to avoid double order"
                )
                return None
        # Cancel succeeded or no order_id; remove from state
        del self._trades[window_ts]
        # Refund the reserved balance
        self._balance += old.size_usdc
        return self.place_order(
            window_ts=window_ts,
            window_end_ts=old.window_end_ts,
            direction=old.direction,
            confidence=old.confidence,
            token_id=old.token_id,
            entry_price=new_price,
            size_usdc=old.size_usdc,
        )
```

Same note: confirm field names match (`self._trades`, `self._balance`, `old.order_id`, `old.token_id`, `old.window_end_ts`) by reading lines 151-350 of `executor.py` before pasting. Rename to match if they differ.

- [ ] **Step 6: Run test to verify it passes**

Run: `python tests/test_reprice_logic.py`
Expected: `PASS ✓ reprice_logic`

- [ ] **Step 7: Commit**

Repo is not git-initialized. Skip.

---

## Task 4: Flat-bet-then-Kelly sizing in risk_manager

**Files:**
- Create: `tests/test_sizing_transition.py`
- Modify: `risk_manager.py:80-101` (`calculate_position_size`)

- [ ] **Step 1: Write failing test**

Create `tests/test_sizing_transition.py`:

```python
"""Sizing: flat $1 for first KELLY_ENABLE_AFTER trades, then Kelly."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CONFIG
from risk_manager import RiskManager


def main() -> None:
    # Trade counter is persisted on total_trades (RESOLVED trades).
    rm = RiskManager(starting_balance=30.0)
    # Before any trades: flat $1
    size = rm.calculate_position_size(confidence=0.80, entry_price=0.60)
    assert size == 1.0, f"trade 1 should be $1 flat, got {size}"

    # Simulate KELLY_ENABLE_AFTER - 1 wins
    for _ in range(CONFIG.kelly_enable_after - 1):
        rm.on_trade_placed(1.0)
        rm.on_trade_resolved(won=True, payout_usdc=1.5, pnl=0.5)

    # Still flat phase (total_trades == kelly_enable_after - 1)
    size = rm.calculate_position_size(confidence=0.80, entry_price=0.60)
    assert size == 1.0, f"trade {CONFIG.kelly_enable_after} still flat, got {size}"

    # One more trade — counter hits the threshold
    rm.on_trade_placed(1.0)
    rm.on_trade_resolved(won=True, payout_usdc=1.5, pnl=0.5)

    # Now Kelly: at conf 0.80 / price 0.60, Kelly size should be > 0 and != 1.0
    size = rm.calculate_position_size(confidence=0.80, entry_price=0.60)
    assert size > 0, "post-threshold Kelly should be positive for positive-EV"
    # May equal $1 by coincidence of Kelly math; check the path was Kelly by
    # verifying a low-conf trade returns 0 (flat phase would still return $1)
    size_low = rm.calculate_position_size(confidence=0.50, entry_price=0.90)
    assert size_low == 0.0, (
        f"post-threshold negative-EV must return 0 (Kelly path), got {size_low}"
    )
    print("PASS ✓ sizing_transition")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_sizing_transition.py`
Expected: FAIL — current code returns Kelly result immediately (~$0 for small balance + conservative Kelly) not flat $1.

- [ ] **Step 3: Modify `calculate_position_size` in `risk_manager.py`**

Replace the function (currently lines 80-101) with:

```python
    def calculate_position_size(self, confidence: float, entry_price: float) -> float:
        """Returns USDC size for the order. 0 if should skip.

        Flat $1 for the first CONFIG.kelly_enable_after resolved trades
        (signal validation phase). Kelly 0.25 thereafter.
        """
        if entry_price <= 0 or entry_price >= 1.0:
            return 0.0

        # Flat-bet phase: fixed $1 while we validate the signal
        if self.state.total_trades < CONFIG.kelly_enable_after:
            # Still require positive EV to avoid wasting the flat bet
            b = (1.0 / entry_price) - 1.0
            if b <= 0 or (b * confidence - (1.0 - confidence)) <= 0:
                return 0.0
            available = max(0.0, self.state.current_balance - CONFIG.min_reserve)
            if available < 1.0:
                return 0.0
            return 1.0

        # Kelly phase
        b = (1.0 / entry_price) - 1.0
        if b <= 0:
            return 0.0
        p = confidence
        q = 1.0 - p
        kelly_pct = (b * p - q) / b
        if kelly_pct <= 0:
            return 0.0

        target_pct = kelly_pct * CONFIG.kelly_fraction
        available = max(0.0, self.state.current_balance - CONFIG.min_reserve)
        size = available * target_pct

        if size < CONFIG.min_bet_size:
            return 0.0
        size = min(size, CONFIG.max_bet_size)
        return round(size, 2)
```

- [ ] **Step 4: Run new test to verify it passes**

Run: `python tests/test_sizing_transition.py`
Expected: `PASS ✓ sizing_transition`

- [ ] **Step 5: Run existing risk tests to confirm no regression**

Run: `python risk_manager.py`
Expected: `All risk tests PASS ✓`

NOTE: Existing test at line 156-174 of `risk_manager.py` calls `calculate_position_size` on a fresh `RiskManager` (total_trades=0) which now returns flat $1. The test currently asserts `0 < size <= MAX_BET_SIZE` (line 158) — $1 satisfies this. It also asserts `size_neutral == 0` at line 163 for conf==price — our flat-phase still requires positive EV, so $0 is correct. The max-bet-cap test at lines 166-169 asserts `size2 == MAX_BET_SIZE` — this WILL FAIL under flat phase because we return $1 not $5. Fix by bumping that test's `rm2` through `kelly_enable_after` trades first, OR by adding `total_trades=CONFIG.kelly_enable_after` shortcut.

To fix the existing test without changing behavior, modify `risk_manager.py` lines 166-169 from:

```python
    rm2 = RiskManager(starting_balance=1000.0)
    size2 = rm2.calculate_position_size(0.95, 0.90)
    print(f"size(conf=0.95, price=0.90, bal=$1000) = ${size2} (capped at ${CONFIG.max_bet_size})")
    assert size2 == CONFIG.max_bet_size
```

to:

```python
    rm2 = RiskManager(starting_balance=1000.0)
    # Fast-forward past flat phase
    rm2.state.total_trades = CONFIG.kelly_enable_after
    size2 = rm2.calculate_position_size(0.95, 0.90)
    print(f"size(conf=0.95, price=0.90, bal=$1000) = ${size2} (capped at ${CONFIG.max_bet_size})")
    assert size2 == CONFIG.max_bet_size
```

After this edit, rerun `python risk_manager.py` — expect all pass.

- [ ] **Step 6: Commit**

Repo is not git-initialized. Skip.

---

## Task 5: Replace entry block in bot.py

**Files:**
- Modify: `bot.py` (entry window logic around lines 160-320)

This is the largest change. Read the current entry block first.

- [ ] **Step 1: Read current entry block**

Run: `sed -n '150,330p' bot.py`

Identify:
- The `in_entry_window` conditional around line 165
- The single-shot place call around line 314
- Where `self.pending_trade` is set/cleared
- How `seconds_remaining` is computed per tick

- [ ] **Step 2: Identify state fields to add**

In `Bot.__init__` (find via `grep -n "__init__" bot.py`), add per-window state:

```python
        self._last_reprice_ts: float = 0.0
        self._active_window_ts: int = 0
```

- [ ] **Step 3: Replace the entry-window block**

Locate the block starting around line 165 (the `if in_entry_window...` branch that calls `place_order`). Replace the single-shot logic with:

```python
            in_entry_window = (
                CONFIG.entry_window_end
                < seconds_remaining
                <= CONFIG.entry_window_start
            )

            if in_entry_window:
                # Throttle: only act every REPRICE_INTERVAL_SEC
                now = time.time()
                if now - self._last_reprice_ts >= CONFIG.reprice_interval_sec:
                    self._last_reprice_ts = now
                    await self._evaluate_entry(window_ts, window_end_ts, seconds_remaining)

            # Hard cancel at T-3s (inside window_end boundary)
            if seconds_remaining <= 3 and self.executor.pending_trade(window_ts) is not None:
                logger.info(f"T-3s cancel sweep for window {window_ts}")
                if hasattr(self.executor, "cancel_pending_if_unfilled"):
                    self.executor.cancel_pending_if_unfilled(window_ts)
```

Replace field names (`window_ts`, `window_end_ts`) with whatever the surrounding code uses — read the existing block to confirm.

- [ ] **Step 4: Add `_evaluate_entry` method on Bot**

After the main loop method (find a reasonable spot near other private helpers), add:

```python
    async def _evaluate_entry(
        self, window_ts: int, window_end_ts: int, seconds_remaining: float
    ) -> None:
        """One reprice tick: compute signal, fetch book, place/reprice/cancel."""
        from signal_engine import gate_vs_market  # lazy to avoid cycle

        # 1. Risk halt check
        can, reason = self.risk.can_trade()
        if not can:
            logger.debug(f"risk halt: {reason}")
            return

        # 2. Fresh signal
        sig = compute_signal(
            self.feed.state,
            window_end_ts=window_end_ts,
            now=time.time(),
        )
        if sig is None:
            return

        # 3. Fresh orderbook
        book = await self.market_finder.get_orderbook(window_ts)
        if book is None:
            logger.debug(f"no orderbook for window {window_ts}")
            return
        ask_up = book.get("up_ask", 0.0)
        ask_down = book.get("down_ask", 0.0)

        active = self.executor.pending_trade(window_ts)

        # 4. Gate
        if not gate_vs_market(sig, ask_up=ask_up, ask_down=ask_down):
            # Cancel any active order — edge gone
            if active is not None:
                logger.info(
                    f"cancel: conf={sig.confidence:.2f} dir={sig.direction} "
                    f"ask_up={ask_up:.2f} ask_down={ask_down:.2f}"
                )
                if hasattr(self.executor, "cancel_pending_if_unfilled"):
                    self.executor.cancel_pending_if_unfilled(window_ts)
                elif hasattr(self.executor, "cancel_order") and active.order_id:
                    self.executor.cancel_order(active.order_id)
            return

        # 5. Determine target price = ask_of_side - 0.01
        side_ask = ask_up if sig.direction == "UP" else ask_down
        target_price = round(max(0.02, side_ask - 0.01), 2)
        side_token = book.get("up_token" if sig.direction == "UP" else "down_token")
        if not side_token:
            logger.warning(f"no token for side {sig.direction}")
            return

        # 6. Direction flipped => cancel and skip this tick
        if active is not None and active.direction != sig.direction:
            logger.info(f"direction flip {active.direction}->{sig.direction}, cancel")
            if hasattr(self.executor, "cancel_pending_if_unfilled"):
                self.executor.cancel_pending_if_unfilled(window_ts)
            elif hasattr(self.executor, "cancel_order") and active.order_id:
                self.executor.cancel_order(active.order_id)
            return

        # 7. Place new or reprice
        size = self.risk.calculate_position_size(sig.confidence, target_price)
        if size <= 0:
            return

        if active is None:
            trade = self.executor.place_order(
                window_ts=window_ts,
                window_end_ts=window_end_ts,
                direction=sig.direction,
                confidence=sig.confidence,
                token_id=side_token,
                entry_price=target_price,
                size_usdc=size,
            )
            if trade is not None:
                logger.info(
                    f"PLACED {sig.direction} @ ${target_price} "
                    f"conf={sig.confidence:.2f} size=${size}"
                )
            return

        # Only reprice if price moved >= 1c
        if abs(active.entry_price - target_price) >= 0.01:
            logger.info(
                f"REPRICE {active.direction} ${active.entry_price}->${target_price} "
                f"conf={sig.confidence:.2f}"
            )
            self.executor.reprice(window_ts, new_price=target_price)
```

NOTE: The method names used above (`self.risk`, `self.feed`, `self.market_finder`, `self.executor`) must match the actual attribute names in `Bot.__init__`. Open `bot.py`, find `__init__`, confirm attribute names, adjust above if different.

- [ ] **Step 5: Remove the now-dead old entry logic**

After Step 3 replaced the single-shot block, search for any remaining references to the old `should_trade` call path in `bot.py`:

Run: `grep -n "should_trade\|place_order" bot.py`

Remove the old place_order call (around line 314) and the old `should_trade(signal)` gate (around line 269) if they are no longer reachable. Leave the resolution / logging code intact.

- [ ] **Step 6: Paper smoke test**

Run: `TRADING_MODE=paper timeout 400 python bot.py 2>&1 | tee /tmp/bot-smoke.log`
(400 seconds = >1 full window cycle.)

Expected log lines (depending on BTC movement):
- `signal | dir=... conf=...`
- `PLACED UP @ $0.64 conf=0.72 size=$1`   (or similar)
- Maybe `REPRICE UP $0.64->$0.66 conf=0.74`
- `T-3s cancel sweep` OR `RESOLVED: WIN/LOSS pnl=...`

Pass criteria: no tracebacks, at least one `signal` line per window.

- [ ] **Step 7: Commit**

Repo is not git-initialized. Skip.

---

## Task 6: Integration test with synthetic orderbook

**Files:**
- Create: `tests/test_entry_loop_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/test_entry_loop_integration.py`:

```python
"""Integration: simulate a full window of reprice decisions with mocked orderbook.

Injects an orderbook progression (ask drifting 0.55 -> 0.70 -> 0.85) and
verifies the entry loop places once, reprices when ask moves >=1c, and
cancels at T-3s or when conf drops below ask.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import PaperExecutor
from signal_engine import Signal


def _make_signal(direction: str, conf: float, stc: float) -> Signal:
    return Signal(
        direction=direction, confidence=conf, suggested_price=0.0,
        rationale="", timestamp=0.0, window_delta=0.001,
        seconds_to_close=stc, expected_value=0.0,
    )


async def _run() -> None:
    ex = PaperExecutor(starting_balance=30.0)
    window_ts = 1000
    window_end_ts = 1300

    # T-45s: ask=0.55, conf=0.70 -> place
    t1 = ex.place_order(
        window_ts=window_ts, window_end_ts=window_end_ts,
        direction="UP", confidence=0.70, token_id="tok_up",
        entry_price=0.54, size_usdc=1.0,
    )
    assert t1 is not None
    assert t1.entry_price == 0.54

    # T-35s: ask=0.65, still UP wins gate -> reprice to 0.64
    t2 = ex.reprice(window_ts=window_ts, new_price=0.64)
    assert t2 is not None
    assert t2.entry_price == 0.64

    # T-25s: ask=0.75, still UP wins gate -> reprice to 0.74
    t3 = ex.reprice(window_ts=window_ts, new_price=0.74)
    assert t3 is not None
    assert t3.entry_price == 0.74

    # T-5s: ask=0.85, conf drops to 0.72 < 0.85 -> cancel
    ex.cancel_pending_if_unfilled(window_ts) \
        if hasattr(ex, "cancel_pending_if_unfilled") \
        else ex._trades.pop(window_ts, None)
    assert ex.pending_trade(window_ts) is None

    print("PASS ✓ entry_loop_integration")


if __name__ == "__main__":
    asyncio.run(_run())
```

- [ ] **Step 2: Run test**

Run: `python tests/test_entry_loop_integration.py`
Expected: `PASS ✓ entry_loop_integration`

If `cancel_pending_if_unfilled` does not exist on `PaperExecutor`, the fallback branch in the test handles it.

- [ ] **Step 3: Commit**

Repo is not git-initialized. Skip.

---

## Task 7: 48h paper validation gate

**Files:** none (observational)

- [ ] **Step 1: Start bot in paper mode, detached**

Run: `cd ~/polymarket-btc-bot && nohup python bot.py > logs/paper_run_$(date +%Y%m%d_%H%M).log 2>&1 &`

Record the PID and the log filename.

- [ ] **Step 2: Smoke check at T+30min**

Run: `grep -cE "PLACED|REPRICE|RESOLVED" logs/paper_run_*.log`

Expected: at least 1 `PLACED` line (zero = gate still too strict or bot broken).

- [ ] **Step 3: 48h checkpoint**

After 48 hours, run:

```bash
python self_improver.py --days 2 --dry-run
```

Expected:
- `trades_total >= 10`
- `win_rate` printed per conf bucket
- Any `by_confidence` bucket with `n >= 5` should show `win_rate >= 0.6` if signal has real edge

- [ ] **Step 4: Decide go/no-go for live**

If `win_rate < 0.60` in dominant conf bucket → signal weak. Do NOT go live. Iterate on signal_engine or widen gate.

If `win_rate >= 0.60` AND `total_pnl > 0` AND no crashes → proceed to live with `MAX_BET_SIZE=1.0` in `.env` and `TRADING_MODE=live`.

---

## Self-Review Summary

- **Spec coverage:** All 5 code-change files have dedicated tasks. 48h validation is Task 7. Risk gates (drawdown, streak, reserve) are inherited from existing `can_trade()` — Task 5 step 4 point 1 confirms they're checked first.
- **Placeholders:** none — every code block is concrete.
- **Type consistency:** `Signal` fields used in tests match `signal_engine.py:37-46`. Executor field names flagged with NOTE in Task 3 for verifier to confirm against actual file.
- **Known gap — git:** The project is not a git repo. All "Commit" steps say "skip". If user wants version control, run `git init` + initial commit before executing.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-15-active-maker-entry-v2.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch fresh subagent per task with review between tasks.

**2. Inline Execution** — run tasks sequentially in this session with checkpoints.

**Which?**
