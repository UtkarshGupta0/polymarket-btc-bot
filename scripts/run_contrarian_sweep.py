"""Run the contrarian variant across a range of CONTRARIAN_ASK_THRESHOLD
values and aggregate results into a single summary JSON.

Usage:
    python scripts/run_contrarian_sweep.py \
        --tape data/btc_5m_tape.parquet \
        --out logs/contrarian_sweep.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import backtester_v2 as bt2  # noqa: E402
from config import CONFIG  # noqa: E402


THRESHOLDS = [0.85, 0.88, 0.90, 0.92, 0.94]


def _set_cfg(**kwargs) -> dict:
    prev = {k: getattr(CONFIG, k) for k in kwargs}
    for k, v in kwargs.items():
        object.__setattr__(CONFIG, k, v)
    CONFIG.validate()
    return prev


def _restore_cfg(prev: dict) -> None:
    for k, v in prev.items():
        object.__setattr__(CONFIG, k, v)


def run_one(tape_path: str, threshold: float, fee_rate: float) -> dict:
    prev = _set_cfg(
        signal_variant="contrarian",
        contrarian_ask_threshold=threshold,
    )
    try:
        tape = bt2.load_tape(tape_path)
        trades, _ticks = bt2.replay(
            tape, starting_balance=CONFIG.starting_capital,
            fee_rate=fee_rate, fillability_required=False,
        )
    finally:
        _restore_cfg(prev)

    settled = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    n = len(settled)
    wins = sum(1 for t in settled if t.outcome == "WIN")
    pnl_total = sum(float(t.pnl) for t in settled)
    market_implied_underdog_p = round(1.0 - threshold, 4)
    win_rate = round(wins / n, 4) if n else 0.0
    return {
        "threshold": threshold,
        "n_trades": n,
        "win_rate": win_rate,
        "pnl_total": round(pnl_total, 4),
        "pnl_per_trade": round(pnl_total / n, 4) if n else 0.0,
        "market_implied_underdog_p": market_implied_underdog_p,
        "edge_pp": round(win_rate - market_implied_underdog_p, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default="data/btc_5m_tape.parquet")
    ap.add_argument("--out",  default="logs/contrarian_sweep.json")
    ap.add_argument("--fee",  type=float, default=0.0)
    args = ap.parse_args()

    tape_path = str(Path(args.tape).resolve())
    rows = [run_one(tape_path, th, args.fee) for th in THRESHOLDS]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({
            "thresholds": rows,
            "tape_path": tape_path,
            "fee_rate": args.fee,
            "config_hash": bt2.config_hash(),
        }, f, indent=2)

    print(f"{'thr':>5} {'n':>5} {'WR':>6} {'pnl':>8} {'pnl/t':>8} {'edge_pp':>8}")
    for r in rows:
        print(f"{r['threshold']:>5.2f} {r['n_trades']:>5} "
              f"{r['win_rate']:>6.3f} {r['pnl_total']:>+8.2f} "
              f"{r['pnl_per_trade']:>+8.4f} {r['edge_pp']:>+8.4f}")
    print(f"\nSummary written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
