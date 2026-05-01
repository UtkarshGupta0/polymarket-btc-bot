"""Discovery: slug-filter BTC 5-min markets from a synthetic gamma payload."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_book_capture.discovery import filter_btc_5m, parse_market_row


def test_parse_market_row_extracts_window_ts() -> None:
    row = {
        "market_slug": "btc-updown-5m-1735689600",
        "condition_id": "0xabc",
        "token1": "111",
        "token2": "222",
    }
    m = parse_market_row(row)
    assert m is not None
    assert m.btc_window_ts == 1735689600
    assert m.market_id == "0xabc"
    assert m.token_up == "111"
    assert m.token_down == "222"


def test_filter_btc_5m_skips_non_btc_slugs() -> None:
    rows = [
        {"market_slug": "btc-updown-5m-1735689600", "condition_id": "0x1",
         "token1": "1", "token2": "2"},
        {"market_slug": "btc-updown-15m-1735689600", "condition_id": "0x2",
         "token1": "3", "token2": "4"},
        {"market_slug": "eth-updown-5m-1735689600", "condition_id": "0x3",
         "token1": "5", "token2": "6"},
        {"market_slug": "btc-updown-5m-XX", "condition_id": "0x4",
         "token1": "7", "token2": "8"},
    ]
    out = filter_btc_5m(rows)
    assert len(out) == 1
    assert out[0].market_id == "0x1"


def test_filter_btc_5m_handles_missing_fields() -> None:
    rows = [
        {"market_slug": "btc-updown-5m-1735689600"},  # missing tokens
        {"condition_id": "0xnope"},                   # missing slug
    ]
    out = filter_btc_5m(rows)
    assert out == []
