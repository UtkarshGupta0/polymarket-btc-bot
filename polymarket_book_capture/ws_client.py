"""Polymarket WebSocket book-channel client.

Schema reference (best guess; iterate via `--dump-raw` if wrong):

    Subscribe frame  (we send):
        {"type": "MARKET", "assets_ids": ["<token_id>", ...]}

    Book event       (we receive):
        {
            "event_type": "book",
            "asset_id": "<token_id>",
            "market":   "<condition_id>",
            "timestamp": "<unix_ms_str>",
            "bids":     [{"price": "0.92", "size": "50"}, ...],
            "asks":     [{"price": "0.94", "size": "30"}, ...]
        }

    Other event_type values ("trade", "price_change", ...) are dropped
    by parse_book_frame for the MVP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Iterable, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

from polymarket_book_capture.schema import BookEvent, BookLevel, MarketInfo

logger = logging.getLogger(__name__)


def parse_book_frame(
    frame: dict,
    registry: dict[str, MarketInfo],
    n_levels: int,
) -> Optional[BookEvent]:
    """Convert a single WS book frame into a BookEvent. Drop non-book events
    and unknown asset_ids."""
    if frame.get("event_type") != "book":
        return None

    asset_id = str(frame.get("asset_id") or "")
    market_info = registry.get(asset_id)
    if market_info is None:
        return None

    side = "UP" if asset_id == market_info.token_up else "DOWN"

    try:
        ts_ms = int(frame.get("timestamp") or "0")
    except ValueError:
        return None
    ts = ts_ms / 1000.0

    def _levels(items) -> list[BookLevel]:
        out: list[BookLevel] = []
        for it in (items or [])[:n_levels]:
            try:
                p = float(it.get("price"))
                s = float(it.get("size"))
            except (TypeError, ValueError):
                continue
            out.append(BookLevel(price=p, size=s))
        return out

    return BookEvent(
        ts=ts,
        market_id=str(frame.get("market") or market_info.market_id),
        token_id=asset_id,
        side=side,
        btc_window_ts=market_info.btc_window_ts,
        bids=_levels(frame.get("bids")),
        asks=_levels(frame.get("asks")),
        n_levels=n_levels,
    )


class BookWSClient:
    """Owns a single Polymarket market-channel WebSocket connection.

    Auto-reconnects with exponential backoff (1s, 2s, ..., cap 30s) on
    ConnectionClosed. Subscriptions are resent on reconnect.
    """

    def __init__(self, url: str, n_levels: int = 10) -> None:
        self._url = url
        self._n_levels = n_levels
        self._subscribed: set[str] = set()
        self._raw_dump_fh = None  # set by .enable_raw_dump(path)

    def enable_raw_dump(self, path: Union[str, os.PathLike]) -> None:
        self._raw_dump_fh = open(path, "a", encoding="utf-8")

    def close(self) -> None:
        """Close the raw-dump file handle if one was opened. Idempotent."""
        if self._raw_dump_fh is not None:
            try:
                self._raw_dump_fh.close()
            finally:
                self._raw_dump_fh = None

    async def _subscribe(self, ws: Any, asset_ids: Iterable[str]) -> None:
        msg = {"type": "MARKET", "assets_ids": list(asset_ids)}
        await ws.send(json.dumps(msg))

    async def run(
        self,
        registry: dict[str, MarketInfo],
        sub_queue: "asyncio.Queue[tuple[str, str]]",
    ) -> AsyncIterator[BookEvent]:
        """Connect, subscribe, yield BookEvents. Reconnects forever.

        sub_queue carries ("add", asset_id) and ("remove", asset_id) tuples
        emitted by discovery_loop.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._url, ping_interval=30) as ws:
                    if self._subscribed:
                        await self._subscribe(ws, self._subscribed)
                    backoff = 1.0
                    logger.info("ws connected: %s", self._url)
                    async for ev in self._stream(ws, registry, sub_queue):
                        yield ev
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                logger.warning("ws disconnected: %s; backoff=%.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _stream(
        self,
        ws: Any,
        registry: dict[str, MarketInfo],
        sub_queue: "asyncio.Queue[tuple[str, str]]",
    ) -> AsyncIterator[BookEvent]:
        recv_task = asyncio.create_task(ws.recv())
        sub_task = asyncio.create_task(sub_queue.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {recv_task, sub_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if recv_task in done:
                    raw = recv_task.result()
                    if self._raw_dump_fh is not None:
                        self._raw_dump_fh.write(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                        self._raw_dump_fh.write("\n")
                        self._raw_dump_fh.flush()
                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError):
                        logger.error("ws non-json frame: %s", str(raw)[:200])
                        recv_task = asyncio.create_task(ws.recv())
                        continue
                    if isinstance(frame, list):
                        for f in frame:
                            ev = parse_book_frame(f, registry, self._n_levels)
                            if ev is not None:
                                yield ev
                    else:
                        ev = parse_book_frame(frame, registry, self._n_levels)
                        if ev is not None:
                            yield ev
                    recv_task = asyncio.create_task(ws.recv())
                if sub_task in done:
                    op, asset_id = sub_task.result()
                    if op == "add" and asset_id not in self._subscribed:
                        self._subscribed.add(asset_id)
                        await self._subscribe(ws, [asset_id])
                        logger.info("ws subscribed: %s", asset_id)
                    elif op == "remove" and asset_id in self._subscribed:
                        self._subscribed.discard(asset_id)
                        # Polymarket has no per-asset unsub; rely on resubscribe-on-reconnect to drop.
                        logger.info("ws unsubscribe-pending: %s", asset_id)
                    sub_task = asyncio.create_task(sub_queue.get())
        finally:
            if not recv_task.done(): recv_task.cancel()
            if not sub_task.done(): sub_task.cancel()
