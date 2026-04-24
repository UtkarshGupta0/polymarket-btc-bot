"""Counterfactually apply the 4 new gates to historical paper trades.

Reads logs/trades_2026*.json, prints three analyses to stdout.
Spec: docs/superpowers/specs/2026-04-24-gate-validation-design.md
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as _cfg_mod

CONFIG = _cfg_mod.CONFIG
MIN_CONFIDENCE = CONFIG.min_confidence
MIN_EDGE = CONFIG.min_edge
TRADING_HOURS_BLOCK = CONFIG.trading_hours_block
MIN_DELTA_PCT = CONFIG.min_delta_pct

GATES: tuple[str, ...] = ("min_confidence", "min_edge", "hours_block", "min_delta")


def load_trades(log_dir: Path) -> list[dict]:
    """Load WIN/LOSS trades from all logs/trades_2026*.json files."""
    out: list[dict] = []
    for path in sorted(log_dir.glob("trades_2026*.json")):
        data = json.loads(path.read_text())
        for t in data.get("trades", []):
            if t.get("outcome") in {"WIN", "LOSS"}:
                out.append(t)
    return out


def _trade_hour(trade: dict) -> int:
    return datetime.fromisoformat(trade["time_iso"]).hour


def _trade_edge(trade: dict) -> float:
    # Live code: target = ask - 0.01, so ask ≈ entry_price + 0.01.
    side_ask = trade["entry_price"] + 0.01
    return trade["confidence"] - side_ask


def gate_pass(trade: dict, *, active: set[str]) -> bool:
    """Return True iff trade passes every gate in `active`."""
    if "min_confidence" in active and trade["confidence"] < MIN_CONFIDENCE:
        return False
    if "min_edge" in active and _trade_edge(trade) < MIN_EDGE:
        return False
    if "hours_block" in active and _trade_hour(trade) in TRADING_HOURS_BLOCK:
        return False
    if "min_delta" in active and abs(trade.get("delta_pct", 0.0)) < MIN_DELTA_PCT:
        return False
    return True


TRAIN_DATES = {"2026-04-19", "2026-04-20", "2026-04-21"}
TEST_DATES = {"2026-04-22", "2026-04-23"}


def summarize(trades) -> dict:
    trades = list(trades)
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl_total": 0.0, "pnl_per_trade": 0.0}
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    pnl_total = sum(t.get("pnl", 0.0) for t in trades)
    return {
        "n": n,
        "wr": wins / n,
        "pnl_total": pnl_total,
        "pnl_per_trade": pnl_total / n,
    }


def _trade_date(trade: dict) -> str:
    return datetime.fromisoformat(trade["time_iso"]).date().isoformat()


def regime_split(trades) -> tuple[list[dict], list[dict]]:
    train, test = [], []
    for t in trades:
        d = _trade_date(t)
        if d in TRAIN_DATES:
            train.append(t)
        elif d in TEST_DATES:
            test.append(t)
    return train, test


def _fmt_row(label: str, s: dict) -> str:
    if s["n"] == 0:
        return f"{label:<10}   0      -         -           -"
    return (
        f"{label:<10} {s['n']:>4}   {s['wr']*100:>5.1f}%  "
        f"{s['pnl_total']:>+8.2f}   {s['pnl_per_trade']:>+7.3f}"
    )


def analysis_a(trades: list[dict]) -> None:
    kept = [t for t in trades if gate_pass(t, active=set(GATES))]
    removed = [t for t in trades if not gate_pass(t, active=set(GATES))]

    print()
    print("=== Analysis A: counterfactual stack (all 4 gates) ===")
    print(f"{'':<10} {'n':>4}   {'WR':>5}   {'PnL':>8}   {'$/trade':>7}")
    print("-" * 50)
    print(_fmt_row("BEFORE",  summarize(trades)))
    print(_fmt_row("AFTER",   summarize(kept)))
    print(_fmt_row("REMOVED", summarize(removed)))


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trades = load_trades(repo_root / "logs")
    print(f"Loaded {len(trades)} resolved trades")


if __name__ == "__main__":
    main()
