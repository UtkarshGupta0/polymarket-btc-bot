"""Build the per-window backtest tape.

Joins:
  - data/btc_5m_markets.parquet  (filtered Polymarket markets)
  - ~/poly_data/processed/trades.csv  (Polymarket fills)
  - Binance BTCUSDT 1m klines  (fetched on demand)

Output: data/btc_5m_tape.parquet  (one row per 5-min window).

Schema (per row):
  window_ts: int (unix seconds, aligned to 5-min boundary)
  condition_id: str
  token_up: str
  token_down: str
  btc_open: float
  btc_close: float
  bar_closes / bar_highs / bar_lows / bar_volumes: list[float] of 5 entries
  up_fills / down_fills: list[(unix_sec, price, usd)]

Only taker-BUY fills are kept (they hit the resting ask). Sell-side fills tell
us about the bid, not the ask we'd pay as a maker buy.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtester import fetch_klines  # noqa: E402

logger = logging.getLogger("build_market_tape")

DEFAULT_POLY_DATA = os.path.expanduser("~/poly_data")
DEFAULT_MARKETS = "data/btc_5m_markets.parquet"
DEFAULT_OUT = "data/btc_5m_tape.parquet"
DEFAULT_META = "data/btc_5m_tape_meta.json"

EXPECTED_TRADE_COLS = [
    "timestamp", "market_id", "maker", "taker", "nonusdc_side",
    "maker_direction", "taker_direction", "price", "usd_amount",
    "token_amount", "transactionHash",
]

EXPECTED_ORDERFILLED_COLS = [
    "timestamp", "maker", "makerAssetId", "makerAmountFilled",
    "taker", "takerAssetId", "takerAmountFilled", "transactionHash",
]


def _orient_up_down(answer1: str, answer2: str, token1: str, token2: str
                    ) -> tuple[str, str]:
    a1 = (answer1 or "").strip().lower()
    a2 = (answer2 or "").strip().lower()
    UP = {"up", "yes", "higher", "above"}
    DOWN = {"down", "no", "lower", "below"}
    if a1 in UP or any(a1.startswith(t) for t in UP):
        return token1, token2
    if a2 in UP or any(a2.startswith(t) for t in UP):
        return token2, token1
    if a1 in DOWN:
        return token2, token1
    if a2 in DOWN:
        return token1, token2
    return token1, token2  # fallback (documented)


def load_markets(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix == ".parquet":
        return pd.read_parquet(p)
    return pd.read_csv(p, low_memory=False)


def annotate_markets(markets: pd.DataFrame) -> pd.DataFrame:
    out = markets.copy()
    out["createdAt"] = pd.to_datetime(out["createdAt"], errors="coerce", utc=True)

    # Prefer the slug-embedded timestamp (e.g. `btc-updown-5m-1766162100`) since
    # `closedTime` is null on markets that haven't been resolved on-chain yet
    # (true for almost every BTC 5m market in Polymarket's API).
    if "window_ts" not in out.columns or out["window_ts"].isna().all():
        out["window_ts"] = out["market_slug"].fillna("").astype(str).str.extract(
            r"-(\d{10})$", expand=False
        ).astype("Int64")

    pairs = out.apply(
        lambda r: _orient_up_down(r.get("answer1"), r.get("answer2"),
                                  r.get("token1"), r.get("token2")),
        axis=1,
    )
    out["token_up"] = [u for u, _ in pairs]
    out["token_down"] = [d for _, d in pairs]
    out = out.dropna(subset=["window_ts"]).copy()
    out["window_ts"] = out["window_ts"].astype("int64")
    return out


def stream_fills_for_markets(
    trades_csv: str,
    market_ids: set[str],
    token_to_side: dict[str, dict[str, str]],
    chunksize: int = 1_000_000,
) -> dict[str, dict[str, list[tuple[int, float, float]]]]:
    out: dict[str, dict[str, list[tuple[int, float, float]]]] = {
        mid: {"up": [], "down": []} for mid in market_ids
    }
    seen_chunks = seen_rows = 0

    reader = pd.read_csv(
        trades_csv, low_memory=False, chunksize=chunksize, usecols=EXPECTED_TRADE_COLS,
        dtype={"market_id": str},
    )
    for chunk in reader:
        seen_chunks += 1
        seen_rows += len(chunk)
        sel = chunk.loc[chunk["market_id"].astype(str).isin(market_ids)]
        sel = sel.loc[sel["taker_direction"] == "BUY"]
        if sel.empty:
            continue
        ts = pd.to_datetime(sel["timestamp"], errors="coerce", utc=True)
        # pandas 3.0+ defaults to datetime64[us]; older to ns. Convert via
        # numpy's seconds-precision int (after dropping tz) to be unit-agnostic.
        ts_unix = ts.dt.tz_convert(None).astype("datetime64[s]").astype("int64")
        for mid, side, t, price, usd in zip(
            sel["market_id"].values,
            sel["nonusdc_side"].values,
            ts_unix.values,
            sel["price"].astype(float).values,
            sel["usd_amount"].astype(float).values,
        ):
            sides = token_to_side.get(mid)
            if sides is None:
                continue
            label = sides.get(side)
            if label is None:
                continue
            out[mid][label].append((int(t), float(price), float(usd)))

        if seen_chunks % 10 == 0:
            logger.info(
                f"streamed {seen_rows:,} rows, "
                f"{sum(len(v['up']) + len(v['down']) for v in out.values()):,} fills kept"
            )

    for d in out.values():
        d["up"].sort()
        d["down"].sort()
    return out


def stream_fills_from_orderfilled(
    orderfilled_csv: str,
    token_to_market: dict[str, str],
    token_to_label: dict[str, str],
    min_ts: int = 0,
    chunksize: int = 500_000,
) -> dict[str, dict[str, list[tuple[int, float, float]]]]:
    """Stream raw goldsky/orderFilled.csv. Avoids running process_live on 39GB.

    Replicates process_live.py's price logic inline (process_live.py:52-96):
      - Identify the non-USDC asset id (the side != "0").
      - taker pays USDC for token = BUY (hits ask); price = takerAmount / makerAmount.
      - taker receives USDC = SELL (hits bid); we skip these.
      - Amounts are scaled by 10**6 (USDC + token decimals on Polymarket are 6).
    """
    out: dict[str, dict[str, list[tuple[int, float, float]]]] = {}
    seen_chunks = seen_rows = kept = 0
    tokens = set(token_to_market.keys())

    reader = pd.read_csv(
        orderfilled_csv, low_memory=False, chunksize=chunksize,
        usecols=EXPECTED_ORDERFILLED_COLS,
        dtype={"makerAssetId": str, "takerAssetId": str},
    )
    for chunk in reader:
        seen_chunks += 1
        seen_rows += len(chunk)
        if min_ts:
            chunk = chunk.loc[chunk["timestamp"] >= min_ts]
            if chunk.empty:
                if seen_chunks % 50 == 0:
                    logger.info(f"streamed {seen_rows:,} orderFilled rows (pre-min_ts)")
                continue
        # Filter rows that touch any of our BTC tokens
        m = chunk["makerAssetId"].isin(tokens) | chunk["takerAssetId"].isin(tokens)
        sel = chunk.loc[m]
        if sel.empty:
            if seen_chunks % 20 == 0:
                logger.info(f"streamed {seen_rows:,} orderFilled rows, kept {kept:,}")
            continue

        for _, r in sel.iterrows():
            mka = str(r["makerAssetId"])
            tka = str(r["takerAssetId"])
            taker_pays_usdc = (tka == "0")
            if not taker_pays_usdc:
                # Taker SELL (received USDC) → bid hit, not ask. Skip.
                continue
            if mka == "0":
                # Both sides USDC? shouldn't happen
                continue
            non_usdc = mka
            mkt_id = token_to_market.get(non_usdc)
            if mkt_id is None:
                continue
            label = token_to_label.get(non_usdc)
            if label is None:
                continue
            try:
                t = int(r["timestamp"])
            except (TypeError, ValueError):
                continue
            try:
                taker_amt = float(r["takerAmountFilled"])
                maker_amt = float(r["makerAmountFilled"])
            except (TypeError, ValueError):
                continue
            if maker_amt <= 0:
                continue
            # taker paid USDC (takerAmountFilled, scaled 10**6) for token (makerAmountFilled, 10**6)
            usd = taker_amt / 1e6
            tok = maker_amt / 1e6
            if tok <= 0:
                continue
            price = usd / tok
            d = out.setdefault(mkt_id, {"up": [], "down": []})
            d[label].append((t, float(price), float(usd)))
            kept += 1

        if seen_chunks % 20 == 0:
            logger.info(f"streamed {seen_rows:,} orderFilled rows, kept {kept:,}")

    for d in out.values():
        d["up"].sort()
        d["down"].sort()
    return out


def build_kline_index(klines: list[list]) -> dict[int, dict]:
    idx = {}
    for k in klines:
        open_ts = int(k[0]) // 1000
        idx[open_ts] = {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
    return idx


def assemble_tape(markets: pd.DataFrame,
                  fills: dict[str, dict],
                  klines: dict[int, dict]) -> pd.DataFrame:
    rows = []
    skipped_no_klines = skipped_no_fills = 0
    for _, m in markets.iterrows():
        ws = int(m["window_ts"])
        bars = []
        ok = True
        for i in range(5):
            b = klines.get(ws + i * 60)
            if b is None:
                ok = False
                break
            bars.append(b)
        if not ok:
            skipped_no_klines += 1
            continue

        mf = fills.get(str(m["id"])) or {"up": [], "down": []}
        if len(mf["up"]) + len(mf["down"]) == 0:
            skipped_no_fills += 1
            continue

        rows.append({
            "window_ts": ws,
            "condition_id": str(m.get("condition_id", "")),
            "token_up": str(m["token_up"]),
            "token_down": str(m["token_down"]),
            "btc_open": bars[0]["open"],
            "btc_close": bars[-1]["close"],
            "bar_closes": [b["close"] for b in bars],
            "bar_highs": [b["high"] for b in bars],
            "bar_lows": [b["low"] for b in bars],
            "bar_volumes": [b["volume"] for b in bars],
            "up_fills": mf["up"],
            "down_fills": mf["down"],
        })

    logger.info(
        f"assembled {len(rows):,} windows; "
        f"skipped {skipped_no_klines} (no klines) + {skipped_no_fills} (no fills)"
    )
    return pd.DataFrame(rows)


def fetch_klines_for_range(start_ts: int, end_ts: int) -> dict[int, dict]:
    raw = fetch_klines(start_ts * 1000, (end_ts + 60) * 1000)
    return build_kline_index(raw)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=DEFAULT_MARKETS)
    ap.add_argument("--trades", default=os.path.join(DEFAULT_POLY_DATA, "processed/trades.csv"))
    ap.add_argument("--orderfilled",
                    default=os.path.join(DEFAULT_POLY_DATA, "goldsky/orderFilled.csv"),
                    help="Raw goldsky orderFilled.csv (used if --trades missing)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--meta", default=DEFAULT_META)
    ap.add_argument("--days", type=int, default=90,
                    help="Limit windows to last N days (0 = no limit)")
    args = ap.parse_args()

    markets = load_markets(args.markets)
    logger.info(f"loaded {len(markets):,} markets from {args.markets}")
    markets = annotate_markets(markets)

    if args.days > 0:
        cutoff = int(time.time()) - args.days * 86_400
        markets = markets.loc[markets["window_ts"] >= cutoff].copy()
        logger.info(f"limited to last {args.days}d → {len(markets):,} markets")

    if markets.empty:
        logger.error("no markets after filter; nothing to build")
        return 2

    market_ids = set(markets["id"].astype(str))
    token_to_side: dict[str, dict[str, str]] = {}
    token_to_market: dict[str, str] = {}
    token_to_label: dict[str, str] = {}
    for _, m in markets.iterrows():
        is_up_t1 = str(m["token_up"]) == str(m["token1"])
        mkt = str(m["id"])
        token_to_side[mkt] = {
            "token1": "up" if is_up_t1 else "down",
            "token2": "down" if is_up_t1 else "up",
        }
        token_to_market[str(m["token1"])] = mkt
        token_to_market[str(m["token2"])] = mkt
        token_to_label[str(m["token1"])] = "up" if is_up_t1 else "down"
        token_to_label[str(m["token2"])] = "down" if is_up_t1 else "up"

    if Path(args.trades).is_file():
        fills = stream_fills_for_markets(args.trades, market_ids, token_to_side)
    elif Path(args.orderfilled).is_file():
        logger.info(f"trades.csv missing; streaming raw {args.orderfilled} instead "
                    f"(replicates process_live price logic inline)")
        # Skip orderFilled rows older than the earliest BTC market we care about
        # (39GB total, but only the recent slice can match our token set).
        min_ts = int(markets["window_ts"].min())
        logger.info(f"min_ts prefilter: {min_ts} ({pd.to_datetime(min_ts, unit='s', utc=True)})")
        fills = stream_fills_from_orderfilled(
            args.orderfilled, token_to_market, token_to_label, min_ts=min_ts
        )
    else:
        logger.error(f"neither {args.trades} nor {args.orderfilled} present.")
        return 2
    n_fills = sum(len(v["up"]) + len(v["down"]) for v in fills.values())
    logger.info(f"collected {n_fills:,} taker-BUY fills across {len(market_ids)} markets")

    earliest_ws = int(markets["window_ts"].min())
    latest_ws = int(markets["window_ts"].max()) + 300
    logger.info(f"fetching Binance klines {earliest_ws} → {latest_ws}")
    kidx = fetch_klines_for_range(earliest_ws, latest_ws)
    logger.info(f"got {len(kidx):,} 1m bars")

    tape = assemble_tape(markets, fills, kidx)
    if tape.empty:
        logger.error("tape empty; nothing to write")
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tape.to_parquet(out_path, index=False)
    except Exception as e:
        logger.warning(f"parquet write failed ({e}); falling back to JSON")
        out_path = out_path.with_suffix(".json")
        with open(out_path, "w") as f:
            json.dump(tape.to_dict(orient="records"), f)
    logger.info(f"wrote tape to {out_path} ({len(tape):,} windows)")

    meta = {
        "tape_path": str(out_path),
        "n_windows": int(len(tape)),
        "n_markets_seen": int(len(market_ids)),
        "n_fills": int(n_fills),
        "earliest_window_ts": int(tape["window_ts"].min()),
        "latest_window_ts": int(tape["window_ts"].max()),
        "kline_source": "Binance BTCUSDT 1m via api.binance.com/api/v3/klines",
        "trades_source": str(Path(args.trades).resolve()),
        "markets_source": str(Path(args.markets).resolve()),
    }
    with open(args.meta, "w") as f:
        json.dump(meta, f, indent=2)
    logger.info(f"wrote meta to {args.meta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
