"""validate_gates.py — loader filters WIN/LOSS only."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _synth_trade(outcome: str, **overrides) -> dict:
    base = {
        "outcome": outcome,
        "confidence": 0.65,
        "entry_price": 0.55,
        "time_iso": "2026-04-20T12:00:00+00:00",
        "delta_pct": 0.0003,
        "pnl": 1.0 if outcome == "WIN" else -1.0,
    }
    base.update(overrides)
    return base


def test_load_trades() -> None:
    from scripts.validate_gates import load_trades

    with tempfile.TemporaryDirectory() as d:
        log_dir = Path(d)
        # File 1: mix of outcomes
        (log_dir / "trades_20260420.json").write_text(json.dumps({
            "session": {"mode": "paper"},
            "trades": [
                _synth_trade("WIN"),
                _synth_trade("LOSS"),
                _synth_trade("SKIPPED"),
                _synth_trade("UNFILLED"),
            ],
        }))
        # File 2: one more WIN
        (log_dir / "trades_20260421.json").write_text(json.dumps({
            "session": {"mode": "paper"},
            "trades": [_synth_trade("WIN")],
        }))
        # File that should NOT match glob
        (log_dir / "other.json").write_text("{}")

        trades = load_trades(log_dir)
        assert len(trades) == 3, f"expected 3 WIN/LOSS trades, got {len(trades)}"
        assert all(t["outcome"] in {"WIN", "LOSS"} for t in trades)


def test_gate_pass() -> None:
    from scripts.validate_gates import gate_pass, GATES

    # Thresholds in this project: MIN_CONFIDENCE=0.55, MIN_EDGE=0.03,
    # TRADING_HOURS_BLOCK={0,2,3,20,21}, MIN_DELTA_PCT=0.0002.
    # A trade that passes every gate:
    # confidence=0.70, entry_price=0.60 -> edge = 0.70 - 0.61 = 0.09 >= 0.03.
    # hour=12 (not blocked), |delta|=0.0005 >= 0.0002.
    good = {
        "confidence": 0.70,
        "entry_price": 0.60,
        "time_iso": "2026-04-20T12:00:00+00:00",
        "delta_pct": 0.0005,
    }
    assert gate_pass(good, active=set(GATES)), "good trade should pass all 4"

    # Confidence below MIN_CONFIDENCE=0.55 (0.54).
    # entry_price lowered to 0.40 so edge = 0.54 - 0.41 = 0.13 still passes edge gate.
    low_conf = {**good, "confidence": 0.54, "entry_price": 0.40}
    assert not gate_pass(low_conf, active={"min_confidence"})
    assert gate_pass(low_conf, active={"min_edge"})  # edge still fine

    # Edge below MIN_EDGE=0.03: conf=0.62, entry=0.60 -> edge=0.01.
    # conf=0.62 > 0.55 so min_confidence gate still passes.
    low_edge = {**good, "confidence": 0.62, "entry_price": 0.60}
    assert not gate_pass(low_edge, active={"min_edge"})
    assert gate_pass(low_edge, active={"min_confidence"})

    # Blocked hour (UTC 02).
    blocked_hr = {**good, "time_iso": "2026-04-20T02:15:00+00:00"}
    assert not gate_pass(blocked_hr, active={"hours_block"})
    assert gate_pass(blocked_hr, active={"min_confidence"})

    # Delta below MIN_DELTA_PCT=0.0002.
    low_delta = {**good, "delta_pct": 0.00005}
    assert not gate_pass(low_delta, active={"min_delta"})
    assert gate_pass(low_delta, active={"min_confidence"})

    # Empty active set -> always pass.
    assert gate_pass(low_conf, active=set())


def run() -> None:
    test_load_trades()
    test_gate_pass()
    print("PASS ✓ validate_gates.{load_trades,gate_pass}")


if __name__ == "__main__":
    run()
