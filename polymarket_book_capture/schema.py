"""Dataclasses + JSON serialization for book-capture events."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class BookEvent:
    ts: float                       # unix-epoch seconds, fractional
    market_id: str                  # Polymarket condition_id (hex)
    token_id: str                   # outcome ERC-1155 token id (decimal string)
    side: str                       # "UP" | "DOWN"
    btc_window_ts: int              # parsed from slug suffix; identifies the 5-min window
    bids: list[BookLevel]
    asks: list[BookLevel]
    n_levels: int                   # configured max levels per side

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, separators=(",", ":"))


@dataclass(frozen=True)
class MarketInfo:
    market_id: str                  # condition_id
    slug: str                       # e.g. btc-updown-5m-1735689600
    btc_window_ts: int              # unix sec parsed from slug suffix
    token_up: str                   # token1 (the "UP" outcome's token id)
    token_down: str                 # token2 (the "DOWN" outcome's token id)
