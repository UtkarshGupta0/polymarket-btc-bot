"""Hour-block gate in _evaluate_entry: blocked hour + no active trade => skip."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_bot(hours_block: frozenset[int]):
    import config as cfg_mod
    from dataclasses import replace
    cfg_mod.CONFIG = replace(
        cfg_mod.load_config(), trading_hours_block=hours_block
    )
    import importlib, bot as bot_mod
    importlib.reload(bot_mod)
    bot_mod.CONFIG = cfg_mod.CONFIG
    b = bot_mod.Bot()

    class StubRisk:
        class state:
            max_drawdown_hit = False
            consecutive_losses = 0
            day_utc = "2026-04-24"
        def can_trade(self): return True, ""
    b.risk_manager = StubRisk()
    return b, bot_mod


def run() -> None:
    # Use the actual current UTC hour so no clock mocking is needed.
    current_hour = datetime.now(timezone.utc).hour
    other_hour = (current_hour + 1) % 24

    # --- Case 1: hour IS blocked, NO active trade -> signal NOT computed.
    b, bot_mod = _make_bot(frozenset({current_hour}))
    class StubExec:
        def pending_trade(self, ts): return None
    b.executor = StubExec()
    with patch.object(bot_mod, "compute_signal") as mock_sig:
        asyncio.run(b._evaluate_entry(1776948000, 1776948300, 100))
        assert not mock_sig.called, "blocked hour should skip before signal compute"

    # --- Case 2: hour NOT blocked (we block a different hour) -> signal IS called.
    b, bot_mod = _make_bot(frozenset({other_hour}))
    b.executor = StubExec()
    with patch.object(bot_mod, "compute_signal", return_value=None) as mock_sig:
        asyncio.run(b._evaluate_entry(1776948000, 1776948300, 100))
        assert mock_sig.called, "non-blocked hour should proceed to signal"

    # --- Case 3: hour BLOCKED but active trade exists -> signal IS called
    # (must let existing trade flow through reprice/cancel logic).
    b, bot_mod = _make_bot(frozenset({current_hour}))
    class StubExecActive:
        class Fake:
            direction = "UP"; size_usdc = 1.0; entry_price = 0.5
        def pending_trade(self, ts): return StubExecActive.Fake()
    b.executor = StubExecActive()
    with patch.object(bot_mod, "compute_signal", return_value=None) as mock_sig:
        asyncio.run(b._evaluate_entry(1776948000, 1776948300, 100))
        assert mock_sig.called, "active trade should bypass hour-block gate"

    # --- Case 4: empty block set -> always proceeds.
    b, bot_mod = _make_bot(frozenset())
    b.executor = StubExec()
    with patch.object(bot_mod, "compute_signal", return_value=None) as mock_sig:
        asyncio.run(b._evaluate_entry(1776948000, 1776948300, 100))
        assert mock_sig.called, "empty block set should never gate"

    print("PASS \u2713 hours-block gate")


if __name__ == "__main__":
    run()
