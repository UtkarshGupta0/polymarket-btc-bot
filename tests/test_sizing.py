"""Sizing: pure Kelly from trade 1, with explicit too-small skip."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import MIN_SHARE_SIZE
from risk_manager import RiskManager


def test_trade_1_uses_kelly_not_flat_one_dollar() -> None:
    """First trade must size by Kelly, not the legacy flat $1."""
    from config import CONFIG
    rm = RiskManager(starting_balance=200.0)
    size = rm.calculate_position_size(confidence=0.85, entry_price=0.20)
    # b = 4, kelly_pct = 0.8125, target_pct = 0.203125, available = 195
    # raw size ~ $39.61, capped to MAX_BET_SIZE
    assert size == CONFIG.max_bet_size, \
        f"trade 1 should be Kelly (capped at MAX_BET_SIZE={CONFIG.max_bet_size}), got {size}"


def test_too_small_kelly_returns_zero_with_log(caplog) -> None:
    """When Kelly produces < MIN_SHARE_SIZE shares, return 0 and log at DEBUG."""
    import logging
    rm = RiskManager(starting_balance=30.0)
    with caplog.at_level(logging.DEBUG, logger="risk_manager"):
        size = rm.calculate_position_size(confidence=0.85, entry_price=0.80)
    assert size == 0.0, f"expected 0 (too small), got {size}"
    assert any("size too small" in r.message for r in caplog.records), \
        "expected DEBUG log line containing 'size too small'"


def test_kelly_above_min_shares_fills() -> None:
    """When Kelly produces >= MIN_SHARE_SIZE shares, return positive size."""
    rm = RiskManager(starting_balance=200.0)
    size = rm.calculate_position_size(confidence=0.85, entry_price=0.20)
    assert size > 0, f"expected positive size, got {size}"
    shares = round(size / 0.20, 2)
    assert shares >= MIN_SHARE_SIZE, \
        f"expected shares >= {MIN_SHARE_SIZE}, got {shares}"


def test_negative_ev_returns_zero() -> None:
    """confidence < entry_price implies negative Kelly => 0 (unchanged)."""
    rm = RiskManager(starting_balance=200.0)
    size = rm.calculate_position_size(confidence=0.50, entry_price=0.90)
    assert size == 0.0, f"expected 0 for negative-EV, got {size}"


def test_no_kelly_enable_after_attribute_on_config() -> None:
    """The config field must be gone after this change."""
    from config import CONFIG
    assert not hasattr(CONFIG, "kelly_enable_after"), \
        "CONFIG.kelly_enable_after must be removed"
