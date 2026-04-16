"""Test reprice() atomically cancels old order and places new at updated price."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import PaperExecutor


def main() -> None:
    ex = PaperExecutor()

    # Place initial order
    t1 = ex.place_order(
        window_ts=1000, direction="UP", confidence=0.75,
        entry_price=0.60, size_usdc=5.0, token_id="tok_up",
        btc_open=80000.0, seconds_to_close=30.0,
    )
    assert t1 is not None, "initial place failed"
    assert t1.entry_price == 0.60

    # Reprice to new ask
    t2 = ex.reprice(window_ts=1000, new_price=0.65)
    assert t2 is not None, "reprice returned None"
    assert t2.entry_price == 0.65, f"expected 0.65, got {t2.entry_price}"
    assert t2.size_usdc == 5.0, "size must carry over"
    assert t2.direction == "UP", "direction must carry over"
    # Old order removed — only one active
    assert ex.pending_trade(1000) is t2, "pending should be new trade"

    # Reprice when no active order => None
    t_noop = ex.reprice(window_ts=9999, new_price=0.70)
    assert t_noop is None, "reprice on unknown window must return None"

    # Reprice multiple times
    t3 = ex.reprice(window_ts=1000, new_price=0.70)
    assert t3 is not None
    assert t3.entry_price == 0.70
    assert ex.pending_trade(1000) is t3

    print("PASS ✓ reprice_logic")


if __name__ == "__main__":
    main()
