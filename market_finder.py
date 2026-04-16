"""Deterministic Polymarket BTC 5-min market discovery."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import CONFIG

logger = logging.getLogger(__name__)

INTERVAL_5M = 300


@dataclass
class MarketWindow:
    start_ts: int
    end_ts: int
    slug: str
    event_url: str
    market_id: Optional[str] = None
    condition_id: Optional[str] = None
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None
    up_best_bid: Optional[float] = None
    up_best_ask: Optional[float] = None
    down_best_bid: Optional[float] = None
    down_best_ask: Optional[float] = None
    question: Optional[str] = None


def get_current_window_start(now: float | None = None) -> int:
    n = int(now if now is not None else time.time())
    return n - (n % INTERVAL_5M)


def get_next_window_start(now: float | None = None) -> int:
    return get_current_window_start(now) + INTERVAL_5M


def build_slug(start_ts: int) -> str:
    return f"btc-updown-5m-{start_ts}"


def build_event_url(start_ts: int) -> str:
    return f"https://polymarket.com/event/{build_slug(start_ts)}"


def _extract_token_ids(raw: object) -> list[str]:
    """clobTokenIds may be list or JSON string. Return list of string IDs."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


class MarketFinder:
    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._own_session = session is None
        self._cache: dict[int, MarketWindow] = {}

    async def __aenter__(self) -> "MarketFinder":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    # --- market lookup ---

    async def find_market(self, start_ts: int, refresh: bool = False) -> MarketWindow:
        if not refresh and start_ts in self._cache:
            return self._cache[start_ts]

        mw = MarketWindow(
            start_ts=start_ts,
            end_ts=start_ts + INTERVAL_5M,
            slug=build_slug(start_ts),
            event_url=build_event_url(start_ts),
        )

        data = await self._fetch_event_by_slug(mw.slug)
        if data is None:
            data = await self._fallback_recent_markets(start_ts)

        if data is not None:
            self._populate_from_market(mw, data)

        self._cache[start_ts] = mw
        return mw

    async def _fetch_event_by_slug(self, slug: str) -> Optional[dict]:
        url = f"{CONFIG.gamma_api_url}/events"
        params = {"slug": slug, "limit": 1}
        try:
            sess = await self._get_session()
            async with sess.get(url, params=params,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    logger.debug(f"gamma /events {slug} HTTP {r.status}")
                    return None
                payload = await r.json()
        except Exception as e:
            logger.warning(f"gamma /events error: {e}")
            return None

        if not payload:
            return None
        # Payload is list of events
        events = payload if isinstance(payload, list) else payload.get("data", [])
        if not events:
            return None
        markets = events[0].get("markets") or []
        if not markets:
            return None
        return markets[0]

    async def _fallback_recent_markets(self, start_ts: int) -> Optional[dict]:
        url = f"{CONFIG.gamma_api_url}/markets"
        params = {
            "tag_id": "crypto",
            "active": "true",
            "limit": 50,
            "order": "createdAt",
            "ascending": "false",
        }
        try:
            sess = await self._get_session()
            async with sess.get(url, params=params,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                payload = await r.json()
        except Exception as e:
            logger.warning(f"gamma /markets fallback error: {e}")
            return None

        markets = payload if isinstance(payload, list) else payload.get("data", [])
        needle = str(start_ts)
        for m in markets:
            slug = (m.get("slug") or m.get("marketSlug") or "") or ""
            if needle in slug and "btc-updown-5m" in slug:
                return m
        return None

    def _populate_from_market(self, mw: MarketWindow, m: dict) -> None:
        mw.market_id = str(m.get("id") or m.get("marketId") or "") or None
        mw.condition_id = m.get("conditionId") or m.get("condition_id")
        mw.question = m.get("question") or m.get("description")
        token_ids = _extract_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
        if len(token_ids) >= 2:
            mw.up_token_id = token_ids[0]
            mw.down_token_id = token_ids[1]

    # --- orderbook ---

    async def fetch_orderbook(self, token_id: str) -> dict | None:
        url = f"{CONFIG.clob_api_url}/book"
        params = {"token_id": token_id}
        try:
            sess = await self._get_session()
            async with sess.get(url, params=params,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    logger.debug(f"clob /book HTTP {r.status}")
                    return None
                return await r.json()
        except Exception as e:
            logger.warning(f"clob /book error: {e}")
            return None

    @staticmethod
    def _best_bid_ask(book: dict | None) -> tuple[Optional[float], Optional[float]]:
        if not book:
            return None, None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        # bids sorted desc by price; asks asc. But sort defensively.
        best_bid = None
        best_ask = None
        if bids:
            try:
                best_bid = max(float(b["price"]) for b in bids)
            except (KeyError, ValueError):
                pass
        if asks:
            try:
                best_ask = min(float(a["price"]) for a in asks)
            except (KeyError, ValueError):
                pass
        return best_bid, best_ask

    async def refresh_prices(self, mw: MarketWindow) -> MarketWindow:
        tasks = []
        if mw.up_token_id:
            tasks.append(self.fetch_orderbook(mw.up_token_id))
        else:
            tasks.append(asyncio.sleep(0, result=None))  # placeholder
        if mw.down_token_id:
            tasks.append(self.fetch_orderbook(mw.down_token_id))
        else:
            tasks.append(asyncio.sleep(0, result=None))

        up_book, down_book = await asyncio.gather(*tasks, return_exceptions=False)
        mw.up_best_bid, mw.up_best_ask = self._best_bid_ask(
            up_book if isinstance(up_book, dict) else None)
        mw.down_best_bid, mw.down_best_ask = self._best_bid_ask(
            down_book if isinstance(down_book, dict) else None)
        return mw


# --- standalone test ---

async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start_ts = get_current_window_start()
    print(f"Current window start: {start_ts}")
    print(f"Slug: {build_slug(start_ts)}")
    print(f"Event URL: {build_event_url(start_ts)}")
    print(f"Seconds into window: {int(time.time()) - start_ts}")
    print(f"Seconds remaining:   {start_ts + INTERVAL_5M - int(time.time())}")
    print()

    async with MarketFinder() as mf:
        # Try current window, then previous (more likely indexed)
        for ts in (start_ts, start_ts - INTERVAL_5M, start_ts - 2 * INTERVAL_5M):
            print(f"--- Fetching market for ts={ts} ({build_slug(ts)}) ---")
            mw = await mf.find_market(ts, refresh=True)
            print(f"market_id:    {mw.market_id}")
            print(f"condition_id: {mw.condition_id}")
            print(f"question:     {mw.question}")
            print(f"up_token_id:   {mw.up_token_id}")
            print(f"down_token_id: {mw.down_token_id}")

            if mw.up_token_id and mw.down_token_id:
                await mf.refresh_prices(mw)
                print(
                    f"UP   bid=${mw.up_best_bid}  ask=${mw.up_best_ask}")
                print(
                    f"DOWN bid=${mw.down_best_bid} ask=${mw.down_best_ask}")
                print()
                return
            print("(no token IDs — trying previous window)\n")

        print("WARN: no markets found in last 3 windows.")


if __name__ == "__main__":
    asyncio.run(_main())
