"""Split parameter sweep over the backtest tape.

D1 (signal-invariant inner loop):
  Replay the tape ONCE with emit_ticks=True for the current signal config.
  Then iterate 200 (min_conf, min_edge, min_delta, hours_block) combos as cheap
  pandas filters over the tick cache + a sequential drawdown re-simulation.

D2 (signal-varying outer loop):
  For each (weights, time_boost) combo, mutate signal_engine constants in place,
  re-run D1, restore.

Score:
  primary  = ROI per $ at risk
  secondary= Sharpe (daily P&L)
  tiebreak = worst 7-day rolling P&L

Output: logs/sweep_<timestamp>.json (top 20 + full leaderboard CSV).
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtester_v2  # noqa: E402
import signal_engine  # noqa: E402
from config import CONFIG  # noqa: E402

logger = logging.getLogger("param_sweep")


MIN_CONF = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
MIN_EDGE = [0.00, 0.01, 0.02, 0.03, 0.05, 0.10]
MIN_DELTA = [0.0]

WEIGHT_VARIANTS = [
    ("default", 0.40, 0.25, 0.20, 0.15),
    ("delta_heavy", 0.55, 0.20, 0.15, 0.10),
    ("balanced", 0.30, 0.30, 0.20, 0.20),
]

BOOST_VARIANTS = [
    ("flat", 1.00, 1.00),
    ("classic", 1.08, 1.15),
    ("light", 1.05, 1.10),
    ("aggressive", 1.10, 1.20),
]


@dataclass
class FilterConfig:
    min_confidence: float
    min_edge: float
    min_delta_pct: float
    trading_hours_block: frozenset[int]
    weight_label: str
    boost_label: str


@dataclass
class FilterResult:
    cfg: FilterConfig
    n_trades: int
    wins: int
    losses: int
    roi: float
    total_pnl: float
    capital_at_risk: float
    sharpe_daily: float
    max_drawdown: float
    worst_7d: float


def _bottom_hours_by_roi(ticks: pd.DataFrame, n: int = 3) -> frozenset[int]:
    df = ticks.copy()
    df["hour"] = ((df["t"] // 3600) % 24).astype(int)
    side_ask = df.apply(
        lambda r: r["ask_up"] if r["direction"] == "UP" else r["ask_down"], axis=1
    )
    df["edge"] = df["confidence"] - side_ask
    g = df.groupby("hour")["edge"].mean()
    worst = g.sort_values().head(n).index.tolist()
    return frozenset(int(h) for h in worst)


def _filter_first_qualifying_tick(ticks: pd.DataFrame, cfg: FilterConfig
                                  ) -> pd.DataFrame:
    df = ticks
    if df.empty:
        return df.iloc[0:0]
    side_ask = df.apply(
        lambda r: r["ask_up"] if r["direction"] == "UP" else r["ask_down"], axis=1
    )
    edge = df["confidence"] - side_ask
    mask = (
        (df["confidence"] >= cfg.min_confidence)
        & (edge >= cfg.min_edge)
        & (df["window_delta"].abs() >= cfg.min_delta_pct)
        & (side_ask > 0)
    )
    if cfg.trading_hours_block:
        hours = ((df["t"] // 3600) % 24).astype(int)
        mask &= ~hours.isin(list(cfg.trading_hours_block))

    qualifying = df.loc[mask].copy()
    if qualifying.empty:
        return qualifying
    qualifying["side_ask"] = side_ask.loc[qualifying.index]
    qualifying.sort_values(["window_ts", "t"], inplace=True)
    return qualifying.drop_duplicates(subset=["window_ts"], keep="first")


def _join_outcomes(picks: pd.DataFrame, tape_outcomes: pd.DataFrame) -> pd.DataFrame:
    if picks.empty:
        return picks
    j = picks.merge(tape_outcomes, on="window_ts", how="left", validate="1:1")
    went_up = j["btc_close_actual"] >= j["btc_open"]
    j["won"] = ((j["direction"] == "UP") & went_up) | (
        (j["direction"] == "DOWN") & (~went_up)
    )
    j["entry_price"] = (j["side_ask"] - 0.01).round(2).clip(lower=0.02)
    return j


def _simulate_pnl(picks: pd.DataFrame, starting_balance: float
                  ) -> tuple[float, float, list[float], int, int]:
    if picks.empty:
        return 0.0, 0.0, [], 0, 0
    from datetime import datetime, timezone

    picks = picks.sort_values("window_ts")
    bal = starting_balance
    daily_pnl = 0.0
    streak = 0
    halted_today = False
    cur_day = None
    daily_pnls: list[float] = []
    total_pnl = 0.0
    car = 0.0
    wins = losses = 0
    total_trades = 0

    for _, r in picks.iterrows():
        ts = int(r["window_ts"])
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        if day != cur_day:
            if cur_day is not None:
                daily_pnls.append(daily_pnl)
            cur_day = day
            daily_pnl = 0.0
            streak = 0
            halted_today = False

        if halted_today:
            continue
        if streak >= CONFIG.max_consecutive_losses:
            halted_today = True
            continue
        if bal <= CONFIG.min_reserve:
            halted_today = True
            continue

        ep = float(r["entry_price"])
        conf = float(r["confidence"])
        b = (1.0 / ep) - 1.0
        if b <= 0:
            continue
        kelly_pct = max(0.0, (b * conf - (1 - conf)) / b)
        if kelly_pct <= 0:
            continue
        available = max(0.0, bal - CONFIG.min_reserve)
        size = min(CONFIG.max_bet_size, available * kelly_pct * CONFIG.kelly_fraction)
        if size < CONFIG.min_bet_size:
            continue
        size = round(size, 2)

        shares = round(size / ep, 2)
        if shares < 5.0:
            continue

        total_trades += 1
        car += size
        won = bool(r["won"])
        if won:
            payout = shares * 1.0
            pnl = payout - size
            wins += 1
            streak = 0
        else:
            payout = 0.0
            pnl = -size
            losses += 1
            streak += 1
        bal += pnl
        daily_pnl += pnl
        total_pnl += pnl

        if daily_pnl <= -CONFIG.max_daily_drawdown:
            halted_today = True

    if cur_day is not None:
        daily_pnls.append(daily_pnl)
    return total_pnl, car, daily_pnls, wins, losses


def _sharpe(daily_pnls: list[float]) -> float:
    if len(daily_pnls) < 2:
        return 0.0
    m = sum(daily_pnls) / len(daily_pnls)
    v = sum((x - m) ** 2 for x in daily_pnls) / len(daily_pnls)
    s = math.sqrt(v)
    return m / s if s > 0 else 0.0


def _max_dd(daily_pnls: list[float]) -> float:
    eq = [0.0]
    for d in daily_pnls:
        eq.append(eq[-1] + d)
    peak = eq[0]
    mx = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = peak - v
        mx = max(mx, dd)
    return mx


def _worst_7d(daily_pnls: list[float]) -> float:
    if len(daily_pnls) < 7:
        return sum(daily_pnls)
    worst = float("inf")
    for i in range(len(daily_pnls) - 6):
        s = sum(daily_pnls[i : i + 7])
        if s < worst:
            worst = s
    return worst if worst != float("inf") else 0.0


def evaluate_filter(ticks: pd.DataFrame, tape_outcomes: pd.DataFrame,
                    cfg: FilterConfig, starting_balance: float) -> FilterResult:
    picks = _filter_first_qualifying_tick(ticks, cfg)
    picks = _join_outcomes(picks, tape_outcomes)
    if picks.empty:
        return FilterResult(cfg=cfg, n_trades=0, wins=0, losses=0, roi=0.0,
                            total_pnl=0.0, capital_at_risk=0.0,
                            sharpe_daily=0.0, max_drawdown=0.0, worst_7d=0.0)
    total_pnl, car, daily, w, l = _simulate_pnl(picks, starting_balance)
    return FilterResult(
        cfg=cfg, n_trades=w + l, wins=w, losses=l,
        roi=(total_pnl / car) if car else 0.0,
        total_pnl=total_pnl, capital_at_risk=car,
        sharpe_daily=_sharpe(daily),
        max_drawdown=_max_dd(daily),
        worst_7d=_worst_7d(daily),
    )


def _set_signal_constants(weights: tuple[float, float, float, float],
                          boost: tuple[float, float]) -> dict:
    prev = {
        "W_DELTA": signal_engine.W_DELTA,
        "W_MOMENTUM": signal_engine.W_MOMENTUM,
        "W_VOLUME": signal_engine.W_VOLUME,
        "W_VWAP": signal_engine.W_VWAP,
        "BOOST_30": getattr(signal_engine, "BOOST_30", 1.08),
        "BOOST_15": getattr(signal_engine, "BOOST_15", 1.15),
    }
    signal_engine.W_DELTA, signal_engine.W_MOMENTUM, \
        signal_engine.W_VOLUME, signal_engine.W_VWAP = weights
    signal_engine.BOOST_30, signal_engine.BOOST_15 = boost
    return prev


def _restore_signal_constants(prev: dict) -> None:
    signal_engine.W_DELTA = prev["W_DELTA"]
    signal_engine.W_MOMENTUM = prev["W_MOMENTUM"]
    signal_engine.W_VOLUME = prev["W_VOLUME"]
    signal_engine.W_VWAP = prev["W_VWAP"]
    if "BOOST_30" in prev:
        signal_engine.BOOST_30 = prev["BOOST_30"]
    if "BOOST_15" in prev:
        signal_engine.BOOST_15 = prev["BOOST_15"]


def replay_for_ticks(tape_path: str, starting_balance: Optional[float]
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    tape = backtester_v2.load_tape(tape_path)
    _, ticks = backtester_v2.replay(
        tape, starting_balance=starting_balance,
        fee_rate=0.0, fillability_required=False,
        emit_ticks=True, log_every=0,
    )
    ticks_df = pd.DataFrame([asdict(t) for t in ticks])
    outcomes = pd.DataFrame([
        {"window_ts": w.window_ts, "btc_open": w.btc_open,
         "btc_close_actual": w.btc_close}
        for w in tape
    ])
    return ticks_df, outcomes


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default="data/btc_5m_tape.parquet")
    ap.add_argument("--starting-balance", type=float, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--limit-d2", type=int, default=None)
    args = ap.parse_args()

    bal = args.starting_balance if args.starting_balance is not None else CONFIG.starting_capital

    if not os.path.exists(args.tape):
        logger.error(f"tape not found: {args.tape}")
        return 2

    leaderboard: list[FilterResult] = []
    d2_combos = list(itertools.product(WEIGHT_VARIANTS, BOOST_VARIANTS))
    if args.limit_d2:
        d2_combos = d2_combos[: args.limit_d2]

    for d2_idx, (wv, bv) in enumerate(d2_combos):
        wlabel, *weights = wv
        blabel, b30, b15 = bv
        logger.info(f"[D2 {d2_idx + 1}/{len(d2_combos)}] weights={wlabel} boost={blabel}")
        prev = _set_signal_constants(tuple(weights), (b30, b15))
        try:
            ticks_df, outcomes_df = replay_for_ticks(args.tape, bal)
        finally:
            _restore_signal_constants(prev)
        if ticks_df.empty:
            logger.warning("  D2 combo emitted no ticks; skipping")
            continue

        bottom = _bottom_hours_by_roi(ticks_df, n=3)
        hours_options = [frozenset(), bottom]

        for mc, me, md, hb in itertools.product(
            MIN_CONF, MIN_EDGE, MIN_DELTA, hours_options
        ):
            cfg = FilterConfig(
                min_confidence=mc, min_edge=me, min_delta_pct=md,
                trading_hours_block=hb,
                weight_label=wlabel, boost_label=blabel,
            )
            object.__setattr__(CONFIG, "min_confidence", mc)
            object.__setattr__(CONFIG, "min_edge", me)
            object.__setattr__(CONFIG, "min_delta_pct", md)
            object.__setattr__(CONFIG, "trading_hours_block", hb)
            try:
                CONFIG.validate()
            except AssertionError:
                continue
            res = evaluate_filter(ticks_df, outcomes_df, cfg, bal)
            leaderboard.append(res)

        logger.info(
            f"  evaluated {len(MIN_CONF) * len(MIN_EDGE) * len(MIN_DELTA) * len(hours_options)} configs"
        )

    leaderboard.sort(
        key=lambda r: (-r.roi, -r.sharpe_daily, -r.worst_7d)
    )

    out = args.out or f"logs/sweep_{int(time.time())}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    def _entry(r: FilterResult) -> dict:
        return {
            "weight_label": r.cfg.weight_label,
            "boost_label": r.cfg.boost_label,
            "min_confidence": r.cfg.min_confidence,
            "min_edge": r.cfg.min_edge,
            "min_delta_pct": r.cfg.min_delta_pct,
            "trading_hours_block": sorted(r.cfg.trading_hours_block),
            "n_trades": r.n_trades,
            "wins": r.wins,
            "losses": r.losses,
            "win_rate": round(r.wins / r.n_trades, 4) if r.n_trades else 0,
            "roi": round(r.roi, 4),
            "total_pnl": round(r.total_pnl, 4),
            "capital_at_risk": round(r.capital_at_risk, 4),
            "sharpe_daily": round(r.sharpe_daily, 4),
            "max_drawdown": round(r.max_drawdown, 4),
            "worst_7d": round(r.worst_7d, 4),
        }

    payload = {
        "evaluated": len(leaderboard),
        "top": [_entry(r) for r in leaderboard[: args.top]],
        "all": [_entry(r) for r in leaderboard],
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"wrote {out}")

    csv_path = Path(out).with_suffix(".csv")
    pd.DataFrame(payload["all"]).to_csv(csv_path, index=False)
    logger.info(f"wrote {csv_path}")

    print("=" * 70)
    print(f"TOP {args.top} CONFIGS")
    print("=" * 70)
    for i, e in enumerate(payload["top"], 1):
        print(f"#{i:>2}  ROI={e['roi']:+.4f}  PnL=${e['total_pnl']:+.2f}  "
              f"WR={e['win_rate']:.3f}  N={e['n_trades']:>4}  "
              f"DD=${e['max_drawdown']:.2f}  Sh={e['sharpe_daily']:+.2f}  "
              f"| w={e['weight_label']} b={e['boost_label']} "
              f"conf>={e['min_confidence']} edge>={e['min_edge']} "
              f"delta>={e['min_delta_pct']} hrs={e['trading_hours_block']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
