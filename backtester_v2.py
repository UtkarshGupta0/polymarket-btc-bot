"""Faithful historical replay using poly_data fills + Binance klines.

Differences from backtester.py:
  * Real Polymarket ask proxy (last on-chain fill price within 30s of decision tick).
  * Honours `gate_vs_market` with min_edge / min_delta_pct / hours_block.
  * Drives the live `PaperExecutor` and `RiskManager` (sizing, drawdown halt).
  * `_FrozenClock` patches risk_manager.datetime + executor.time + executor.datetime
    so daily resets, Trade.placed_at, and Trade.time_iso line up with replay ts.
  * Optional fee model (Polymarket taker fee deducted from payout).

Inputs:
  data/btc_5m_tape.parquet — built by scripts/build_market_tape.py.

Outputs:
  logs/bt_v2_<config_hash>.json — summary + per-trade rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional
from unittest import mock

import config as _config_mod
import executor as _executor_mod
import risk_manager as _risk_mod
import signal_engine as _signal_mod
from config import CONFIG
from executor import PaperExecutor, Trade
from price_feed import PriceState
from risk_manager import RiskManager
from signal_engine import compute_signal, gate_vs_market

logger = logging.getLogger("backtester_v2")

REPRICE_INTERVAL = 5  # seconds, mirrors CONFIG.reprice_interval_sec
ENTRY_WINDOW_START = None  # populated from CONFIG at runtime
ENTRY_WINDOW_END = None
CANCEL_FAIL_THRESHOLD = 3  # mirrors bot reprice/cancel hysteresis
DEFAULT_FEE_RATE = 0.0
ASK_FRESHNESS_SEC = 30


# --- frozen clock shim --------------------------------------------------------

class _FrozenDateTime:
    """Stub of `datetime` whose `now()` returns a fixed unix timestamp."""

    def __init__(self, ts_holder: dict) -> None:
        self._ts_holder = ts_holder

    def now(self, tz=None):
        return datetime.fromtimestamp(self._ts_holder["t"], tz)

    def fromtimestamp(self, ts, tz=None):
        return datetime.fromtimestamp(ts, tz)

    def __getattr__(self, item):
        return getattr(datetime, item)


class _FrozenTime:
    """Stub of `time` module whose `time()` returns the frozen unix timestamp."""

    def __init__(self, ts_holder: dict) -> None:
        self._ts_holder = ts_holder

    def time(self):
        return self._ts_holder["t"]

    def __getattr__(self, item):
        return getattr(time, item)


@contextmanager
def _frozen_clock():
    holder = {"t": 0.0}
    fake_dt = _FrozenDateTime(holder)
    fake_time = _FrozenTime(holder)
    patches = [
        mock.patch.object(_risk_mod, "datetime", fake_dt),
        mock.patch.object(_executor_mod, "time", fake_time),
        mock.patch.object(_executor_mod, "datetime", fake_dt),
    ]
    for p in patches:
        p.start()
    try:
        yield holder
    finally:
        for p in patches:
            p.stop()


# --- tape loader --------------------------------------------------------------

@dataclass
class WindowRow:
    window_ts: int
    btc_open: float
    btc_close: float
    bar_closes: list[float]
    bar_volumes: list[float]
    bar_highs: list[float]
    bar_lows: list[float]
    up_fills: list[tuple[int, float, float]]    # (unix_sec, price, usd)
    down_fills: list[tuple[int, float, float]]
    token_up: str
    token_down: str


def load_tape(path: str) -> list[WindowRow]:
    if path.endswith(".parquet"):
        try:
            import pandas as pd
            df = pd.read_parquet(path)
        except Exception as e:
            raise RuntimeError(
                f"Failed to read parquet {path}: {e}. Install pyarrow or use JSON."
            )
        rows: list[WindowRow] = []
        for _, r in df.iterrows():
            rows.append(WindowRow(
                window_ts=int(r["window_ts"]),
                btc_open=float(r["btc_open"]),
                btc_close=float(r["btc_close"]),
                bar_closes=list(map(float, r["bar_closes"])),
                bar_volumes=list(map(float, r["bar_volumes"])),
                bar_highs=list(map(float, r["bar_highs"])),
                bar_lows=list(map(float, r["bar_lows"])),
                up_fills=[tuple(x) for x in r["up_fills"]],
                down_fills=[tuple(x) for x in r["down_fills"]],
                token_up=str(r["token_up"]),
                token_down=str(r["token_down"]),
            ))
        return rows
    elif path.endswith((".json", ".jsonl")):
        with open(path) as f:
            data = json.load(f)
        rows: list[WindowRow] = []
        for r in data:
            rows.append(WindowRow(
                window_ts=int(r["window_ts"]),
                btc_open=float(r["btc_open"]),
                btc_close=float(r["btc_close"]),
                bar_closes=list(map(float, r["bar_closes"])),
                bar_volumes=list(map(float, r["bar_volumes"])),
                bar_highs=list(map(float, r["bar_highs"])),
                bar_lows=list(map(float, r["bar_lows"])),
                up_fills=[tuple(x) for x in r["up_fills"]],
                down_fills=[tuple(x) for x in r["down_fills"]],
                token_up=str(r["token_up"]),
                token_down=str(r["token_down"]),
            ))
        return rows
    raise ValueError(f"Unsupported tape format: {path}")


# --- price-state builder ------------------------------------------------------

def build_state(row: WindowRow, minutes_completed: int) -> PriceState:
    n = max(1, min(5, minutes_completed))
    closes = row.bar_closes[:n]
    highs = row.bar_highs[:n]
    lows = row.bar_lows[:n]
    vols = row.bar_volumes[:n]
    open_px = row.btc_open
    current = closes[-1]

    vw_num = sum((highs[i] + lows[i] + closes[i]) / 3.0 * vols[i] for i in range(n))
    vw_den = sum(vols[i] for i in range(n))
    vwap = vw_num / vw_den if vw_den > 0 else current

    momentum_per_sec = (closes[-1] - closes[-2]) / 60.0 if n >= 2 else 0.0
    delta = (current - open_px) / open_px if open_px else 0.0
    rv = _realised_vol_from_closes(closes)

    st = PriceState()
    st.current_price = current
    st.window_open_price = open_px
    st.vwap = vwap
    st.momentum = momentum_per_sec
    st.delta_from_open = delta
    st.delta_from_open_abs = abs(delta)
    st.volume_imbalance = 0.0  # kline limitation
    st.tick_count = n * 60
    setattr(st, "realised_vol", rv)
    return st


def _realised_vol_from_closes(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


# --- ask proxy ---------------------------------------------------------------

def ask_proxy_at(fills: list[tuple[int, float, float]], t: int,
                 freshness_sec: int = ASK_FRESHNESS_SEC) -> tuple[float, bool]:
    """Last on-chain trade price in [t - freshness_sec, t]. Returns (ask, fresh)."""
    if not fills:
        return 0.0, False
    last = 0.0
    for ts, price, _usd in fills:
        if ts > t:
            break
        if t - ts <= freshness_sec:
            last = price
    return last, last > 0


# --- per-tick stream emit (used by param_sweep) ------------------------------

@dataclass
class TickRecord:
    window_ts: int
    t: int
    confidence: float
    direction: str
    window_delta: float
    ask_up: float
    ask_down: float
    fresh_up: bool
    fresh_down: bool
    btc_close: float
    minutes_completed: int


# --- replay -------------------------------------------------------------------

@dataclass
class TradeRow:
    window_ts: int
    time_iso: str
    direction: str
    confidence: float
    entry_price: float
    size_usdc: float
    shares: float
    outcome: str        # WIN | LOSS | UNFILLED | SKIPPED
    pnl: float
    delta_pct: float
    ask_used: float
    fillable: bool


def replay(
    tape: list[WindowRow],
    starting_balance: Optional[float] = None,
    fee_rate: float = DEFAULT_FEE_RATE,
    fillability_required: bool = True,
    emit_ticks: bool = False,
    log_every: int = 5000,
) -> tuple[list[TradeRow], list[TickRecord]]:

    global ENTRY_WINDOW_START, ENTRY_WINDOW_END
    ENTRY_WINDOW_START = CONFIG.entry_window_start
    ENTRY_WINDOW_END = CONFIG.entry_window_end

    bal = starting_balance if starting_balance is not None else CONFIG.starting_capital
    rm = RiskManager(starting_balance=bal)
    ex = PaperExecutor()

    trades: list[TradeRow] = []
    ticks: list[TickRecord] = []

    with _frozen_clock() as clock:
        for i, row in enumerate(tape):
            if log_every and i % log_every == 0 and i:
                logger.info(f"window {i}/{len(tape)} bal=${rm.state.current_balance:.2f}")

            clock["t"] = float(row.window_ts)

            window_end_ts = row.window_ts + 300
            consecutive_fails = 0

            tick_seconds_remaining = list(range(
                ENTRY_WINDOW_START, ENTRY_WINDOW_END, -REPRICE_INTERVAL
            ))
            for sec_remaining in tick_seconds_remaining:
                t = window_end_ts - sec_remaining
                clock["t"] = float(t)
                # `bar_closes[i]` is the close at end of minute `i+1`, i.e. at
                # `window_ts + (i+1)*60`. Only minutes that have FULLY closed by
                # `t` are observable. During the entry window (T-45..T-8) this
                # is always 4; we never read the resolution close at minute 5.
                minutes_completed = max(1, min(5, (t - row.window_ts) // 60))

                state = build_state(row, minutes_completed)
                sig = compute_signal(state, window_end_ts, now=t)
                if sig is None:
                    continue

                ask_up, fresh_up = ask_proxy_at(row.up_fills, t)
                ask_down, fresh_down = ask_proxy_at(row.down_fills, t)

                if emit_ticks:
                    ticks.append(TickRecord(
                        window_ts=row.window_ts, t=t,
                        confidence=sig.confidence, direction=sig.direction,
                        window_delta=sig.window_delta,
                        ask_up=ask_up, ask_down=ask_down,
                        fresh_up=fresh_up, fresh_down=fresh_down,
                        btc_close=row.btc_close,
                        minutes_completed=minutes_completed,
                    ))

                can, _reason = rm.can_trade()
                if not can:
                    continue

                if CONFIG.trading_hours_block and ex.pending_trade(row.window_ts) is None:
                    hr = datetime.fromtimestamp(t, timezone.utc).hour
                    if hr in CONFIG.trading_hours_block:
                        continue

                gate_ok = gate_vs_market(sig, ask_up=ask_up, ask_down=ask_down)
                active = ex.pending_trade(row.window_ts)

                if not gate_ok:
                    if active is not None:
                        consecutive_fails += 1
                        if consecutive_fails >= CANCEL_FAIL_THRESHOLD:
                            ex.cancel_pending_if_unfilled(row.window_ts)
                            rm.on_trade_resolved(
                                won=False,
                                payout_usdc=active.size_usdc,
                                pnl=0.0,
                                skipped=True,
                            )
                            consecutive_fails = 0
                    continue

                consecutive_fails = 0
                side_ask = ask_up if sig.direction == "UP" else ask_down
                target_price = round(max(0.02, side_ask - 0.01), 2)
                side_token = row.token_up if sig.direction == "UP" else row.token_down

                if active is not None and active.direction != sig.direction:
                    ex.cancel_pending_if_unfilled(row.window_ts)
                    rm.on_trade_resolved(
                        won=False, payout_usdc=active.size_usdc, pnl=0.0, skipped=True,
                    )
                    continue

                size = rm.calculate_position_size(sig.confidence, target_price)
                if size <= 0:
                    continue

                if active is None:
                    trade = ex.place_order(
                        window_ts=row.window_ts,
                        direction=sig.direction,
                        confidence=sig.confidence,
                        entry_price=target_price,
                        size_usdc=size,
                        token_id=side_token,
                        btc_open=row.btc_open,
                        seconds_to_close=sec_remaining,
                    )
                    if trade is not None:
                        rm.on_trade_placed(trade.size_usdc)
                elif abs(active.entry_price - target_price) >= 0.01:
                    ex.reprice(row.window_ts, new_price=target_price)

            # --- resolution ---
            active = ex.pending_trade(row.window_ts)
            if active is None:
                continue

            fillable = _was_fillable(row, active.direction, active.entry_price)
            if fillability_required and not fillable:
                ex.cancel_pending_if_unfilled(row.window_ts)
                rm.on_trade_resolved(
                    won=False, payout_usdc=active.size_usdc, pnl=0.0, skipped=True,
                )
                trades.append(TradeRow(
                    window_ts=row.window_ts,
                    time_iso=datetime.fromtimestamp(row.window_ts, timezone.utc).isoformat(),
                    direction=active.direction,
                    confidence=active.confidence,
                    entry_price=active.entry_price,
                    size_usdc=active.size_usdc,
                    shares=active.shares,
                    outcome="UNFILLED",
                    pnl=0.0,
                    delta_pct=(row.btc_close - row.btc_open) / row.btc_open if row.btc_open else 0.0,
                    ask_used=active.entry_price + 0.01,
                    fillable=False,
                ))
                ex.forget_window(row.window_ts)
                continue

            clock["t"] = float(row.window_ts + 300)
            resolved = ex.resolve_trade(row.window_ts, btc_close=row.btc_close)
            if resolved is None:
                continue

            if fee_rate > 0 and resolved.outcome == "WIN":
                fee = resolved.payout * fee_rate
                resolved.payout = round(resolved.payout - fee, 6)
                resolved.pnl = round(resolved.pnl - fee, 6)

            won = resolved.outcome == "WIN"
            rm.on_trade_resolved(
                won=won,
                payout_usdc=resolved.payout or 0.0,
                pnl=resolved.pnl or 0.0,
                skipped=False,
            )

            trades.append(TradeRow(
                window_ts=row.window_ts,
                time_iso=resolved.time_iso,
                direction=resolved.direction,
                confidence=resolved.confidence,
                entry_price=resolved.entry_price,
                size_usdc=resolved.size_usdc,
                shares=resolved.shares,
                outcome=resolved.outcome or "",
                pnl=float(resolved.pnl or 0.0),
                delta_pct=float(resolved.delta_pct or 0.0),
                ask_used=resolved.entry_price + 0.01,
                fillable=True,
            ))
            ex.forget_window(row.window_ts)

    return trades, ticks


def _was_fillable(row: WindowRow, direction: str, our_limit: float) -> bool:
    """Was there a taker fill at-or-below our limit during the entry window?"""
    fills = row.up_fills if direction == "UP" else row.down_fills
    if not fills:
        return False
    window_start = row.window_ts + 300 - CONFIG.entry_window_start
    window_end = row.window_ts + 300 - CONFIG.entry_window_end
    for ts, price, _usd in fills:
        if window_start <= ts <= window_end and price <= our_limit + 0.001:
            return True
    return False


# --- summary -----------------------------------------------------------------

def _bucket_conf(c: float) -> str:
    if c < 0.55: return "<55"
    if c < 0.60: return "55-60"
    if c < 0.65: return "60-65"
    if c < 0.70: return "65-70"
    if c < 0.80: return "70-80"
    if c < 0.90: return "80-90"
    return "90+"


def _bucket_hour(ts_iso: str) -> str:
    return ts_iso[11:13]


def _bucket_delta(d: float) -> str:
    a = abs(d) * 100
    if a < 0.01: return "<0.01%"
    if a < 0.05: return "0.01-0.05%"
    if a < 0.10: return "0.05-0.10%"
    if a < 0.25: return "0.10-0.25%"
    if a < 0.50: return "0.25-0.50%"
    return ">0.50%"


def _stats_by(trades: list[TradeRow], key_fn) -> dict:
    from collections import defaultdict
    g: dict[str, list[TradeRow]] = defaultdict(list)
    for t in trades:
        if t.outcome in ("WIN", "LOSS"):
            g[key_fn(t)].append(t)
    out = {}
    for k, ts in sorted(g.items()):
        w = sum(1 for t in ts if t.outcome == "WIN")
        p = sum(t.pnl for t in ts)
        out[k] = {
            "n": len(ts),
            "wins": w,
            "win_rate": round(w / len(ts), 3) if ts else 0,
            "total_pnl": round(p, 4),
        }
    return out


def summarize(trades: list[TradeRow], starting_balance: float) -> dict:
    settled = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    n = len(settled)
    if n == 0:
        return {"trades_settled": 0, "trades_unfilled":
                sum(1 for t in trades if t.outcome == "UNFILLED")}

    wins = sum(1 for t in settled if t.outcome == "WIN")
    total_pnl = sum(t.pnl for t in settled)
    capital_at_risk = sum(t.size_usdc for t in settled)

    streak = max_streak = 0
    for t in settled:
        if t.outcome == "LOSS":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    from collections import defaultdict
    by_day: dict[str, float] = defaultdict(float)
    for t in settled:
        day = t.time_iso[:10]
        by_day[day] += t.pnl
    daily_pnl = [by_day[d] for d in sorted(by_day)]
    if len(daily_pnl) >= 2:
        mean_d = sum(daily_pnl) / len(daily_pnl)
        var_d = sum((x - mean_d) ** 2 for x in daily_pnl) / len(daily_pnl)
        sd = math.sqrt(var_d)
        sharpe_daily = mean_d / sd if sd > 0 else 0.0
    else:
        sharpe_daily = 0.0

    eq = [starting_balance]
    for t in settled:
        eq.append(eq[-1] + t.pnl)
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd

    return {
        "trades_settled": n,
        "trades_unfilled": sum(1 for t in trades if t.outcome == "UNFILLED"),
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4),
        "total_pnl": round(total_pnl, 4),
        "capital_at_risk": round(capital_at_risk, 4),
        "roi": round(total_pnl / capital_at_risk, 4) if capital_at_risk else 0.0,
        "avg_pnl_per_trade": round(total_pnl / n, 4),
        "sharpe_daily": round(sharpe_daily, 4),
        "max_drawdown": round(max_dd, 4),
        "longest_loss_streak": max_streak,
        "by_confidence": _stats_by(settled, lambda t: _bucket_conf(t.confidence)),
        "by_hour_utc":   _stats_by(settled, lambda t: _bucket_hour(t.time_iso)),
        "by_delta":      _stats_by(settled, lambda t: _bucket_delta(t.delta_pct)),
        "by_direction":  _stats_by(settled, lambda t: t.direction),
    }


def config_hash() -> str:
    keys = [
        "min_confidence", "min_edge", "min_delta_pct", "trading_hours_block",
        "entry_window_start", "entry_window_end", "kelly_fraction",
        "max_bet_size", "min_bet_size",
        "starting_capital", "signal_variant", "min_edge_up", "min_edge_down",
        "vol_regime_min", "vol_regime_max",
    ]
    parts = []
    for k in keys:
        v = getattr(CONFIG, k, None)
        if isinstance(v, frozenset):
            v = sorted(v)
        parts.append(f"{k}={v}")
    blob = ";".join(parts).encode()
    return hashlib.sha1(blob).hexdigest()[:10]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default="data/btc_5m_tape.parquet")
    ap.add_argument("--variant", default=None,
                    help="Override CONFIG.signal_variant for this run")
    ap.add_argument("--starting-balance", type=float, default=None)
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE_RATE)
    ap.add_argument("--no-fillability", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--ticks-out", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.variant is not None:
        object.__setattr__(CONFIG, "signal_variant", args.variant)
        CONFIG.validate()

    if not os.path.exists(args.tape):
        logger.error(f"tape not found: {args.tape}")
        logger.error("Build it via `python scripts/build_market_tape.py`")
        return 2

    tape = load_tape(args.tape)
    if args.limit:
        tape = tape[:args.limit]
    logger.info(f"loaded {len(tape)} windows from {args.tape}")

    trades, ticks = replay(
        tape,
        starting_balance=args.starting_balance,
        fee_rate=args.fee,
        fillability_required=not args.no_fillability,
        emit_ticks=args.ticks_out is not None,
    )
    bal = args.starting_balance if args.starting_balance is not None else CONFIG.starting_capital
    summary = summarize(trades, starting_balance=bal)

    out_path = args.out or f"logs/bt_v2_{config_hash()}.json"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    payload = {
        "config_hash": config_hash(),
        "signal_variant": CONFIG.signal_variant,
        "min_confidence": CONFIG.min_confidence,
        "min_edge": CONFIG.min_edge,
        "min_delta_pct": CONFIG.min_delta_pct,
        "trading_hours_block": sorted(CONFIG.trading_hours_block),
        "fee_rate": args.fee,
        "fillability_required": not args.no_fillability,
        "summary": summary,
        "trades": [asdict(t) for t in trades],
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    logger.info(f"wrote {out_path}")

    if args.ticks_out:
        os.makedirs(os.path.dirname(args.ticks_out) or ".", exist_ok=True)
        try:
            import pandas as pd
            pd.DataFrame([asdict(t) for t in ticks]).to_parquet(args.ticks_out, index=False)
        except Exception:
            with open(args.ticks_out + ".json", "w") as f:
                json.dump([asdict(t) for t in ticks], f)
        logger.info(f"wrote {args.ticks_out} ({len(ticks)} ticks)")

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
