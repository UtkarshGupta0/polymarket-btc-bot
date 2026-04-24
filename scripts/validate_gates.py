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


def _slice_line(label: str, trades: list[dict]) -> str:
    kept = [t for t in trades if gate_pass(t, active=set(GATES))]
    b, a = summarize(trades), summarize(kept)
    dn = a["n"] - b["n"]
    dwr = (a["wr"] - b["wr"]) * 100
    dpnl = a["pnl_total"] - b["pnl_total"]
    return (
        f"{label:<6} "
        f"{b['n']:>4} {b['wr']*100:>5.1f}% {b['pnl_total']:>+7.2f}   "
        f"{a['n']:>4} {a['wr']*100:>5.1f}% {a['pnl_total']:>+7.2f}   "
        f"{dn:>+4} {dwr:>+5.1f}pp {dpnl:>+7.2f}"
    )


def analysis_b(trades: list[dict]) -> None:
    train, test = regime_split(trades)
    print()
    print("=== Analysis B: regime split (train Apr19-21, test Apr22-23) ===")
    print(f"{'slice':<6} {'':>21}     {'':>21}     DELTA")
    print(f"{'':<6} {'BEFORE':>21}  {'AFTER':>21}  {'n':>4} {'WR':>6} {'PnL':>7}")
    print("-" * 80)
    print(_slice_line("train", train))
    print(_slice_line("test",  test))


_COMBOS: list[tuple[str, set[str]]] = [
    ("MIN_CONFIDENCE>=0.60",    {"min_confidence"}),
    ("MIN_EDGE>=0.03",          {"min_edge"}),
    ("TRADING_HOURS_BLOCK",     {"hours_block"}),
    ("MIN_DELTA_PCT>=0.0002",   {"min_delta"}),
    ("all 4 stacked",           set(GATES)),
    ("MIN_CONFIDENCE+MIN_EDGE", {"min_confidence", "min_edge"}),
    ("MIN_CONFIDENCE+HOURS",    {"min_confidence", "hours_block"}),
    ("HOURS+MIN_DELTA",         {"hours_block", "min_delta"}),
]


def _signal_quality(removed_n: int, removed_wr: float, kept_wr: float) -> str:
    if removed_n < 5:
        return "?"
    diff_pp = (kept_wr - removed_wr) * 100
    if diff_pp > 5:
        return "✅ kills losers"
    if diff_pp < -5:
        return "❌ kills winners"
    return "⚠️ noisy"


def analysis_c(trades: list[dict]) -> None:
    print()
    print("=== Analysis C: per-gate marginal attribution ===")
    print(f"{'gate':<26} {'rem_n':>5}  {'rem_WR':>6}  {'kept_WR':>7}  {'ΔWR':>6}   signal")
    print("-" * 80)
    for label, active in _COMBOS:
        kept    = [t for t in trades if     gate_pass(t, active=active)]
        removed = [t for t in trades if not gate_pass(t, active=active)]
        sk, sr  = summarize(kept), summarize(removed)
        dwr_pp  = (sk["wr"] - sr["wr"]) * 100
        quality = _signal_quality(sr["n"], sr["wr"], sk["wr"])
        print(
            f"{label:<26} {sr['n']:>5}  "
            f"{sr['wr']*100:>5.1f}%  {sk['wr']*100:>6.1f}%  "
            f"{dwr_pp:>+5.1f}pp  {quality}"
        )


def print_caveats() -> None:
    print()
    print("=== Caveats ===")
    print("1. MIN_DELTA_PCT uses btc_close-btc_open (resolution delta), not entry-time")
    print("   delta. This over-estimates trades the live gate would let through.")
    print("2. MIN_EDGE reconstructs side_ask as entry_price + 0.01 (live does")
    print("   target = ask - 0.01). ~1-cent error from reprice-loop staleness.")
    print("3. Gates were derived from these same 166 trades. Analysis A is")
    print("   self-consistent by construction. B is the only out-of-sample check.")
    print("4. Apr 22-23 may be transient regime; n=52 is small. Re-run in 7 days.")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trades = load_trades(repo_root / "logs")
    print(f"Loaded {len(trades)} resolved trades")
    print(f"Thresholds: MIN_CONFIDENCE={MIN_CONFIDENCE} MIN_EDGE={MIN_EDGE} "
          f"HOURS_BLOCK={sorted(TRADING_HOURS_BLOCK) or 'off'} "
          f"MIN_DELTA_PCT={MIN_DELTA_PCT}")
    analysis_a(trades)
    analysis_b(trades)
    analysis_c(trades)
    print_caveats()


if __name__ == "__main__":
    main()
