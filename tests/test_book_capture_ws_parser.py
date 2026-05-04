"""WS book-event parser: synthetic frame -> BookEvent."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_book_capture.schema import MarketInfo
from polymarket_book_capture.ws_client import parse_book_frame


_REGISTRY = {
    "1234567890": MarketInfo(
        market_id="0xabc", slug="btc-updown-5m-1735689600",
        btc_window_ts=1735689600, token_up="1234567890", token_down="999",
    ),
    "999": MarketInfo(
        market_id="0xabc", slug="btc-updown-5m-1735689600",
        btc_window_ts=1735689600, token_up="1234567890", token_down="999",
    ),
}


def test_parse_book_frame_top10() -> None:
    frame = {
        "event_type": "book",
        "asset_id": "1234567890",
        "market": "0xabc",
        "timestamp": "1735689812345",
        "bids": [
            {"price": "0.92", "size": "50"},
            {"price": "0.91", "size": "20"},
        ],
        "asks": [{"price": "0.94", "size": "30"}],
    }
    ev = parse_book_frame(frame, registry=_REGISTRY, n_levels=10)
    assert ev is not None
    assert ev.market_id == "0xabc"
    assert ev.token_id == "1234567890"
    assert ev.side == "UP"
    assert ev.btc_window_ts == 1735689600
    assert abs(ev.ts - 1735689812.345) < 1e-3
    assert len(ev.bids) == 2
    assert ev.bids[0].price == 0.92 and ev.bids[0].size == 50.0
    assert ev.asks[0].price == 0.94


def test_parse_book_frame_truncates_to_n_levels() -> None:
    frame = {
        "event_type": "book",
        "asset_id": "1234567890",
        "market": "0xabc",
        "timestamp": "1735689812345",
        "bids": [{"price": str(0.9 - i * 0.01), "size": "5"} for i in range(15)],
        "asks": [{"price": str(0.95 + i * 0.01), "size": "5"} for i in range(15)],
    }
    ev = parse_book_frame(frame, registry=_REGISTRY, n_levels=10)
    assert ev is not None
    assert len(ev.bids) == 10
    assert len(ev.asks) == 10


def test_parse_book_frame_returns_none_for_unknown_token() -> None:
    frame = {
        "event_type": "book", "asset_id": "deadbeef", "market": "0x",
        "timestamp": "0", "bids": [], "asks": [],
    }
    ev = parse_book_frame(frame, registry=_REGISTRY, n_levels=10)
    assert ev is None


def test_parse_book_frame_returns_none_for_non_book_event() -> None:
    frame = {"event_type": "trade", "asset_id": "1234567890"}
    ev = parse_book_frame(frame, registry=_REGISTRY, n_levels=10)
    assert ev is None


def test_parse_book_frame_assigns_down_side_from_token2() -> None:
    frame = {
        "event_type": "book", "asset_id": "999", "market": "0xabc",
        "timestamp": "1735689812345",
        "bids": [{"price": "0.08", "size": "100"}],
        "asks": [{"price": "0.10", "size": "50"}],
    }
    ev = parse_book_frame(frame, registry=_REGISTRY, n_levels=10)
    assert ev is not None
    assert ev.side == "DOWN"
