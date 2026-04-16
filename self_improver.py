"""Weekly performance review via Claude API. Prints recommendations.

Usage:
    python self_improver.py                # last 7 days
    python self_improver.py --days 14      # custom window
    python self_improver.py --dry-run      # show payload, skip API call
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config import CONFIG

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent / "logs"
MODEL_ID = "claude-sonnet-4-6"
MAX_TOKENS = 1500


# --- Log collection ---

def collect_trades(days: int) -> list[dict]:
    """Gather resolved trades from the last `days` days of log files."""
    today = datetime.now(timezone.utc).date()
    trades: list[dict] = []
    for i in range(days):
        day = today - timedelta(days=i)
        path = LOG_DIR / f"trades_{day.strftime('%Y%m%d')}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"skipping bad log {path}: {e}")
            continue
        for t in data.get("trades", []):
            if t.get("outcome") in ("WIN", "LOSS"):  # ignore SKIPPED/unfilled
                trades.append(t)
    return trades


# --- Aggregations ---

def _bucket_conf(c: float) -> str:
    if c < 0.60: return "<60"
    if c < 0.70: return "60-70"
    if c < 0.80: return "70-80"
    if c < 0.90: return "80-90"
    return "90+"


def _bucket_hour(iso_ts: str) -> str:
    try:
        h = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).hour
    except Exception:
        return "?"
    return f"{h:02d}"


def _bucket_delta(d_pct: float) -> str:
    a = abs(d_pct) * 100
    if a < 0.01: return "<0.01%"
    if a < 0.05: return "0.01-0.05%"
    if a < 0.10: return "0.05-0.10%"
    if a < 0.25: return "0.10-0.25%"
    if a < 0.50: return "0.25-0.50%"
    return ">0.50%"


def _stats(trades: Iterable[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    out = {}
    for k, ts in sorted(groups.items()):
        wins = sum(1 for t in ts if t["outcome"] == "WIN")
        pnl = sum(t.get("pnl", 0) or 0 for t in ts)
        out[k] = {
            "n": len(ts),
            "wins": wins,
            "win_rate": round(wins / len(ts), 3) if ts else 0,
            "pnl": round(pnl, 2),
        }
    return out


def build_summary(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}

    total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    wins = sum(1 for t in trades if t["outcome"] == "WIN")

    # Longest loss streak
    streak = max_streak = 0
    for t in trades:
        if t["outcome"] == "LOSS":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "trades_total": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades), 3),
        "total_pnl": round(total_pnl, 2),
        "longest_loss_streak": max_streak,
        "by_confidence": _stats(trades, lambda t: _bucket_conf(t.get("confidence", 0))),
        "by_hour_utc": _stats(trades, lambda t: _bucket_hour(t.get("time", t.get("time_iso", "")))),
        "by_delta": _stats(trades, lambda t: _bucket_delta(t.get("delta_pct", 0) or 0)),
        "by_direction": _stats(trades, lambda t: t.get("direction", "?")),
        "by_seconds_to_close": _stats(
            trades,
            lambda t: (
                "T-0-10" if (t.get("seconds_to_close_at_entry") or 0) < 10
                else "T-10-20" if (t.get("seconds_to_close_at_entry") or 0) < 20
                else "T-20-30" if (t.get("seconds_to_close_at_entry") or 0) < 30
                else "T-30+"
            ),
        ),
    }


# --- Claude call ---

SYSTEM_PROMPT = """You are a trading bot analyst. You review binary option trade logs
from a Polymarket BTC 5-minute maker bot and suggest parameter adjustments.

The bot trades binary UP/DOWN markets. Entry is a resting GTC limit buy (maker) in the
final 45 seconds of each 5-min window. Current tunable parameters:

- MIN_CONFIDENCE: threshold below which no trade is placed
- ENTRY_WINDOW_START: seconds before close when entry logic activates
- ENTRY_WINDOW_END: seconds before close when entry logic stops
- Signal weights: delta=0.40, momentum=0.25, volume=0.20, vwap=0.15 (must sum to 1.0)
- KELLY_FRACTION: Kelly sizing multiplier (conservative: 0.25)

Review statistics and:
1. Win rate breakdown by confidence bucket — is higher confidence actually better?
2. Win rate by hour-of-day — are there consistently profitable / unprofitable hours?
3. Win rate by BTC delta magnitude — do small moves win more or less than large ones?
4. Pattern in consecutive losses — clustered in specific regimes?
5. Entry timing — is T-8s to T-45s optimal?

Return crisp, actionable recommendations. Give EXACT new numbers, not ranges.
If insufficient data (<50 trades), say so and suggest a min sample size before acting."""


def call_claude(summary: dict, raw_sample: list[dict]) -> str:
    import anthropic
    if not CONFIG.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    client = anthropic.Anthropic(api_key=CONFIG.anthropic_api_key)

    current_params = {
        "MIN_CONFIDENCE": CONFIG.min_confidence,
        "ENTRY_WINDOW_START": CONFIG.entry_window_start,
        "ENTRY_WINDOW_END": CONFIG.entry_window_end,
        "KELLY_FRACTION": CONFIG.kelly_fraction,
        "MAX_BET_SIZE": CONFIG.max_bet_size,
        "MAX_DAILY_DRAWDOWN": CONFIG.max_daily_drawdown,
    }

    user_content = [
        # Cacheable: static schema/context block
        {
            "type": "text",
            "text": (
                "Current parameters:\n"
                f"{json.dumps(current_params, indent=2)}\n\n"
                "Statistic schema:\n"
                "- Top-level: trades_total, wins, losses, win_rate, total_pnl, longest_loss_streak\n"
                "- by_confidence: buckets <60, 60-70, 70-80, 80-90, 90+\n"
                "- by_hour_utc: 24 hour buckets\n"
                "- by_delta: abs(BTC delta) buckets\n"
                "- by_direction: UP vs DOWN\n"
                "- by_seconds_to_close: entry timing buckets\n"
            ),
            "cache_control": {"type": "ephemeral"},
        },
        # Dynamic: this run's aggregates
        {
            "type": "text",
            "text": f"Aggregated stats:\n{json.dumps(summary, indent=2)}",
        },
    ]
    # Optional raw sample for qualitative review (small, not cached)
    if raw_sample:
        user_content.append({
            "type": "text",
            "text": f"Recent 10 trades (sample):\n{json.dumps(raw_sample[-10:], indent=2)}",
        })

    resp = client.messages.create(
        model=MODEL_ID,
        max_tokens=MAX_TOKENS,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    # Report cache stats
    u = resp.usage
    logger.info(
        f"tokens: in={u.input_tokens} out={u.output_tokens} "
        f"cache_create={getattr(u, 'cache_creation_input_tokens', 0)} "
        f"cache_read={getattr(u, 'cache_read_input_tokens', 0)}"
    )
    return resp.content[0].text


# --- CLI ---

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print payload, skip API call")
    args = ap.parse_args()

    trades = collect_trades(args.days)
    summary = build_summary(trades)
    print("=" * 70)
    print(f"TRADES (last {args.days} days): {len(trades)}")
    print("=" * 70)
    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print("\n[dry-run] skipping API call")
        return 0

    if len(trades) < 10:
        print(f"\nToo few trades ({len(trades)}) for meaningful review. Need ≥50.")
        return 0

    if not CONFIG.anthropic_api_key:
        print("\nANTHROPIC_API_KEY not set; cannot call Claude.")
        return 1

    print("\n" + "=" * 70)
    print("CLAUDE REVIEW")
    print("=" * 70)
    try:
        text = call_claude(summary, trades)
    except Exception as e:
        print(f"API call failed: {e}")
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
