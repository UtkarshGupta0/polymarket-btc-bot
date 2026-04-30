"""Apply the backtest-winning config to .env.

Reads logs/sweep_top20.json (or any sweep output), takes the #1 entry, backs up
.env to .env.bak.<unix_ts>, then rewrites only the affected keys in .env. Other
lines (comments, secrets, untouched keys) are preserved.

Use --dry-run to preview the diff without writing.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger("apply_bt_config")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_SWEEP = REPO_ROOT / "logs" / "sweep_top20.json"

KEYS_TO_APPLY = {
    "min_confidence": "MIN_CONFIDENCE",
    "min_edge": "MIN_EDGE",
    "min_delta_pct": "MIN_DELTA_PCT",
}


def winner_from_sweep(path: Path) -> dict:
    payload = json.loads(path.read_text())
    top = payload.get("top") or payload.get("all") or []
    if not top:
        raise SystemExit(f"no entries in {path}")
    return top[0]


def winner_to_env_pairs(winner: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, env_key in KEYS_TO_APPLY.items():
        if k in winner:
            out[env_key] = f"{winner[k]}"
    hours = winner.get("trading_hours_block") or []
    out["TRADING_HOURS_BLOCK"] = ",".join(str(h) for h in sorted(hours)) if hours else ""
    return out


def parse_env(text: str) -> tuple[list[tuple[str, str | None, str]], dict[str, int]]:
    """Return (lines, key_index)."""
    lines: list[tuple[str, str | None, str]] = []
    idx: dict[str, int] = {}
    for i, raw in enumerate(text.splitlines()):
        s = raw.strip()
        if not s or s.startswith("#"):
            lines.append(("", None, raw))
            continue
        if "=" in s:
            key, _, val = s.partition("=")
            key = key.strip()
            val = val.strip()
            lines.append((key, val, raw))
            idx[key] = i
        else:
            lines.append(("", None, raw))
    return lines, idx


def render_env(lines: list[tuple[str, str | None, str]],
               idx: dict[str, int],
               new_pairs: dict[str, str]) -> tuple[str, list[str]]:
    diff: list[str] = []
    out_lines = [raw for _, _, raw in lines]

    for key, new_val in new_pairs.items():
        if key in idx:
            old_raw = out_lines[idx[key]]
            new_raw = f"{key}={new_val}"
            if "#" in old_raw and "=" in old_raw:
                trailing = old_raw[old_raw.index("#"):]
                if trailing not in new_raw:
                    new_raw = new_raw + "  " + trailing
            if old_raw != new_raw:
                diff.append(f"- {old_raw}")
                diff.append(f"+ {new_raw}")
                out_lines[idx[key]] = new_raw
        else:
            new_raw = f"{key}={new_val}"
            diff.append(f"+ {new_raw}")
            out_lines.append(new_raw)

    return "\n".join(out_lines) + "\n", diff


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", type=Path, default=DEFAULT_SWEEP)
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.sweep.is_file():
        logger.error(f"sweep file not found: {args.sweep}")
        return 2
    if not args.env.is_file():
        logger.warning(f"{args.env} does not exist; will create on apply")

    winner = winner_from_sweep(args.sweep)
    new_pairs = winner_to_env_pairs(winner)
    logger.info(f"winner: {json.dumps({k: winner.get(k) for k in ('roi','total_pnl','min_confidence','min_edge','min_delta_pct','trading_hours_block')}, default=str)}")

    text = args.env.read_text() if args.env.is_file() else ""
    lines, idx = parse_env(text)
    new_text, diff = render_env(lines, idx, new_pairs)

    print("=" * 70)
    print(f"DIFF for {args.env}")
    print("=" * 70)
    if not diff:
        print("(no changes — current values already match winner)")
    else:
        for d in diff:
            print(d)

    if args.dry_run:
        print("\n[dry-run] no files written. Re-run without --dry-run to apply.")
        return 0
    if not diff:
        return 0

    if args.env.is_file():
        bak = args.env.with_name(f".env.bak.{int(time.time())}")
        shutil.copy2(args.env, bak)
        logger.info(f"backed up to {bak}")

    args.env.write_text(new_text)
    logger.info(f"wrote {args.env}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
