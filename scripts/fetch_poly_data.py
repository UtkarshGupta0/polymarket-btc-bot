"""Filter poly_data markets.csv to BTC up/down 5-min markets.

Input:  ~/poly_data/markets.csv  (created by `uv run python update_all.py`)
Output: data/btc_5m_markets.parquet  (or .csv if pyarrow missing)

The slug regex is verified by `--inspect` mode which dumps the first 20 BTC-related
question texts so we can confirm the actual pattern before committing.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger("fetch_poly_data")

DEFAULT_POLY_DATA = os.path.expanduser("~/poly_data")
DEFAULT_OUT = "data/btc_5m_markets.parquet"

EXPECTED_MARKET_COLS = [
    "createdAt", "id", "question", "answer1", "answer2", "neg_risk",
    "market_slug", "token1", "token2", "condition_id", "volume",
    "ticker", "closedTime",
]

# Verified empirically: BTC 5-min up/down markets use the slug shape
# `btc-updown-5m-<unix_ts>`. Other durations exist (15m, 4h) and must NOT
# match this pattern.
DEFAULT_SLUG_PATTERNS = [
    r"^btc-updown-5m-\d{10}$",
]

DEFAULT_QUESTION_HINTS: list[str] = []  # rely on slug-only filter for precision


def load_markets(poly_data_dir: str) -> pd.DataFrame:
    p = Path(poly_data_dir) / "markets.csv"
    if not p.is_file():
        raise FileNotFoundError(
            f"{p} not found. Run `cd {poly_data_dir} && uv run python update_all.py` first."
        )
    df = pd.read_csv(p, low_memory=False)
    missing = [c for c in EXPECTED_MARKET_COLS if c not in df.columns]
    if missing:
        logger.warning(f"markets.csv missing columns {missing}; got {list(df.columns)}")
    return df


def filter_btc_5m(
    df: pd.DataFrame,
    slug_patterns: list[str] = None,
    question_hints: list[str] = None,
) -> pd.DataFrame:
    slug_patterns = slug_patterns or DEFAULT_SLUG_PATTERNS
    question_hints = question_hints or DEFAULT_QUESTION_HINTS

    slug_re = re.compile("|".join(slug_patterns), re.IGNORECASE)
    slug_col = df["market_slug"].fillna("").astype(str)
    mask = slug_col.str.contains(slug_re)
    if question_hints:
        qhint_re = re.compile("|".join(re.escape(h) for h in question_hints),
                              re.IGNORECASE)
        q_col = df["question"].fillna("").astype(str)
        mask |= q_col.str.contains(qhint_re)
    out = df.loc[mask].copy()

    # Extract window_ts from the trailing unix-seconds in the slug (e.g.
    # `btc-updown-5m-1766162100`). Polymarket leaves `closedTime` null while a
    # market is unresolved, so we cannot rely on it.
    out["slug_ts"] = out["market_slug"].fillna("").astype(str).str.extract(
        r"-(\d{10})$", expand=False
    )
    out = out.loc[out["slug_ts"].notna()].copy()
    out["window_ts"] = out["slug_ts"].astype(int)
    out = out.drop(columns=["slug_ts"])

    return out.sort_values("window_ts").reset_index(drop=True)


def inspect(df: pd.DataFrame, n: int = 20) -> None:
    qcol = df["question"].fillna("").astype(str).str.lower()
    btc_rows = df.loc[qcol.str.contains("btc|bitcoin")].head(n)
    print(f"--- first {len(btc_rows)} BTC-related markets in markets.csv ---")
    for _, r in btc_rows.iterrows():
        print(f"slug={r.get('market_slug', '?')!r}")
        print(f"  question={r.get('question', '')[:120]}")
        print(f"  createdAt={r.get('createdAt')}  closedTime={r.get('closedTime')}")
        print(f"  ticker={r.get('ticker', '?')}")
        print()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--poly-data", default=DEFAULT_POLY_DATA)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--slug-pattern", action="append", default=None,
                    help="Override regex(es) for market_slug; can repeat")
    args = ap.parse_args()

    df = load_markets(args.poly_data)
    logger.info(f"loaded {len(df):,} markets from {args.poly_data}/markets.csv")

    if args.inspect:
        inspect(df)
        return 0

    btc = filter_btc_5m(df, slug_patterns=args.slug_pattern)
    logger.info(f"filtered to {len(btc):,} BTC up/down 5-min markets")

    if len(btc) == 0:
        logger.error("no markets matched. Re-run with --inspect to verify the regex.")
        return 2

    keep = [c for c in EXPECTED_MARKET_COLS if c in btc.columns] + ["window_ts"]
    btc = btc[keep]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix == ".parquet":
        try:
            btc.to_parquet(out_path, index=False)
        except Exception as e:
            logger.warning(f"parquet write failed ({e}); falling back to CSV")
            out_path = out_path.with_suffix(".csv")
            btc.to_csv(out_path, index=False)
    else:
        btc.to_csv(out_path, index=False)

    logger.info(f"wrote {out_path} ({len(btc):,} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
