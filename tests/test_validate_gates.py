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


def run() -> None:
    test_load_trades()
    print("PASS ✓ validate_gates.load_trades")


if __name__ == "__main__":
    run()
