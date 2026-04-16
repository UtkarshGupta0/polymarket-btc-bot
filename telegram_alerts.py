"""Telegram alerts via direct Bot API. No extra deps (uses aiohttp)."""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from config import CONFIG

logger = logging.getLogger(__name__)


class TelegramAlerts:
    def __init__(self) -> None:
        self.token = CONFIG.telegram_bot_token
        self.chat_id = CONFIG.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        if self.enabled:
            logger.info("Telegram alerts: ENABLED")
        else:
            logger.info("Telegram alerts: disabled (creds not set)")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            sess = await self._get_session()
            async with sess.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"telegram HTTP {r.status}: {body[:200]}")
        except Exception as e:
            logger.warning(f"telegram send error: {e}")

    # --- canned messages ---

    async def trade_placed(self, trade) -> None:
        msg = (
            f"🎯 BTC 5m | {trade.direction} @ ${trade.entry_price:.2f} | "
            f"${trade.size_usdc:.2f} | conf {trade.confidence*100:.0f}% | "
            f"T-{trade.seconds_to_close_at_entry:.0f}s"
        )
        await self.send(msg)

    async def trade_resolved(self, trade, risk_state) -> None:
        wr = (
            (risk_state.daily_wins / risk_state.daily_trades * 100)
            if risk_state.daily_trades else 0.0
        )
        if trade.outcome == "WIN":
            head = f"✅ WON +${trade.pnl:.2f}"
        else:
            head = f"❌ LOST {trade.pnl:+.2f}"
        msg = (
            f"{head} | bal ${risk_state.current_balance:.2f} | "
            f"WR {wr:.0f}% ({risk_state.daily_wins}/{risk_state.daily_trades}) | "
            f"streak {risk_state.consecutive_losses}L"
        )
        await self.send(msg)

    async def daily_summary(self, summary: dict) -> None:
        wr_pct = summary.get("win_rate", 0.0) * 100
        msg = (
            f"📊 Daily {summary.get('day_utc', '?')}\n"
            f"Trades: {summary.get('trades_resolved', 0)} | "
            f"W: {summary.get('wins', 0)} | WR: {wr_pct:.0f}%\n"
            f"PnL: {summary.get('pnl', 0):+.2f} | "
            f"Bal: ${summary.get('balance_end', 0):.2f}\n"
            f"Best: {summary.get('best_trade', 0):+.2f} | "
            f"Worst: {summary.get('worst_trade', 0):+.2f}"
        )
        await self.send(msg)

    async def risk_paused(self, reason: str) -> None:
        await self.send(f"⚠️ Bot paused: {reason}")

    async def startup(self, mode: str, capital: float) -> None:
        await self.send(f"🤖 Bot started — mode={mode.upper()} capital=${capital:.2f}")

    async def shutdown(self, summary_text: str) -> None:
        await self.send(f"🛑 Bot stopped\n{summary_text}")


# --- standalone test ---

async def _main() -> None:
    import asyncio
    logging.basicConfig(level=logging.INFO)
    t = TelegramAlerts()
    if not t.enabled:
        print("Telegram disabled (no creds). Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
        return
    await t.send("test message from polymarket-btc-bot")
    await t.close()
    print("sent")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
