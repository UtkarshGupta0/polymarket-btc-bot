"""Binance WebSocket BTC/USDT real-time price feed."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Deque, Tuple

import aiohttp
import websockets

from config import CONFIG

logger = logging.getLogger(__name__)

VWAP_WINDOW_SECONDS = 120
MOMENTUM_SAMPLE = 10
PRICE_BUFFER_MAXLEN = 500
TICK_BUFFER_MAXLEN = 10000


@dataclass
class PriceState:
    current_price: float = 0.0
    window_open_price: float = 0.0
    window_start_ts: int = 0
    vwap: float = 0.0
    momentum: float = 0.0
    trend_direction: int = 0
    delta_from_open: float = 0.0
    delta_from_open_abs: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    volume_imbalance: float = 0.0
    tick_count: int = 0
    last_update: float = 0.0


class PriceFeed:
    def __init__(self) -> None:
        self.state = PriceState()
        self._running = False
        self._prices: Deque[float] = deque(maxlen=PRICE_BUFFER_MAXLEN)
        # ticks: (timestamp_s, price, qty, is_buy)
        self._ticks: Deque[Tuple[float, float, float, bool]] = deque(
            maxlen=TICK_BUFFER_MAXLEN)
        self._task: asyncio.Task | None = None

    # --- lifecycle ---

    def start(self) -> asyncio.Task:
        self._running = True
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # --- window tracking ---

    def set_window_open(self, price: float, start_ts: int) -> None:
        self.state.window_open_price = price
        self.state.window_start_ts = start_ts
        self.state.buy_volume = 0.0
        self.state.sell_volume = 0.0
        self.state.volume_imbalance = 0.0
        self.state.tick_count = 0
        self.state.delta_from_open = 0.0
        self.state.delta_from_open_abs = 0.0
        logger.info(f"Window opened ts={start_ts} open_price=${price:.2f}")

    # --- REST fallback ---

    async def get_current_price_rest(self) -> float | None:
        url = f"{CONFIG.binance_rest_url}/api/v3/ticker/price?symbol=BTCUSDT"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    return float(data["price"])
        except Exception as e:
            logger.warning(f"REST price fallback failed: {e}")
            return None

    # --- stream ---

    async def _run(self) -> None:
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"WS disconnected: {e}. Reconnect in 2s")
                await asyncio.sleep(2)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(
            CONFIG.binance_ws_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:
            logger.info(f"Connected to Binance WS: {CONFIG.binance_ws_url}")
            async for message in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(message)
                    self._on_tick(data)
                except Exception as e:
                    logger.warning(f"Tick parse error: {e}")

    # --- tick processing ---

    def _on_tick(self, data: dict) -> None:
        try:
            price = float(data["p"])
            qty = float(data["q"])
            ts_ms = int(data["T"])
            is_buyer_maker = bool(data["m"])
        except (KeyError, ValueError, TypeError):
            return

        # is_buyer_maker=False => buyer is taker => buy-side pressure
        is_buy = not is_buyer_maker
        ts = ts_ms / 1000.0

        self._prices.append(price)
        self._ticks.append((ts, price, qty, is_buy))

        st = self.state
        st.current_price = price
        st.last_update = ts
        st.tick_count += 1

        if is_buy:
            st.buy_volume += qty
        else:
            st.sell_volume += qty
        total_vol = st.buy_volume + st.sell_volume
        st.volume_imbalance = (
            (st.buy_volume - st.sell_volume) / total_vol) if total_vol > 0 else 0.0

        if st.window_open_price > 0:
            st.delta_from_open = (price - st.window_open_price) / st.window_open_price
            st.delta_from_open_abs = abs(st.delta_from_open)

        # VWAP — rolling 120s. Recompute every tick (cheap since deque drops old)
        self._trim_old_ticks(ts)
        st.vwap = self._compute_vwap()

        # Momentum every 10 ticks
        if st.tick_count % MOMENTUM_SAMPLE == 0 and len(self._prices) >= 2 * MOMENTUM_SAMPLE:
            recent = list(self._prices)[-MOMENTUM_SAMPLE:]
            earlier = list(self._prices)[-2 * MOMENTUM_SAMPLE:-MOMENTUM_SAMPLE]
            st.momentum = mean(recent) - mean(earlier)
            st.trend_direction = 1 if st.momentum > 0 else (-1 if st.momentum < 0 else 0)

    def _trim_old_ticks(self, now_ts: float) -> None:
        cutoff = now_ts - VWAP_WINDOW_SECONDS
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.popleft()

    def _compute_vwap(self) -> float:
        num = 0.0
        den = 0.0
        for _, p, q, _ in self._ticks:
            num += p * q
            den += q
        return (num / den) if den > 0 else self.state.current_price


# --- standalone test ---

async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    feed = PriceFeed()
    feed.start()

    # Wait first tick
    deadline = time.time() + 10
    while feed.state.current_price == 0.0 and time.time() < deadline:
        await asyncio.sleep(0.1)

    if feed.state.current_price == 0.0:
        print("ERROR: No tick received in 10s. Check network.")
        rest = await feed.get_current_price_rest()
        print(f"REST fallback: {rest}")
        await feed.stop()
        return

    print(f"First tick: ${feed.state.current_price:.2f}")
    # Seed window at current price for delta demo
    feed.set_window_open(feed.state.current_price, int(time.time()))

    last_print = 0
    end = time.time() + 30
    while time.time() < end:
        await asyncio.sleep(0.5)
        st = feed.state
        if st.tick_count - last_print >= 50:
            last_print = st.tick_count
            print(
                f"ticks={st.tick_count} price=${st.current_price:.2f} "
                f"vwap=${st.vwap:.2f} momentum={st.momentum:+.4f} "
                f"delta={st.delta_from_open*100:+.4f}% "
                f"vol_imb={st.volume_imbalance:+.3f} "
                f"buy_vol={st.buy_volume:.4f} sell_vol={st.sell_volume:.4f}"
            )

    await feed.stop()
    print("Stopped.")


if __name__ == "__main__":
    asyncio.run(_main())
