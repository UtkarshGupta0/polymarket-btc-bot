"""Backtest the signal engine against historical Binance 1m klines.

Fetches N days of BTCUSDT 1m candles, replays each 5-min window using
signal_engine, assumes entry fill at confidence_to_price(conf), and
computes approximate P&L.

LIMITATIONS:
  - No real Polymarket orderbook. Entry price = confidence_to_price curve.
    Real fills may differ 1-3c (slippage / queue / cancellation).
  - No volume imbalance (klines lack trade-direction). Set to 0.
  - Signal computed at T-60s (end of minute 4). Live bot uses T-45s..T-8s.
    Close enough for edge estimation.
  - Resolution: close_of_window > open_of_window => UP wins.
    Ties rare but count as LOSS (conservative).

Usage:
    python backtester.py --days 30
    python backtester.py --days 7 --min-conf 0.55
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

from price_feed import PriceState
from signal_engine import (
    compute_signal,
    confidence_to_price,
    expected_value,
    should_trade,
)
from config import CONFIG

logger = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
BARS_PER_WINDOW = 5       # 5x 1m = 5min window
SIGNAL_AT_MINUTE = 4      # compute signal at end of 4th minute (~T-60s)


# --- fetch ---

def fetch_klines(start_ms: int, end_ms: int) -> list[list]:
    """Fetch 1m klines between start/end (ms). Paginates over 1000-bar chunks."""
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        r = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        last_close = int(batch[-1][6])  # close_time ms
        if last_close <= cursor:
            break
        cursor = last_close + 1
        print(f"  fetched {len(out)} bars so far (up to {datetime.fromtimestamp(cursor/1000, tz=timezone.utc)})")
        time.sleep(0.15)  # gentle rate limit
    return out


# --- replay ---

def kline_to_tuple(k: list) -> dict:
    return {
        "open_ts": int(k[0]),   # ms
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_ts": int(k[6]),
    }


def build_state_at_signal(window_bars: list[dict]) -> PriceState:
    """Synthesize PriceState at end of minute 4 using first 4 bars."""
    first = window_bars[0]
    sig_bar = window_bars[SIGNAL_AT_MINUTE - 1]  # minute 4 (index 3)
    open_px = first["open"]
    current = sig_bar["close"]

    # VWAP approx: vol-weighted mean of (high+low+close)/3 over first 4 bars
    vw_num = 0.0
    vw_den = 0.0
    for b in window_bars[:SIGNAL_AT_MINUTE]:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        vw_num += typical * b["volume"]
        vw_den += b["volume"]
    vwap = vw_num / vw_den if vw_den > 0 else current

    # Momentum: (close of bar 4 - close of bar 3) / seconds
    # We have 1m resolution. Use price change over last bar.
    prev_close = window_bars[SIGNAL_AT_MINUTE - 2]["close"]
    momentum_per_sec = (current - prev_close) / 60.0

    delta = (current - open_px) / open_px
    st = PriceState()
    st.current_price = current
    st.window_open_price = open_px
    st.vwap = vwap
    st.momentum = momentum_per_sec
    st.delta_from_open = delta
    st.delta_from_open_abs = abs(delta)
    st.volume_imbalance = 0.0  # unavailable from klines
    st.tick_count = SIGNAL_AT_MINUTE * 60
    return st


def replay(klines: list[list], min_conf: float, ignore_ev: bool = False,
           dist: bool = False) -> list[dict]:
    """Slide over klines, align to 5-min windows, simulate trades."""
    bars = [kline_to_tuple(k) for k in klines]
    # Align: find first bar where open_ts % 300000 == 0
    first_aligned = next(
        (i for i, b in enumerate(bars) if b["open_ts"] % 300_000 == 0), None
    )
    if first_aligned is None:
        return []

    trades: list[dict] = []
    all_confs: list[float] = []
    i = first_aligned
    while i + BARS_PER_WINDOW <= len(bars):
        window = bars[i:i + BARS_PER_WINDOW]
        # Sanity: all 5 bars contiguous
        expected_ts = window[0]["open_ts"]
        contiguous = all(
            b["open_ts"] == expected_ts + j * 60_000
            for j, b in enumerate(window)
        )
        if not contiguous:
            i += 1
            continue

        # Simulate signal at end of minute 4
        sig_time = window[SIGNAL_AT_MINUTE - 1]["close_ts"] / 1000.0
        window_end_ts = window[-1]["close_ts"] / 1000.0
        state = build_state_at_signal(window)
        sig = compute_signal(state, window_end_ts, now=sig_time)

        if sig is None:
            i += BARS_PER_WINDOW
            continue

        all_confs.append(sig.confidence)

        # Gate
        if sig.confidence < min_conf:
            i += BARS_PER_WINDOW
            continue
        if not ignore_ev and sig.expected_value <= 0:
            i += BARS_PER_WINDOW
            continue

        # Resolve: compare close_of_window vs open_of_window
        close_px = window[-1]["close"]
        open_px = window[0]["open"]
        actual_dir = "UP" if close_px > open_px else "DOWN"
        won = (actual_dir == sig.direction)

        # Simulate fill at suggested_price
        entry = sig.suggested_price
        # Assume $1 bet for normalization (P&L scales with size)
        size = 1.0
        shares = size / entry
        pnl = round((shares - size) if won else -size, 4)

        iso = datetime.fromtimestamp(
            window[0]["open_ts"] / 1000.0, tz=timezone.utc
        ).isoformat()

        trades.append({
            "window_ts": window[0]["open_ts"] // 1000,
            "time": iso,
            "direction": sig.direction,
            "confidence": round(sig.confidence, 3),
            "entry_price": entry,
            "size_usdc": size,
            "shares": round(shares, 4),
            "outcome": "WIN" if won else "LOSS",
            "pnl": pnl,
            "delta_pct": round(sig.window_delta, 6),
            "seconds_to_close_at_entry": 60.0,  # approx
            "close_px": close_px,
            "open_px": open_px,
        })

        i += BARS_PER_WINDOW

    if dist and all_confs:
        all_confs.sort()
        n = len(all_confs)
        print(f"\nSignal confidence distribution ({n} windows):")
        for pct in (50, 75, 90, 95, 99):
            idx = min(n - 1, int(n * pct / 100))
            print(f"  p{pct}: {all_confs[idx]:.3f}")
        print(f"  max: {all_confs[-1]:.3f}")
        bkt = defaultdict(int)
        for c in all_confs:
            bkt[_bucket_conf(c)] += 1
        print(f"  buckets: {dict(bkt)}")

    return trades


# --- stats ---

def _bucket_conf(c: float) -> str:
    if c < 0.60: return "<60"
    if c < 0.70: return "60-70"
    if c < 0.80: return "70-80"
    if c < 0.90: return "80-90"
    return "90+"


def _bucket_hour(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H")


def _bucket_delta(d: float) -> str:
    a = abs(d) * 100
    if a < 0.01: return "<0.01%"
    if a < 0.05: return "0.01-0.05%"
    if a < 0.10: return "0.05-0.10%"
    if a < 0.25: return "0.10-0.25%"
    if a < 0.50: return "0.25-0.50%"
    return ">0.50%"


def _stats_by(trades: list[dict], key_fn) -> dict:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        g[key_fn(t)].append(t)
    out = {}
    for k, ts in sorted(g.items()):
        w = sum(1 for t in ts if t["outcome"] == "WIN")
        p = sum(t["pnl"] for t in ts)
        out[k] = {
            "n": len(ts),
            "wins": w,
            "win_rate": round(w / len(ts), 3),
            "pnl_per_$1_bet": round(p, 2),
        }
    return out


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    n = len(trades)
    w = sum(1 for t in trades if t["outcome"] == "WIN")
    total_pnl = sum(t["pnl"] for t in trades)
    streak = max_streak = 0
    for t in trades:
        if t["outcome"] == "LOSS":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "trades_total": n,
        "wins": w,
        "losses": n - w,
        "win_rate": round(w / n, 3),
        "total_pnl_per_$1_bet": round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / n, 4),
        "longest_loss_streak": max_streak,
        "by_confidence": _stats_by(trades, lambda t: _bucket_conf(t["confidence"])),
        "by_hour_utc": _stats_by(trades, lambda t: _bucket_hour(t["time"])),
        "by_delta": _stats_by(trades, lambda t: _bucket_delta(t["delta_pct"])),
        "by_direction": _stats_by(trades, lambda t: t["direction"]),
    }


def project_returns(summary: dict, starting: float, max_bet: float, kelly: float) -> dict:
    """Rough projection of daily P&L based on backtest win rate + Kelly sizing."""
    n = summary.get("trades_total", 0)
    if n == 0:
        return {}
    wr = summary["win_rate"]
    # Avg entry price across trades — approx via weighted buckets
    # Use simplified: avg pnl per $1 bet
    avg_edge = summary["avg_pnl_per_trade"]
    # Kelly f = (bp - q) / b where b = profit/cost, p = win rate
    # Simplified: trades_per_day = n / days
    # Use avg_edge * bet_size as expected profit per trade
    return {
        "trades_in_sample": n,
        "sample_win_rate": wr,
        "avg_edge_per_$1_bet": avg_edge,
        "with_max_bet_$": max_bet,
        "expected_profit_per_trade": round(avg_edge * max_bet, 3),
    }


# --- CLI ---

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-conf", type=float, default=CONFIG.min_confidence)
    ap.add_argument("--save", type=str, default=None,
                    help="Path to save raw trades JSON")
    ap.add_argument("--ignore-ev", action="store_true",
                    help="Trade on conf threshold alone, ignore EV gate")
    ap.add_argument("--dist", action="store_true",
                    help="Print confidence distribution of ALL signals")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86_400_000

    print(f"Fetching {args.days}d of BTCUSDT 1m klines from Binance...")
    klines = fetch_klines(start_ms, now_ms)
    print(f"Got {len(klines)} bars\n")

    print(f"Replaying with MIN_CONFIDENCE={args.min_conf}...")
    trades = replay(klines, args.min_conf, args.ignore_ev, args.dist)
    print(f"Simulated {len(trades)} qualifying trades\n")

    summary = summarize(trades)
    print("=" * 70)
    print(f"BACKTEST SUMMARY ({args.days} days)")
    print("=" * 70)
    print(json.dumps(summary, indent=2))

    if summary.get("trades_total", 0) > 0:
        print("\n" + "=" * 70)
        print("PROJECTION (your current config)")
        print("=" * 70)
        proj = project_returns(
            summary, CONFIG.starting_capital,
            CONFIG.max_bet_size, CONFIG.kelly_fraction,
        )
        print(json.dumps(proj, indent=2))

        # Daily rate
        trades_per_day = summary["trades_total"] / args.days
        daily_profit = proj["expected_profit_per_trade"] * trades_per_day
        print(f"\nTrades/day (avg): {trades_per_day:.1f}")
        print(f"Expected daily P&L (at MAX_BET=${CONFIG.max_bet_size}): ${daily_profit:+.2f}")
        print(f"Monthly (30d): ${daily_profit * 30:+.2f}")

    if args.save:
        with open(args.save, "w") as f:
            json.dump({"summary": summary, "trades": trades}, f, indent=2)
        print(f"\nRaw trades saved to {args.save}")

    print("\n" + "=" * 70)
    print("CAVEATS")
    print("=" * 70)
    print("- Entry price assumed = confidence_to_price curve. Real fills may")
    print("  differ 1-3c due to orderbook position / cancellation risk.")
    print("- Volume imbalance unavailable from klines; set to 0. Live signal")
    print("  will be slightly different.")
    print("- Ties counted as LOSS (conservative).")
    print("- Does NOT account for Polymarket fee schedule changes over time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
