"""Fetch trade history per market via Polymarket data-api.

Faster + more current than waiting for `update_goldsky` to backfill — the
poly_data snapshot ends in 2025-10 while our BTC 5-min markets start in
2025-12, so the snapshot has zero overlap with the markets we care about.

Endpoint: GET https://data-api.polymarket.com/trades?market=<conditionId>&limit=500&offset=N

Output CSV mirrors `~/poly_data/processed/trades.csv` so
`scripts/build_market_tape.py --trades data/btc_5m_clob_trades.csv` works
without changes.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger("fetch_clob_trades")

DEFAULT_MARKETS = "data/btc_5m_markets.csv"
DEFAULT_OUT = "data/btc_5m_clob_trades.csv"
API = "https://data-api.polymarket.com/trades"
PAGE = 500

OUT_COLS = [
    "timestamp", "market_id", "maker", "taker", "nonusdc_side",
    "maker_direction", "taker_direction", "price", "usd_amount",
    "token_amount", "transactionHash",
]


def fetch_market_trades(condition_id: str, session: requests.Session,
                        max_pages: int = 200) -> list[dict]:
    out = []
    for page in range(max_pages):
        params = {"market": condition_id, "limit": PAGE, "offset": page * PAGE}
        rows = None
        for attempt in range(3):
            try:
                r = session.get(API, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                rows = r.json()
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"  {condition_id[:18]}... page {page}: giving up ({e})")
                    return out
                time.sleep(1 + attempt)
        if rows is None or not rows:
            return out
        out.extend(rows)
        if len(rows) < PAGE:
            return out
    return out


def transform(row: dict, token1: str, token2: str, market_id: str) -> dict | None:
    asset = str(row.get("asset", ""))
    if asset == token1:
        nonusdc_side = "token1"
    elif asset == token2:
        nonusdc_side = "token2"
    else:
        return None
    side = (row.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    taker_direction = side
    maker_direction = "SELL" if side == "BUY" else "BUY"
    try:
        price = float(row["price"])
        size = float(row["size"])
        ts = int(row["timestamp"])
    except (KeyError, ValueError, TypeError):
        return None
    return {
        "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "market_id": market_id,
        "maker": "",
        "taker": row.get("proxyWallet") or "",
        "nonusdc_side": nonusdc_side,
        "maker_direction": maker_direction,
        "taker_direction": taker_direction,
        "price": price,
        "usd_amount": price * size,
        "token_amount": size,
        "transactionHash": row.get("transactionHash") or "",
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=DEFAULT_MARKETS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit-markets", type=int, default=0,
                    help="Only fetch first N markets (smoke test)")
    ap.add_argument("--rate", type=float, default=0.0,
                    help="Sleep N seconds between markets")
    args = ap.parse_args()

    markets = pd.read_csv(args.markets, low_memory=False, dtype={
        "id": str, "condition_id": str, "token1": str, "token2": str,
    })
    if args.limit_markets:
        markets = markets.head(args.limit_markets)
    logger.info(f"loaded {len(markets):,} markets")

    sess = requests.Session()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    total = 0
    n_markets_with_trades = 0
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        for i, (_, m) in enumerate(markets.iterrows()):
            cid = str(m.get("condition_id") or "")
            mid = str(m.get("id") or cid)
            t1 = str(m["token1"])
            t2 = str(m["token2"])
            if not cid:
                continue
            rows = fetch_market_trades(cid, sess)
            kept = 0
            for r in rows:
                t = transform(r, t1, t2, market_id=mid)
                if t is None:
                    continue
                w.writerow(t)
                kept += 1
            if kept:
                n_markets_with_trades += 1
            total += kept
            if i and i % 50 == 0:
                logger.info(f"  market {i + 1}/{len(markets)}: total {total} fills, "
                            f"{n_markets_with_trades} markets had trades")
            if args.rate > 0:
                time.sleep(args.rate)

    logger.info(f"wrote {args.out} ({total} fills, {n_markets_with_trades} markets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
