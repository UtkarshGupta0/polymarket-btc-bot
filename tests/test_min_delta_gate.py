"""MIN_DELTA_PCT gate in gate_vs_market."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> None:
    import config as cfg_mod
    import signal_engine
    from signal_engine import Signal, gate_vs_market

    # Base: a signal that would pass the edge gate.
    sig = Signal(
        direction="UP",
        confidence=0.65,
        suggested_price=0.55,
        rationale="test",
        timestamp=0.0,
        window_delta=0.0005,   # 0.05%
        seconds_to_close=30.0,
        expected_value=0.10,
    )
    ask_up, ask_down = 0.60, 0.40

    # --- Case 1: min_delta_pct=0 (off) -> passes regardless of small delta.
    orig = cfg_mod.CONFIG
    try:
        cfg_mod.CONFIG = replace(orig, min_delta_pct=0.0)
        signal_engine.CONFIG = cfg_mod.CONFIG
        tiny = replace(sig, window_delta=0.000001)  # 0.0001%
        assert gate_vs_market(tiny, ask_up, ask_down), "off mode must not gate on delta"

        # --- Case 2: min_delta_pct=0.0002, signal delta=0.0005 -> passes.
        cfg_mod.CONFIG = replace(orig, min_delta_pct=0.0002)
        signal_engine.CONFIG = cfg_mod.CONFIG
        assert gate_vs_market(sig, ask_up, ask_down), "0.05% should clear 0.02% gate"

        # --- Case 3: min_delta_pct=0.0002, signal delta=0.00005 -> rejected.
        tiny = replace(sig, window_delta=0.00005)  # 0.005% < 0.02%
        assert not gate_vs_market(tiny, ask_up, ask_down), "0.005% should fail 0.02% gate"

        # --- Case 4: negative delta of same magnitude is evaluated via abs.
        neg = replace(sig, direction="DOWN", window_delta=-0.0005)
        assert gate_vs_market(neg, ask_up, ask_down), "abs is used, DOWN 0.05% should pass"
        neg_tiny = replace(sig, direction="DOWN", window_delta=-0.00005)
        assert not gate_vs_market(neg_tiny, ask_up, ask_down), "DOWN 0.005% should fail"
    finally:
        cfg_mod.CONFIG = orig
        signal_engine.CONFIG = orig

    # Validate bounds on config.
    try:
        replace(orig, min_delta_pct=0.01).validate()
        raise AssertionError("min_delta_pct=0.01 should be rejected (upper bound)")
    except AssertionError as e:
        if "MIN_DELTA_PCT" not in str(e):
            raise
    try:
        replace(orig, min_delta_pct=-0.001).validate()
        raise AssertionError("negative min_delta_pct should be rejected")
    except AssertionError as e:
        if "MIN_DELTA_PCT" not in str(e):
            raise

    print("PASS \u2713 min-delta gate")


if __name__ == "__main__":
    run()
