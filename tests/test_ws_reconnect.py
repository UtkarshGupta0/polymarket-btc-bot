"""Verify PriceFeed._run reconnects after transient WS error."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from price_feed import PriceFeed


async def main() -> None:
    feed = PriceFeed()

    # Force first attempt to raise, second to actually connect
    calls = {"n": 0}
    orig = feed._connect_and_stream

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("simulated drop")
        await orig()

    feed._connect_and_stream = flaky  # type: ignore
    feed.start()

    # Give it up to 15s to see tick after retry
    deadline = asyncio.get_event_loop().time() + 15
    while feed.state.current_price == 0.0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.2)

    await feed.stop()

    assert calls["n"] >= 2, f"expected >=2 connect attempts, got {calls['n']}"
    assert feed.state.current_price > 0, "no tick received after reconnect"
    print(f"PASS ✓ connect_attempts={calls['n']} first_price=${feed.state.current_price:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
