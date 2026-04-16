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
