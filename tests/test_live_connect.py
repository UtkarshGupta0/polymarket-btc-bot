"""Read-only live connectivity test. Skips if POLYMARKET_PRIVATE_KEY unset.

Derives API creds, queries USDC balance. Makes NO orders.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CONFIG


def main() -> int:
    if not CONFIG.polymarket_private_key:
        print("SKIP: POLYMARKET_PRIVATE_KEY not set in .env")
        return 0

    # Temporarily force live construction without changing trading_mode
    from executor import LiveExecutor
    try:
        ex = LiveExecutor()
    except Exception as e:
        print(f"FAIL: LiveExecutor init: {e}")
        return 1

    bal = ex.get_usdc_balance()
    if bal is None:
        print("FAIL: could not read USDC balance")
        return 1
    print(f"PASS ✓ USDC balance: ${bal:.6f}")

    # Check positions
    try:
        # py-clob-client: get_positions may not exist in all versions; guard
        if hasattr(ex.client, "get_positions"):
            positions = ex.client.get_positions()
            print(f"positions: {positions}")
    except Exception as e:
        print(f"positions query error (non-fatal): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
