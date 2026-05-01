"""Discover active BTC 5-min Polymarket markets via the gamma API."""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

import aiohttp

from config import CONFIG
from polymarket_book_capture.schema import MarketInfo

logger = logging.getLogger(__name__)

BTC_5M_SLUG_RE = re.compile(r"^btc-updown-5m-(\d{10})$")


def parse_market_row(row: dict) -> Optional[MarketInfo]:
    """Convert one gamma /markets row into a MarketInfo, or None if not BTC 5-min."""
    slug = row.get("market_slug") or row.get("slug") or ""
    m = BTC_5M_SLUG_RE.match(slug)
    if m is None:
        return None
    cid = row.get("condition_id") or row.get("conditionId")
    token_up = row.get("token1")
    token_down = row.get("token2")
    if not (cid and token_up and token_down):
        return None
    try:
        ts = int(m.group(1))
    except ValueError:
        return None
    return MarketInfo(
        market_id=str(cid),
        slug=slug,
        btc_window_ts=ts,
        token_up=str(token_up),
        token_down=str(token_down),
    )


def filter_btc_5m(rows: Iterable[dict]) -> list[MarketInfo]:
    out: list[MarketInfo] = []
    for r in rows:
        info = parse_market_row(r)
        if info is not None:
            out.append(info)
    return out


async def find_btc_5m_markets(
    session: Optional[aiohttp.ClientSession] = None,
) -> list[MarketInfo]:
    """Fetch active markets from gamma and filter to BTC 5-min."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        url = f"{CONFIG.gamma_api_url}/markets"
        params = {"active": "true", "closed": "false", "limit": "500"}
        async with session.get(url, params=params, timeout=15) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        return filter_btc_5m(rows)
    finally:
        if own_session:
            await session.close()
