"""Schema round-trip tests for BookEvent."""
from __future__ import annotations

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_book_capture.schema import BookEvent, BookLevel, MarketInfo


def test_book_event_to_json_round_trips() -> None:
    ev = BookEvent(
        ts=1735689812.345,
        market_id="0xabc",
        token_id="1234567890",
        side="UP",
        btc_window_ts=1735689600,
        bids=[BookLevel(price=0.92, size=50.0), BookLevel(price=0.91, size=20.0)],
        asks=[BookLevel(price=0.94, size=30.0)],
        n_levels=10,
    )
    line = ev.to_json()
    parsed = json.loads(line)
    assert parsed["ts"] == 1735689812.345
    assert parsed["market_id"] == "0xabc"
    assert parsed["token_id"] == "1234567890"
    assert parsed["side"] == "UP"
    assert parsed["btc_window_ts"] == 1735689600
    assert parsed["bids"] == [{"price": 0.92, "size": 50.0}, {"price": 0.91, "size": 20.0}]
    assert parsed["asks"] == [{"price": 0.94, "size": 30.0}]
    assert parsed["n_levels"] == 10


def test_market_info_carries_token_pair() -> None:
    m = MarketInfo(
        market_id="0xdef",
        slug="btc-updown-5m-1735689600",
        btc_window_ts=1735689600,
        token_up="111",
        token_down="222",
    )
    assert m.btc_window_ts == 1735689600
    assert m.token_up == "111" and m.token_down == "222"
