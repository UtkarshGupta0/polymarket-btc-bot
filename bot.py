"""Main orchestrator loop. Paper mode Phase 3."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Optional

from config import CONFIG
from price_feed import PriceFeed
from market_finder import MarketFinder, MarketWindow, get_current_window_start, INTERVAL_5M
from signal_engine import compute_signal, should_trade
from risk_manager import RiskManager
from executor import PaperExecutor, Trade, build_executor
from trade_logger import TradeLogger
from telegram_alerts import TelegramAlerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

RESOLVE_DELAY_SECONDS = 2.0


class Bot:
    def __init__(self) -> None:
        self.price_feed = PriceFeed()
        self.market_finder = MarketFinder()
        self.risk_manager = RiskManager(CONFIG.starting_capital)
        self.executor = build_executor()  # PaperExecutor (or LiveExecutor Phase 5)
        self.trade_logger = TradeLogger(CONFIG.starting_capital)
        self.telegram = TelegramAlerts()
        self._risk_pause_alerted = False

        self.last_window_ts: int = 0
        self.current_window: Optional[MarketWindow] = None
        self.pending_trade: Optional[Trade] = None
        self._running = False
        self._orderbook_fetched_at: float = 0.0
        self._last_day_utc = self.risk_manager.state.day_utc

    # --- lifecycle ---

    async def run(self) -> None:
        self._install_signal_handlers()
        self._running = True
        print("=" * 70)
        print(f"POLYMARKET BTC 5-MIN BOT — mode={CONFIG.trading_mode.upper()}")
        print("=" * 70)
        CONFIG.print_summary()

        self.price_feed.start()
        await self._wait_first_tick(timeout=10.0)

        await self.market_finder.__aenter__()
        await self.telegram.startup(CONFIG.trading_mode, CONFIG.starting_capital)

        try:
            await self._trading_loop()
        finally:
            await self._shutdown()

    async def _wait_first_tick(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while self.price_feed.state.current_price == 0.0 and time.time() < deadline:
            await asyncio.sleep(0.1)
        if self.price_feed.state.current_price == 0.0:
            rest = await self.price_feed.get_current_price_rest()
            if rest is not None:
                self.price_feed.state.current_price = rest
                logger.warning(f"Using REST fallback price ${rest:.2f}")
            else:
                raise RuntimeError("No Binance tick and REST fallback failed. Network issue.")
        logger.info(f"First price: ${self.price_feed.state.current_price:.2f}")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, self._signal_stop)
            except NotImplementedError:
                pass  # Windows

    def _signal_stop(self) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    async def _shutdown(self) -> None:
        logger.info("Shutting down...")
        # Live mode: cancel any open orders first
        if CONFIG.trading_mode == "live" and hasattr(self.executor, "cancel_all"):
            try:
                self.executor.cancel_all()
            except Exception as e:
                logger.warning(f"cancel_all on shutdown: {e}")
        try:
            await self.market_finder.close()
        except Exception as e:
            logger.warning(f"market_finder close: {e}")
        try:
            await self.price_feed.stop()
        except Exception as e:
            logger.warning(f"price_feed stop: {e}")

        # Write daily summary
        summary_path = None
        try:
            summary_path = self.trade_logger.write_daily_summary(self.risk_manager.state)
        except Exception as e:
            logger.warning(f"daily summary write: {e}")

        summary_text = self.risk_manager.summary()
        try:
            await self.telegram.shutdown(summary_text)
        except Exception as e:
            logger.warning(f"telegram shutdown alert: {e}")
        try:
            await self.telegram.close()
        except Exception as e:
            logger.warning(f"telegram close: {e}")

        print("=" * 70)
        print("FINAL SESSION REPORT")
        print("=" * 70)
        print(summary_text)
        if summary_path:
            print(f"daily summary: {summary_path}")
        print("=" * 70)

    # --- core loop ---

    async def _trading_loop(self) -> None:
        logger.info("Entering trading loop")
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"loop error: {e}")
                await asyncio.sleep(1)

    async def _tick(self) -> None:
        now = time.time()
        current_start = get_current_window_start(now)
        window_end = current_start + INTERVAL_5M
        seconds_remaining = window_end - now

        # 0. DAILY ROLLOVER — write summary for ending day before risk_manager resets
        await self._maybe_rollover_day()

        # 1. NEW WINDOW DETECTED
        if current_start != self.last_window_ts:
            # Resolve previous window if any
            if self.pending_trade is not None and self.last_window_ts:
                await self._resolve_previous_trade()

            # New window setup
            await self._on_new_window(current_start)

        # 2. ENTRY WINDOW — try to place a trade
        in_entry_window = (
            CONFIG.entry_window_end < seconds_remaining <= CONFIG.entry_window_start
        )
        if in_entry_window and self.pending_trade is None \
                and not self.executor.has_traded(current_start):
            await self._maybe_place_trade(current_start, window_end, seconds_remaining)

        # 2b. Live mode — periodic order status refresh + late-window cancel
        if self.pending_trade is not None and CONFIG.trading_mode == "live":
            if hasattr(self.executor, "refresh_trade_status"):
                self.executor.refresh_trade_status(self.pending_trade.window_ts)
            if seconds_remaining <= 3 and self.pending_trade.status in ("LIVE", "PLACED"):
                if hasattr(self.executor, "cancel_pending_if_unfilled"):
                    self.executor.cancel_pending_if_unfilled(self.pending_trade.window_ts)

        # 3. WINDOW CLOSE — resolution handled at top of next tick when window_ts changes.
        # But if we've got a pending trade and we've crossed close, resolve here too.
        if seconds_remaining <= 0 and self.pending_trade is not None \
                and self.pending_trade.window_ts == current_start - INTERVAL_5M:
            await self._resolve_previous_trade()

        # 4. PACE
        if seconds_remaining <= 50 and in_entry_window:
            await asyncio.sleep(0.5)
        elif seconds_remaining <= 50:
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(2.0)

    async def _maybe_rollover_day(self) -> None:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self._last_day_utc:
            return
        prev_day_utc = self._last_day_utc
        prev_day_file = prev_day_utc.replace("-", "")
        try:
            path = self.trade_logger.write_daily_summary(
                self.risk_manager.state, day=prev_day_file
            )
            import json as _json
            try:
                summary = _json.loads(path.read_text())
                await self.telegram.daily_summary(summary)
            except Exception as e:
                logger.warning(f"daily telegram: {e}")
        except Exception as e:
            logger.warning(f"daily rollover: {e}")
        self._last_day_utc = today
        self._risk_pause_alerted = False

    async def _on_new_window(self, window_start: int) -> None:
        open_price = self.price_feed.state.current_price
        if open_price <= 0:
            # No tick yet; wait briefly
            rest = await self.price_feed.get_current_price_rest()
            if rest:
                open_price = rest
                self.price_feed.state.current_price = rest
        self.price_feed.set_window_open(open_price, window_start)
        self.last_window_ts = window_start
        self.pending_trade = None
        self._orderbook_fetched_at = 0.0

        # Kick off market data fetch in background (don't block loop)
        asyncio.create_task(self._fetch_market_async(window_start))

    async def _fetch_market_async(self, window_start: int) -> None:
        try:
            mw = await self.market_finder.find_market(window_start, refresh=True)
            if mw.up_token_id and mw.down_token_id:
                await self.market_finder.refresh_prices(mw)
            self.current_window = mw
            logger.info(
                f"market {window_start}: up_tok={'set' if mw.up_token_id else 'missing'} "
                f"down_tok={'set' if mw.down_token_id else 'missing'} "
                f"up_ask={mw.up_best_ask} down_ask={mw.down_best_ask}"
            )
        except Exception as e:
            logger.warning(f"market fetch error for ts={window_start}: {e}")

    async def _refresh_orderbook_if_stale(self) -> None:
        if self.current_window is None:
            return
        now = time.time()
        if now - self._orderbook_fetched_at < 3.0:
            return
        try:
            await self.market_finder.refresh_prices(self.current_window)
            self._orderbook_fetched_at = now
        except Exception as e:
            logger.warning(f"orderbook refresh: {e}")

    async def _maybe_place_trade(
        self, window_start: int, window_end: int, seconds_remaining: float
    ) -> None:
        signal = compute_signal(self.price_feed.state, window_end)
        if signal is None:
            return
        # Log signal once per second in entry window
        now = time.time()
        if not hasattr(self, "_last_signal_log") or now - self._last_signal_log >= 5.0:
            self._last_signal_log = now
            logger.info(f"signal | {signal.rationale} EV={signal.expected_value:+.4f}")
        if not should_trade(signal):
            return

        can, reason = self.risk_manager.can_trade()
        if not can:
            logger.info(f"risk gate: {reason}")
            if not self._risk_pause_alerted and (
                self.risk_manager.state.max_drawdown_hit
                or self.risk_manager.state.consecutive_losses
                   >= CONFIG.max_consecutive_losses
            ):
                self._risk_pause_alerted = True
                asyncio.create_task(self.telegram.risk_paused(reason))
            return

        size = self.risk_manager.calculate_position_size(
            signal.confidence, signal.suggested_price
        )
        if size <= 0:
            return

        # Need token IDs
        if self.current_window is None or \
                not self.current_window.up_token_id or \
                not self.current_window.down_token_id:
            logger.info(f"market tokens not ready for {window_start}; skip")
            return

        # Refresh orderbook to avoid becoming a taker (price >= best ask)
        await self._refresh_orderbook_if_stale()
        mw = self.current_window
        best_ask = mw.up_best_ask if signal.direction == "UP" else mw.down_best_ask
        entry_price = signal.suggested_price
        if best_ask is not None and entry_price >= best_ask:
            adjusted = round(best_ask - 0.01, 2)
            logger.info(
                f"price ${entry_price} >= best_ask ${best_ask}; adjusting to ${adjusted} "
                "to stay maker"
            )
            entry_price = adjusted
            if entry_price < 0.5:
                logger.info("adjusted price too low, skipping")
                return

        token_id = mw.up_token_id if signal.direction == "UP" else mw.down_token_id
        trade = self.executor.place_order(
            window_ts=window_start,
            direction=signal.direction,
            confidence=signal.confidence,
            entry_price=entry_price,
            size_usdc=size,
            token_id=token_id,
            btc_open=self.price_feed.state.window_open_price,
            seconds_to_close=seconds_remaining,
        )
        if trade is None:
            return
        self.pending_trade = trade
        self.risk_manager.on_trade_placed(trade.size_usdc)
        logger.info(f"TRADE PLACED | {signal.rationale}")
        asyncio.create_task(self.telegram.trade_placed(trade))

    async def _resolve_previous_trade(self) -> None:
        trade = self.pending_trade
        if trade is None:
            return
        # Wait a beat for final price to settle
        await asyncio.sleep(RESOLVE_DELAY_SECONDS)
        btc_close = self.price_feed.state.current_price
        resolved = self.executor.resolve_trade(trade.window_ts, btc_close)
        if resolved is None:
            self.pending_trade = None
            return

        won = resolved.outcome == "WIN"
        skipped = resolved.outcome == "SKIPPED"
        self.risk_manager.on_trade_resolved(
            won=won,
            payout_usdc=resolved.payout or 0.0,
            pnl=resolved.pnl or 0.0,
            skipped=skipped,
        )
        resolved.balance_after = round(self.risk_manager.state.current_balance, 4)
        self.trade_logger.log_trade(resolved)
        logger.info(f"POST-RESOLVE | {self.risk_manager.summary()}")
        asyncio.create_task(
            self.telegram.trade_resolved(resolved, self.risk_manager.state)
        )
        # Reset pause alert flag on any win (fresh day for alerts)
        if won:
            self._risk_pause_alerted = False
        self.executor.forget_window(resolved.window_ts)
        self.pending_trade = None


def main() -> None:
    try:
        asyncio.run(Bot().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
