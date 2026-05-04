"""Discover active BTC 5-min Polymarket markets via the gamma API.

Polymarket's BTC up/down 5-minute series is addressable as one event per
5-minute window:

    GET /events?slug=btc-updown-5m-<unix_ts_5m_boundary>

Each event has a `markets` list (length 1 in practice) whose entry carries:

    {
      "slug":          "btc-updown-5m-<ts>",
      "conditionId":   "0x...",
      "clobTokenIds":  "[\"<token_up>\", \"<token_down>\"]"   # JSON-encoded
    }

`find_btc_5m_markets` enumerates the current 5-minute window plus the next
five future windows so subscribers can pre-position before each window opens.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterable, Optional

import aiohttp

from config import CONFIG
from polymarket_book_capture.schema import MarketInfo

logger = logging.getLogger(__name__)

BTC_5M_SLUG_RE = re.compile(r"^btc-updown-5m-(\d{10})$")
INTERVAL_5M = 300
WINDOWS_AHEAD = 6  # current window + next 5


def _parse_token_ids(row: dict) -> Optional[tuple[str, str]]:
    """Return (token_up, token_down) from a gamma market row.

    Tolerates both the gamma-native `clobTokenIds` (JSON-encoded string of a
    2-element list) and the synthetic `token1`/`token2` shape used in tests.
    """
    raw = row.get("clobTokenIds") or row.get("clob_token_ids")
    tokens: Optional[list] = None
    if isinstance(raw, str):
        try:
            tokens = json.loads(raw)
        except (ValueError, TypeError):
            tokens = None
    elif isinstance(raw, list):
        tokens = raw
    if tokens and len(tokens) >= 2:
        return str(tokens[0]), str(tokens[1])

    t_up = row.get("token1") or row.get("token_up")
    t_dn = row.get("token2") or row.get("token_down")
    if t_up and t_dn:
        return str(t_up), str(t_dn)
    return None


def parse_market_row(row: dict) -> Optional[MarketInfo]:
    """Convert one gamma market row into a MarketInfo, or None if not BTC 5-min."""
    slug = row.get("slug") or row.get("market_slug") or ""
    m = BTC_5M_SLUG_RE.match(slug)
    if m is None:
        return None
    cid = row.get("conditionId") or row.get("condition_id")
    if not cid:
        return None
    tokens = _parse_token_ids(row)
    if tokens is None:
        return None
    try:
        ts = int(m.group(1))
    except ValueError:
        return None
    return MarketInfo(
        market_id=str(cid),
        slug=slug,
        btc_window_ts=ts,
        token_up=tokens[0],
        token_down=tokens[1],
    )


def filter_btc_5m(rows: Iterable[dict]) -> list[MarketInfo]:
    """Filter an iterable of gamma rows to BTC-5m MarketInfos."""
    out: list[MarketInfo] = []
    for r in rows:
        info = parse_market_row(r)
        if info is not None:
            out.append(info)
    return out


def _current_window_base(now: Optional[float] = None) -> int:
    """Return the unix-ts of the 5-minute boundary at-or-before `now`."""
    t = int(now if now is not None else time.time())
    return (t // INTERVAL_5M) * INTERVAL_5M


async def _fetch_event_by_slug(
    session: aiohttp.ClientSession, slug: str
) -> list[dict]:
    """Return the `markets` list from `/events?slug=<slug>`, or [] on error."""
    url = f"{CONFIG.gamma_api_url}/events"
    params = {"slug": slug, "limit": "1"}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    except Exception as e:
        logger.warning("gamma /events %s: %s", slug, e)
        return []
    events = payload if isinstance(payload, list) else payload.get("data", [])
    if not events:
        return []
    return events[0].get("markets") or []


async def find_btc_5m_markets(
    session: Optional[aiohttp.ClientSession] = None,
    *,
    windows_ahead: int = WINDOWS_AHEAD,
    now: Optional[float] = None,
) -> list[MarketInfo]:
    """Fetch active BTC 5-min markets covering the current and upcoming windows."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        base = _current_window_base(now)
        out: list[MarketInfo] = []
        seen: set[int] = set()
        for i in range(windows_ahead):
            slug = f"btc-updown-5m-{base + i * INTERVAL_5M}"
            for m in await _fetch_event_by_slug(session, slug):
                info = parse_market_row(m)
                if info is not None and info.btc_window_ts not in seen:
                    seen.add(info.btc_window_ts)
                    out.append(info)
        return out
    finally:
        if own_session:
            await session.close()
