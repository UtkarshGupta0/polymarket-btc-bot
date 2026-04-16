"""Risk manager: fractional Kelly sizing + drawdown/streak gates."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    starting_balance: float
    current_balance: float
    daily_start_balance: float
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_wins: int = 0
    daily_losses: int = 0
    consecutive_losses: int = 0
    max_drawdown_hit: bool = False
    day_utc: str = ""  # YYYY-MM-DD

    # Session lifetime stats (not reset daily)
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0


class RiskManager:
    def __init__(self, starting_balance: float | None = None) -> None:
        bal = starting_balance if starting_balance is not None else CONFIG.starting_capital
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.state = RiskState(
            starting_balance=bal,
            current_balance=bal,
            daily_start_balance=bal,
            day_utc=today,
        )

    # --- gating ---

    def _maybe_reset_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.state.day_utc:
            logger.info(
                f"Daily reset {self.state.day_utc} -> {today}. "
                f"Daily PnL was {self.state.daily_pnl:+.2f}, "
                f"trades={self.state.daily_trades} "
                f"wins={self.state.daily_wins} losses={self.state.daily_losses}"
            )
            self.state.day_utc = today
            self.state.daily_start_balance = self.state.current_balance
            self.state.daily_pnl = 0.0
            self.state.daily_trades = 0
            self.state.daily_wins = 0
            self.state.daily_losses = 0
            self.state.consecutive_losses = 0
            self.state.max_drawdown_hit = False

    def can_trade(self) -> tuple[bool, str]:
        self._maybe_reset_day()
        s = self.state
        if s.max_drawdown_hit:
            return False, "daily drawdown limit hit"
        if s.daily_pnl <= -CONFIG.max_daily_drawdown:
            s.max_drawdown_hit = True
            return False, f"daily drawdown limit hit (${s.daily_pnl:+.2f})"
        if s.consecutive_losses >= CONFIG.max_consecutive_losses:
            return False, f"consecutive loss streak ({s.consecutive_losses})"
        if s.current_balance <= CONFIG.min_reserve:
            return False, f"balance ${s.current_balance:.2f} at/below reserve"
        return True, "ok"

    # --- sizing ---

    def calculate_position_size(self, confidence: float, entry_price: float) -> float:
        """Returns USDC size for the order. 0 if should skip.

        Flat $1 for the first CONFIG.kelly_enable_after resolved trades
        (signal validation phase). Kelly 0.25 thereafter.
        """
        if entry_price <= 0 or entry_price >= 1.0:
            return 0.0

        # Flat-bet phase: fixed $1 while we validate the signal
        if self.state.total_trades < CONFIG.kelly_enable_after:
            b = (1.0 / entry_price) - 1.0
            if b <= 0 or (b * confidence - (1.0 - confidence)) < 1e-9:
                return 0.0
            available = max(0.0, self.state.current_balance - CONFIG.min_reserve)
            if available < 1.0:
                return 0.0
            return 1.0

        # Kelly phase
        b = (1.0 / entry_price) - 1.0
        if b <= 0:
            return 0.0
        p = confidence
        q = 1.0 - p
        kelly_pct = (b * p - q) / b
        if kelly_pct <= 0:
            return 0.0

        target_pct = kelly_pct * CONFIG.kelly_fraction
        available = max(0.0, self.state.current_balance - CONFIG.min_reserve)
        size = available * target_pct

        if size < CONFIG.min_bet_size:
            return 0.0
        size = min(size, CONFIG.max_bet_size)
        return round(size, 2)

    # --- trade lifecycle ---

    def on_trade_placed(self, cost_usdc: float) -> None:
        self._maybe_reset_day()
        self.state.current_balance -= cost_usdc
        self.state.daily_trades += 1
        self.state.total_trades += 1

    def on_trade_resolved(
        self, won: bool, payout_usdc: float, pnl: float, skipped: bool = False
    ) -> None:
        self._maybe_reset_day()
        self.state.current_balance += payout_usdc
        self.state.daily_pnl += pnl
        if skipped:
            # Unfilled live order — refund only, do not count as W or L
            self.state.daily_trades = max(0, self.state.daily_trades - 1)
            self.state.total_trades = max(0, self.state.total_trades - 1)
            return
        if won:
            self.state.daily_wins += 1
            self.state.total_wins += 1
            self.state.consecutive_losses = 0
        else:
            self.state.daily_losses += 1
            self.state.total_losses += 1
            self.state.consecutive_losses += 1
        if self.state.daily_pnl <= -CONFIG.max_daily_drawdown:
            self.state.max_drawdown_hit = True

    def summary(self) -> str:
        s = self.state
        wr = (s.daily_wins / s.daily_trades * 100) if s.daily_trades else 0.0
        total_wr = (s.total_wins / s.total_trades * 100) if s.total_trades else 0.0
        return (
            f"bal=${s.current_balance:.2f} "
            f"daily: pnl={s.daily_pnl:+.2f} trades={s.daily_trades} "
            f"W={s.daily_wins} L={s.daily_losses} WR={wr:.0f}% "
            f"streak={s.consecutive_losses}L | "
            f"session: trades={s.total_trades} WR={total_wr:.0f}%"
        )


# --- tests ---

def _run_tests() -> None:
    print("=" * 60)
    print("RISK MANAGER TESTS")
    print("=" * 60)

    rm = RiskManager(starting_balance=30.0)

    # Sizing: confidence high, price well below confidence (positive Kelly)
    size = rm.calculate_position_size(0.92, 0.88)
    print(f"size(conf=0.92, price=0.88) = ${size}")
    assert 0 < size <= CONFIG.max_bet_size

    # Sizing: zero when conf == price (EV = 0)
    size_neutral = rm.calculate_position_size(0.88, 0.88)
    print(f"size(conf=0.88, price=0.88) = ${size_neutral} (EV=0)")
    assert size_neutral == 0

    # Sizing: max bet cap
    rm2 = RiskManager(starting_balance=1000.0)
    rm2.state.total_trades = CONFIG.kelly_enable_after
    size2 = rm2.calculate_position_size(0.95, 0.90)
    print(f"size(conf=0.95, price=0.90, bal=$1000) = ${size2} (capped at ${CONFIG.max_bet_size})")
    assert size2 == CONFIG.max_bet_size

    # Sizing: negative Kelly (conf < price) -> 0
    size3 = rm.calculate_position_size(0.50, 0.90)
    print(f"size(conf=0.50, price=0.90) = ${size3} (negative Kelly)")
    assert size3 == 0

    # Consecutive loss pause
    rm3 = RiskManager(starting_balance=30.0)
    for i in range(CONFIG.max_consecutive_losses):
        rm3.on_trade_placed(2.0)
        rm3.on_trade_resolved(won=False, payout_usdc=0.0, pnl=-2.0)
    can, reason = rm3.can_trade()
    print(f"after {CONFIG.max_consecutive_losses} losses: can_trade={can} ({reason})")
    assert not can

    # Drawdown pause
    rm4 = RiskManager(starting_balance=30.0)
    rm4.on_trade_placed(CONFIG.max_daily_drawdown + 1)
    rm4.on_trade_resolved(won=False, payout_usdc=0.0, pnl=-(CONFIG.max_daily_drawdown + 1))
    can, reason = rm4.can_trade()
    print(f"after drawdown: can_trade={can} ({reason})")
    assert not can

    # Win resets streak
    rm5 = RiskManager(starting_balance=30.0)
    rm5.on_trade_placed(2.0); rm5.on_trade_resolved(False, 0.0, -2.0)
    rm5.on_trade_placed(2.0); rm5.on_trade_resolved(False, 0.0, -2.0)
    assert rm5.state.consecutive_losses == 2
    rm5.on_trade_placed(2.0); rm5.on_trade_resolved(True, 3.0, 1.0)
    assert rm5.state.consecutive_losses == 0
    print(f"win resets streak OK")

    # Balance at reserve
    rm6 = RiskManager(starting_balance=CONFIG.min_reserve)
    can, reason = rm6.can_trade()
    print(f"balance at reserve: can_trade={can} ({reason})")
    assert not can

    print("\nAll risk tests PASS ✓")


if __name__ == "__main__":
    _run_tests()
