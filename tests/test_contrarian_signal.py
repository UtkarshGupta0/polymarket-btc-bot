"""Unit tests for compute_contrarian_signal."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from price_feed import PriceState
from signal_engine import compute_contrarian_signal


def _state() -> PriceState:
    s = PriceState()
    s.current_price = 80000.0
    s.window_open_price = 80000.0
    return s


def test_below_threshold_returns_none() -> None:
    """Both asks below threshold (0.90) -> None."""
    sig = compute_contrarian_signal(_state(), ask_up=0.55, ask_down=0.45)
    assert sig is None


def test_high_up_ask_returns_down_signal() -> None:
    """ask_up >= 0.90 -> signal targets DOWN (underdog)."""
    sig = compute_contrarian_signal(_state(), ask_up=0.92, ask_down=0.08)
    assert sig is not None
    assert sig.direction == "DOWN"
    assert sig.suggested_price == 0.08
    assert abs(sig.confidence - 0.08) < 1e-9
    assert sig.expected_value == 0.0
    assert "contrarian" in sig.rationale.lower()


def test_high_down_ask_returns_up_signal() -> None:
    """ask_down >= 0.90 -> signal targets UP (underdog)."""
    sig = compute_contrarian_signal(_state(), ask_up=0.07, ask_down=0.93)
    assert sig is not None
    assert sig.direction == "UP"
    assert sig.suggested_price == 0.07
    assert abs(sig.confidence - 0.07) < 1e-9


def test_zero_ask_returns_none() -> None:
    """Stale/missing ask (0.0) -> None even if other side passes threshold."""
    sig = compute_contrarian_signal(_state(), ask_up=0.95, ask_down=0.0)
    assert sig is None
    sig2 = compute_contrarian_signal(_state(), ask_up=0.0, ask_down=0.95)
    assert sig2 is None


def test_both_above_threshold_picks_higher_favourite() -> None:
    """Degenerate data (sum > 1): pick higher ask as favourite, fade other side."""
    sig = compute_contrarian_signal(_state(), ask_up=0.95, ask_down=0.92)
    assert sig is not None
    assert sig.direction == "DOWN"  # ask_up is higher, so UP is favourite, fade by buying DOWN
    assert sig.suggested_price == 0.92
