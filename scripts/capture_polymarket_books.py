"""Capture Polymarket BTC 5-min market book snapshots into daily JSONL.gz.

Usage:
    python scripts/capture_polymarket_books.py [--out data/books] [--dump-raw raw.log]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import aiohttp  # noqa: E402

from polymarket_book_capture.discovery import find_btc_5m_markets  # noqa: E402
from polymarket_book_capture.schema import BookEvent, MarketInfo  # noqa: E402
from polymarket_book_capture.writer import JsonlWriter  # noqa: E402
from polymarket_book_capture.ws_client import BookWSClient  # noqa: E402

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DISCOVERY_INTERVAL_SEC = 60
LOG_EVERY_N_EVENTS = 1000

logger = logging.getLogger("capture")


async def discovery_loop(
    registry: dict[str, MarketInfo],
    sub_queue: "asyncio.Queue[tuple[str, str]]",
    stop: asyncio.Event,
) -> None:
    """Poll gamma every DISCOVERY_INTERVAL_SEC, diff against registry, push deltas."""
    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            try:
                markets = await find_btc_5m_markets(session)
            except Exception as e:
                logger.warning("discovery: %s", e)
                await _sleep_or_stop(DISCOVERY_INTERVAL_SEC, stop)
                continue

            now_ts = time.time()
            wanted: dict[str, MarketInfo] = {}
            for m in markets:
                if m.btc_window_ts + 300 + 60 < now_ts:
                    continue
                wanted[m.token_up] = m
                wanted[m.token_down] = m

            for asset_id, m in wanted.items():
                if asset_id not in registry:
                    registry[asset_id] = m
                    await sub_queue.put(("add", asset_id))

            for asset_id in list(registry.keys()):
                if asset_id not in wanted:
                    del registry[asset_id]
                    await sub_queue.put(("remove", asset_id))

            await _sleep_or_stop(DISCOVERY_INTERVAL_SEC, stop)


async def _sleep_or_stop(sec: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=sec)
    except asyncio.TimeoutError:
        pass


async def writer_loop(
    writer: JsonlWriter,
    event_queue: "asyncio.Queue[BookEvent]",
    stop: asyncio.Event,
) -> None:
    """Drain event queue, append to writer, log heartbeat every N events."""
    n = 0
    started = time.time()
    try:
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(event_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            writer.append(ev)
            n += 1
            if n % LOG_EVERY_N_EVENTS == 0:
                uptime = time.time() - started
                logger.info("events_written=%d uptime_sec=%.1f", n, uptime)
    finally:
        writer.close()


async def ws_runner(
    client: BookWSClient,
    registry: dict[str, MarketInfo],
    sub_queue: "asyncio.Queue[tuple[str, str]]",
    event_queue: "asyncio.Queue[BookEvent]",
    stop: asyncio.Event,
) -> None:
    """Pump events from BookWSClient.run into event_queue."""
    async for ev in client.run(registry, sub_queue):
        if stop.is_set():
            return
        await event_queue.put(ev)


async def main_async(args: argparse.Namespace) -> int:
    registry: dict[str, MarketInfo] = {}
    sub_queue: "asyncio.Queue[tuple[str, str]]" = asyncio.Queue()
    event_queue: "asyncio.Queue[BookEvent]" = asyncio.Queue(maxsize=10000)
    stop = asyncio.Event()

    def _handle_stop(signame: str) -> None:
        logger.info("received %s; stopping", signame)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(getattr(signal, sig), _handle_stop, sig)

    writer = JsonlWriter(Path(args.out))
    client = BookWSClient(WS_URL, n_levels=10)
    if args.dump_raw:
        client.enable_raw_dump(args.dump_raw)
        logger.info("raw WS frames will be appended to %s", args.dump_raw)

    tasks = [
        asyncio.create_task(discovery_loop(registry, sub_queue, stop)),
        asyncio.create_task(ws_runner(client, registry, sub_queue, event_queue, stop)),
        asyncio.create_task(writer_loop(writer, event_queue, stop)),
    ]

    try:
        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        client.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/books", help="output directory")
    ap.add_argument("--dump-raw", default=None,
                    help="append raw WS frames (one per line) to this path for first-run schema validation")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
