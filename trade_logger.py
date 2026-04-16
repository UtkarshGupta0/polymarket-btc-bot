"""JSON trade logger + daily summary."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import CONFIG
from executor import Trade

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent / "logs"


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _trade_file(day: str | None = None) -> Path:
    d = day or _today_utc()
    return LOG_DIR / f"trades_{d}.json"


def _summary_file(day: str | None = None) -> Path:
    d = day or _today_utc()
    return LOG_DIR / f"daily_summary_{d}.json"


class TradeLogger:
    """Append trades to logs/trades_YYYYMMDD.json (read-modify-write). Safe for
    the low trade volume of this bot (~288 trades/day max)."""

    def __init__(self, session_start_balance: float | None = None) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.session = {
            "mode": CONFIG.trading_mode,
            "start_time": int(time.time()),
            "starting_balance": (
                session_start_balance if session_start_balance is not None
                else CONFIG.starting_capital
            ),
        }
        logger.info(f"Trade log dir: {LOG_DIR}")

    def _load_day(self, day: str | None = None) -> dict:
        path = _trade_file(day)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Corrupt log {path}: {e}. Re-initializing.")
        return {"session": self.session, "trades": []}

    def _write_day(self, data: dict, day: str | None = None) -> None:
        path = _trade_file(day)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, path)  # atomic on POSIX

    def log_trade(self, trade: Trade) -> None:
        try:
            data = self._load_day()
            data["trades"].append(trade.to_dict())
            self._write_day(data)
            logger.info(
                f"logged {trade.direction} window={trade.window_ts} "
                f"outcome={trade.outcome} pnl={trade.pnl}"
            )
        except OSError as e:
            logger.error(f"log_trade OSError: {e}. Trade not persisted.")

    def write_daily_summary(self, risk_state, day: str | None = None) -> Path:
        d = day or _today_utc()
        data = self._load_day(d)
        trades = data.get("trades", [])
        resolved = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
        wins = sum(1 for t in resolved if t["outcome"] == "WIN")
        losses = sum(1 for t in resolved if t["outcome"] == "LOSS")
        pnl = sum((t.get("pnl") or 0) for t in resolved)
        best = max((t.get("pnl") or 0) for t in resolved) if resolved else 0.0
        worst = min((t.get("pnl") or 0) for t in resolved) if resolved else 0.0

        summary = {
            "day_utc": d,
            "mode": CONFIG.trading_mode,
            "trades_total": len(trades),
            "trades_resolved": len(resolved),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(resolved)) if resolved else 0.0,
            "pnl": round(pnl, 4),
            "best_trade": round(best, 4),
            "worst_trade": round(worst, 4),
            "balance_end": risk_state.current_balance,
            "balance_start": risk_state.daily_start_balance,
            "consecutive_losses_end": risk_state.consecutive_losses,
        }
        path = _summary_file(d)
        path.write_text(json.dumps(summary, indent=2, default=str))
        logger.info(f"daily summary: {summary}")
        return path


# --- standalone test ---

def _run_tests() -> None:
    from executor import Trade
    print("=" * 60)
    print("TRADE LOGGER TESTS")
    print("=" * 60)

    tl = TradeLogger(session_start_balance=30.0)
    t = Trade(
        window_ts=9999999999,
        placed_at=time.time(),
        time_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        direction="UP", confidence=0.8, entry_price=0.92,
        size_usdc=3.5, shares=3.8, token_id="tok",
        seconds_to_close_at_entry=18.5, btc_open=84200.0,
        mode="paper", status="WIN", btc_close=84250.3,
        outcome="WIN", pnl=0.28, payout=3.78, delta_pct=0.000597,
        balance_after=30.28,
    )
    tl.log_trade(t)
    path = _trade_file()
    data = json.loads(path.read_text())
    assert any(x["window_ts"] == 9999999999 for x in data["trades"])
    print(f"logged + read back OK: {path}")

    # Cleanup test entry so we don't pollute real logs
    data["trades"] = [x for x in data["trades"] if x["window_ts"] != 9999999999]
    path.write_text(json.dumps(data, indent=2, default=str))
    print("cleaned up test trade")
    print("\nAll trade_logger tests PASS ✓")


if __name__ == "__main__":
    _run_tests()
