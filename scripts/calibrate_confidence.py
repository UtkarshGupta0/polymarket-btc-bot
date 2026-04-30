"""Build a confidence calibration remap from a backtest log.

Read logs/bt_v2_<hash>.json (default-config trades), bucket by raw confidence,
compute realised win-rate per bucket, fit a monotone (isotonic) remap so that
remapped[c] approximates P(win | raw_conf = c).

Output: data/confidence_remap.json
  {"buckets": [{"raw_lo": 0.50, "raw_hi": 0.55, "calibrated": 0.52}, ...]}

Used by signal_engine when CONFIG.signal_variant == "calibrated".
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("calibrate_confidence")

DEFAULT_OUT = "data/confidence_remap.json"


def _isotonic(xs: list[float], ys: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators isotonic regression (non-decreasing fit)."""
    n = len(ys)
    out = list(ys)
    w = list(weights)
    i = 0
    while i < n - 1:
        if out[i] > out[i + 1]:
            tot_w = w[i] + w[i + 1]
            tot = out[i] * w[i] + out[i + 1] * w[i + 1]
            out[i] = out[i + 1] = tot / tot_w
            w[i] = w[i + 1] = tot_w
            j = i
            while j > 0 and out[j - 1] > out[j]:
                tot_w = w[j - 1] + w[j]
                tot = out[j - 1] * w[j - 1] + out[j] * w[j]
                out[j - 1] = out[j] = tot / tot_w
                w[j - 1] = w[j] = tot_w
                j -= 1
            continue
        i += 1
    return out


def calibrate(trades: list[dict], bin_width: float = 0.05) -> list[dict]:
    bins: dict[int, dict] = {}
    for t in trades:
        if t.get("outcome") not in ("WIN", "LOSS"):
            continue
        c = float(t["confidence"])
        if c < 0 or c > 1:
            continue
        idx = int(c // bin_width)
        b = bins.setdefault(idx, {"n": 0, "wins": 0})
        b["n"] += 1
        if t["outcome"] == "WIN":
            b["wins"] += 1

    sorted_idx = sorted(bins)
    if not sorted_idx:
        return []

    centres = [(i + 0.5) * bin_width for i in sorted_idx]
    raw_wr = [bins[i]["wins"] / bins[i]["n"] for i in sorted_idx]
    weights = [float(bins[i]["n"]) for i in sorted_idx]
    iso = _isotonic(centres, raw_wr, weights)

    out = []
    for i, idx in enumerate(sorted_idx):
        lo = idx * bin_width
        hi = lo + bin_width
        out.append({
            "raw_lo": round(lo, 4),
            "raw_hi": round(hi, 4),
            "raw_n": bins[idx]["n"],
            "raw_wins": bins[idx]["wins"],
            "raw_win_rate": round(raw_wr[i], 4),
            "calibrated": round(iso[i], 4),
        })
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="logs/bt_v2_<hash>.json with default-config trades")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--bin-width", type=float, default=0.05)
    args = ap.parse_args()

    payload = json.loads(Path(args.inp).read_text())
    trades = payload.get("trades", [])
    logger.info(f"loaded {len(trades)} trades from {args.inp}")

    buckets = calibrate(trades, bin_width=args.bin_width)
    if not buckets:
        logger.error("no settled trades; cannot calibrate")
        return 2

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"source": str(Path(args.inp).resolve()),
                   "bin_width": args.bin_width, "buckets": buckets}, f, indent=2)
    logger.info(f"wrote {args.out} ({len(buckets)} buckets)")

    print("=" * 70)
    print("CALIBRATION TABLE")
    print("=" * 70)
    print(f"{'raw lo':>8} {'raw hi':>8} {'n':>6} {'raw WR':>8} {'cal':>8}")
    for b in buckets:
        print(f"{b['raw_lo']:>8.4f} {b['raw_hi']:>8.4f} {b['raw_n']:>6} "
              f"{b['raw_win_rate']:>8.4f} {b['calibrated']:>8.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
