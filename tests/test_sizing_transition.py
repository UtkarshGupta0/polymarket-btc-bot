"""Sizing: flat $1 for first KELLY_ENABLE_AFTER trades, then Kelly."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CONFIG
from risk_manager import RiskManager


def main() -> None:
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
    # Verify Kelly path by checking negative-EV returns 0 (flat phase would return $1)
    size_low = rm.calculate_position_size(confidence=0.50, entry_price=0.90)
    assert size_low == 0.0, f"post-threshold negative-EV must return 0 (Kelly path), got {size_low}"
    print("PASS ✓ sizing_transition")


if __name__ == "__main__":
    main()
