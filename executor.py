"""Executor. PaperExecutor first. LiveExecutor is a stub for Phase 5."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config import CONFIG

logger = logging.getLogger(__name__)

MIN_SHARE_SIZE = 5.0  # Polymarket CLOB minimum


@dataclass
class Trade:
    window_ts: int
    placed_at: float              # unix ts
    time_iso: str                 # ISO-8601 UTC
    direction: str                # "UP" or "DOWN"
    confidence: float
    entry_price: float            # 0.01 - 0.95
    size_usdc: float              # money at risk
    shares: float                 # size_usdc / entry_price
    token_id: str
    seconds_to_close_at_entry: float
    btc_open: float
    mode: str                     # "paper" or "live"

    # Resolved
    status: str = "PLACED"        # PLACED | FILLED | WIN | LOSS | REJECTED | CANCELLED
    btc_close: Optional[float] = None
    outcome: Optional[str] = None # "WIN" | "LOSS"
    pnl: Optional[float] = None
    payout: Optional[float] = None
    delta_pct: Optional[float] = None
    balance_after: Optional[float] = None
    order_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class PaperExecutor:
    """Simulated trading. No real money. Assumes immediate fill at our limit."""

    def __init__(self) -> None:
        self._traded_windows: set[int] = set()
        self._active_trades: dict[int, Trade] = {}

    def has_traded(self, window_ts: int) -> bool:
        return window_ts in self._traded_windows

    def get_trade(self, window_ts: int) -> Optional[Trade]:
        return self._active_trades.get(window_ts)

    def place_order(
        self,
        window_ts: int,
        direction: str,
        confidence: float,
        entry_price: float,
        size_usdc: float,
        token_id: str,
        btc_open: float,
        seconds_to_close: float,
    ) -> Optional[Trade]:
        if window_ts in self._traded_windows:
            logger.warning(f"[paper] already traded window {window_ts}, skipping")
            return None

        entry_price = round(entry_price, 2)
        if entry_price <= 0 or entry_price >= 1.0:
            logger.warning(f"[paper] invalid entry_price {entry_price}")
            return None

        shares = round(size_usdc / entry_price, 2)
        if shares < MIN_SHARE_SIZE:
            logger.info(
                f"[paper] shares {shares:.2f} < min {MIN_SHARE_SIZE}, "
                f"(size=${size_usdc:.2f} price=${entry_price}). Skipping."
            )
            return None

        now = time.time()
        trade = Trade(
            window_ts=window_ts,
            placed_at=now,
            time_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            direction=direction,
            confidence=confidence,
            entry_price=entry_price,
            size_usdc=round(size_usdc, 2),
            shares=shares,
            token_id=token_id,
            seconds_to_close_at_entry=round(seconds_to_close, 1),
            btc_open=btc_open,
            mode="paper",
            status="FILLED",  # paper assumes immediate fill
        )
        self._traded_windows.add(window_ts)
        self._active_trades[window_ts] = trade
        logger.info(
            f"[paper] PLACED window={window_ts} {direction} "
            f"@ ${entry_price} size=${trade.size_usdc} shares={shares} "
            f"conf={confidence:.2f} T-{seconds_to_close:.0f}s"
        )
        return trade

    def pending_trade(self, window_ts: int) -> Optional[Trade]:
        return self._active_trades.get(window_ts)

    def reprice(self, window_ts: int, new_price: float) -> Optional[Trade]:
        old = self._active_trades.get(window_ts)
        if old is None:
            return None
        self._traded_windows.discard(window_ts)
        del self._active_trades[window_ts]
        return self.place_order(
            window_ts=window_ts,
            direction=old.direction,
            confidence=old.confidence,
            entry_price=new_price,
            size_usdc=old.size_usdc,
            token_id=old.token_id,
            btc_open=old.btc_open,
            seconds_to_close=old.seconds_to_close_at_entry,
        )

    def resolve_trade(self, window_ts: int, btc_close: float) -> Optional[Trade]:
        trade = self._active_trades.get(window_ts)
        if trade is None:
            return None
        if trade.status not in ("FILLED", "PLACED"):
            return trade

        went_up = btc_close >= trade.btc_open
        won = (trade.direction == "UP" and went_up) or \
              (trade.direction == "DOWN" and not went_up)

        if won:
            payout = trade.shares * 1.0  # each share pays $1
            pnl = payout - trade.size_usdc
            trade.status = "WIN"
            trade.outcome = "WIN"
        else:
            payout = 0.0
            pnl = -trade.size_usdc
            trade.status = "LOSS"
            trade.outcome = "LOSS"

        trade.btc_close = btc_close
        trade.payout = round(payout, 4)
        trade.pnl = round(pnl, 4)
        trade.delta_pct = ((btc_close - trade.btc_open) / trade.btc_open
                           if trade.btc_open else 0.0)
        logger.info(
            f"[paper] RESOLVED window={window_ts} "
            f"open=${trade.btc_open:.2f} close=${btc_close:.2f} "
            f"delta={trade.delta_pct*100:+.4f}% -> {trade.outcome} pnl=${pnl:+.2f}"
        )
        return trade

    def forget_window(self, window_ts: int) -> None:
        """Remove trade from active map (after logging). Keep dedup set."""
        self._active_trades.pop(window_ts, None)


class LiveExecutor:
    """Real orders via py-clob-client. MAKER ONLY (OrderType.GTC)."""

    def __init__(self) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            OrderArgs, OrderType, BalanceAllowanceParams, AssetType,
        )
        from py_clob_client.order_builder.constants import BUY

        self._OrderArgs = OrderArgs
        self._OrderType = OrderType
        self._BUY = BUY
        self._BalanceAllowanceParams = BalanceAllowanceParams
        self._AssetType = AssetType

        if not CONFIG.polymarket_private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY missing — required for live mode")

        sig_type = CONFIG.polymarket_signature_type
        kwargs = dict(
            host=CONFIG.clob_api_url,
            key=CONFIG.polymarket_private_key,
            chain_id=137,
        )
        if sig_type in (1, 2):
            if not CONFIG.polymarket_funder_address:
                raise RuntimeError(
                    f"POLYMARKET_FUNDER_ADDRESS required for signature_type={sig_type} "
                    "(1=email/Magic proxy, 2=MetaMask Gnosis-Safe proxy)"
                )
            kwargs["signature_type"] = sig_type
            kwargs["funder"] = CONFIG.polymarket_funder_address
        # sig_type=0 = EOA direct, no funder

        self.client = ClobClient(**kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        logger.info(f"LiveExecutor ready (sig_type={sig_type})")

        self._traded_windows: set[int] = set()
        self._active_trades: dict[int, Trade] = {}

    # --- dedup ---

    def has_traded(self, window_ts: int) -> bool:
        return window_ts in self._traded_windows

    def get_trade(self, window_ts: int) -> Optional[Trade]:
        return self._active_trades.get(window_ts)

    # --- balance ---

    def get_usdc_balance(self) -> Optional[float]:
        try:
            params = self._BalanceAllowanceParams(
                asset_type=self._AssetType.COLLATERAL)
            resp = self.client.get_balance_allowance(params)
            # balance in wei (6 decimals for USDC)
            bal = resp.get("balance") if isinstance(resp, dict) else None
            if bal is None:
                return None
            return int(bal) / 1_000_000
        except Exception as e:
            logger.warning(f"get_balance_allowance error: {e}")
            return None

    # --- order placement ---

    def place_order(
        self,
        window_ts: int,
        direction: str,
        confidence: float,
        entry_price: float,
        size_usdc: float,
        token_id: str,
        btc_open: float,
        seconds_to_close: float,
    ) -> Optional[Trade]:
        if window_ts in self._traded_windows:
            logger.warning(f"[live] already traded window {window_ts}, skipping")
            return None

        entry_price = round(entry_price, 2)
        if entry_price <= 0 or entry_price >= 1.0:
            logger.warning(f"[live] invalid entry_price {entry_price}")
            return None

        shares = round(size_usdc / entry_price, 2)
        if shares < MIN_SHARE_SIZE:
            logger.info(
                f"[live] shares {shares:.2f} < min {MIN_SHARE_SIZE}; skip"
            )
            return None

        try:
            order_args = self._OrderArgs(
                price=entry_price,
                size=shares,
                side=self._BUY,
                token_id=token_id,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, self._OrderType.GTC)
        except Exception as e:
            logger.error(f"[live] order placement failed: {e}")
            return None

        order_id = None
        status = "REJECTED"
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id")
            success = resp.get("success", True)
            status = resp.get("status") or ("LIVE" if success else "REJECTED")

        if order_id is None or status == "REJECTED":
            logger.error(f"[live] order response suspicious: {resp}")
            return None

        now = time.time()
        trade = Trade(
            window_ts=window_ts,
            placed_at=now,
            time_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            direction=direction,
            confidence=confidence,
            entry_price=entry_price,
            size_usdc=round(size_usdc, 2),
            shares=shares,
            token_id=token_id,
            seconds_to_close_at_entry=round(seconds_to_close, 1),
            btc_open=btc_open,
            mode="live",
            status=status,
            order_id=order_id,
        )
        self._traded_windows.add(window_ts)
        self._active_trades[window_ts] = trade
        logger.info(
            f"[live] PLACED window={window_ts} {direction} @ ${entry_price} "
            f"shares={shares} order_id={order_id} status={status}"
        )
        return trade

    # --- order status ---

    def check_order(self, order_id: str) -> Optional[dict]:
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            logger.warning(f"[live] get_order {order_id} error: {e}")
            return None

    def refresh_trade_status(self, window_ts: int) -> Optional[Trade]:
        trade = self._active_trades.get(window_ts)
        if trade is None or trade.order_id is None:
            return trade
        info = self.check_order(trade.order_id)
        if info is None:
            return trade
        new_status = info.get("status") if isinstance(info, dict) else None
        if new_status:
            trade.status = new_status
        return trade

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            logger.warning(f"[live] cancel {order_id} error: {e}")
            return False

    def cancel_pending_if_unfilled(self, window_ts: int) -> None:
        trade = self._active_trades.get(window_ts)
        if trade is None or trade.order_id is None:
            return
        self.refresh_trade_status(window_ts)
        if trade.status in ("LIVE", "PLACED"):
            if self.cancel_order(trade.order_id):
                trade.status = "CANCELLED"
                logger.info(f"[live] cancelled unfilled order {trade.order_id}")

    def pending_trade(self, window_ts: int) -> Optional[Trade]:
        return self._active_trades.get(window_ts)

    def reprice(self, window_ts: int, new_price: float) -> Optional[Trade]:
        old = self._active_trades.get(window_ts)
        if old is None:
            return None
        if old.order_id:
            ok = self.cancel_order(old.order_id)
            if not ok:
                logger.warning(f"reprice: cancel failed for {old.order_id}, skipping replace")
                return None
        self._traded_windows.discard(window_ts)
        del self._active_trades[window_ts]
        return self.place_order(
            window_ts=window_ts,
            direction=old.direction,
            confidence=old.confidence,
            entry_price=new_price,
            size_usdc=old.size_usdc,
            token_id=old.token_id,
            btc_open=old.btc_open,
            seconds_to_close=old.seconds_to_close_at_entry,
        )

    def cancel_all(self) -> None:
        try:
            self.client.cancel_all()
            logger.info("[live] cancel_all done")
        except Exception as e:
            logger.warning(f"[live] cancel_all error: {e}")

    # --- resolution ---

    def resolve_trade(self, window_ts: int, btc_close: float) -> Optional[Trade]:
        trade = self._active_trades.get(window_ts)
        if trade is None:
            return None

        # Refresh status one last time
        self.refresh_trade_status(window_ts)

        filled = trade.status in ("MATCHED", "FILLED")
        if not filled:
            # Cancel lingering order
            if trade.order_id and trade.status in ("LIVE", "PLACED"):
                self.cancel_order(trade.order_id)
            trade.status = "UNFILLED"
            trade.outcome = "SKIPPED"
            trade.btc_close = btc_close
            trade.pnl = 0.0
            # Refund reserved size so risk_manager balance stays honest
            trade.payout = round(trade.size_usdc, 4)
            trade.delta_pct = ((btc_close - trade.btc_open) / trade.btc_open
                               if trade.btc_open else 0.0)
            logger.info(f"[live] window={window_ts} not filled; refund ${trade.size_usdc}")
            return trade

        went_up = btc_close >= trade.btc_open
        won = (trade.direction == "UP" and went_up) or \
              (trade.direction == "DOWN" and not went_up)

        if won:
            payout = trade.shares * 1.0
            pnl = payout - trade.size_usdc
            trade.status = "WIN"
            trade.outcome = "WIN"
        else:
            payout = 0.0
            pnl = -trade.size_usdc
            trade.status = "LOSS"
            trade.outcome = "LOSS"

        trade.btc_close = btc_close
        trade.payout = round(payout, 4)
        trade.pnl = round(pnl, 4)
        trade.delta_pct = ((btc_close - trade.btc_open) / trade.btc_open
                           if trade.btc_open else 0.0)
        logger.info(
            f"[live] RESOLVED window={window_ts} "
            f"open=${trade.btc_open:.2f} close=${btc_close:.2f} -> "
            f"{trade.outcome} pnl=${pnl:+.2f}"
        )
        return trade

    def forget_window(self, window_ts: int) -> None:
        self._active_trades.pop(window_ts, None)


def build_executor():
    if CONFIG.trading_mode == "live":
        return LiveExecutor()
    return PaperExecutor()


# --- tests ---

def _run_tests() -> None:
    print("=" * 60)
    print("EXECUTOR TESTS")
    print("=" * 60)

    ex = PaperExecutor()
    t1 = ex.place_order(
        window_ts=1000, direction="UP", confidence=0.85,
        entry_price=0.92, size_usdc=5.0, token_id="tok_up",
        btc_open=80000.0, seconds_to_close=20.0,
    )
    assert t1 is not None and t1.status == "FILLED"
    print(f"trade 1 placed: {t1.direction} shares={t1.shares}")

    # Dedup: same window must reject
    t_dup = ex.place_order(
        window_ts=1000, direction="DOWN", confidence=0.90,
        entry_price=0.92, size_usdc=5.0, token_id="tok_down",
        btc_open=80000.0, seconds_to_close=10.0,
    )
    assert t_dup is None, "dedup must reject duplicate window"
    print("dedup OK ✓")

    # Resolve WIN — BTC up, bet was UP
    r = ex.resolve_trade(1000, btc_close=80050.0)
    assert r.outcome == "WIN" and r.pnl > 0
    print(f"WIN resolve: pnl=${r.pnl:+.2f} payout=${r.payout}")

    # New window LOSS
    ex.place_order(
        window_ts=2000, direction="UP", confidence=0.80,
        entry_price=0.90, size_usdc=4.5, token_id="tok_up",
        btc_open=80000.0, seconds_to_close=15.0,
    )
    r2 = ex.resolve_trade(2000, btc_close=79950.0)
    assert r2.outcome == "LOSS" and r2.pnl == -4.5
    print(f"LOSS resolve: pnl=${r2.pnl:+.2f}")

    # DOWN win
    ex.place_order(
        window_ts=3000, direction="DOWN", confidence=0.80,
        entry_price=0.90, size_usdc=4.5, token_id="tok_down",
        btc_open=80000.0, seconds_to_close=12.0,
    )
    r3 = ex.resolve_trade(3000, btc_close=79900.0)
    assert r3.outcome == "WIN"
    print(f"DOWN WIN resolve: pnl=${r3.pnl:+.2f}")

    # Shares below minimum
    t_small = ex.place_order(
        window_ts=4000, direction="UP", confidence=0.9,
        entry_price=0.90, size_usdc=1.0, token_id="tok_up",  # 1/0.9 ≈ 1.11 shares
        btc_open=80000.0, seconds_to_close=10.0,
    )
    assert t_small is None, "should reject shares < MIN_SHARE_SIZE"
    print(f"MIN_SHARE_SIZE gate OK ✓")

    print("\nAll executor tests PASS ✓")


if __name__ == "__main__":
    _run_tests()
