"""Test backtester_v2 replay + FrozenClock shim.

Covers:
  * ask_proxy_at last-fill-within-freshness selection
  * Single-window winning UP trade
  * Fillability filter behaviour
  * Multi-day case crossing UTC midnight to exercise the frozen-clock
    daily-reset path (risk_manager.datetime patched + executor.time/datetime
    patched).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import backtester_v2 as bt2  # noqa: E402
from config import CONFIG  # noqa: E402


def _mk_window(window_ts: int, btc_open: float, btc_close: float,
               up_fills: list[tuple[int, float, float]],
               down_fills: list[tuple[int, float, float]],
               token_up: str = "0xUP", token_down: str = "0xDOWN"
               ) -> bt2.WindowRow:
    closes = [btc_open]
    step = (btc_close - btc_open) / 4 if btc_close != btc_open else 0
    for i in range(4):
        closes.append(round(btc_open + step * (i + 1), 2))
    return bt2.WindowRow(
        window_ts=window_ts,
        btc_open=btc_open,
        btc_close=btc_close,
        bar_closes=closes,
        bar_volumes=[10.0] * 5,
        bar_highs=[c + 1 for c in closes],
        bar_lows=[c - 1 for c in closes],
        up_fills=up_fills,
        down_fills=down_fills,
        token_up=token_up,
        token_down=token_down,
    )


def _set_cfg(**kwargs) -> dict:
    prev = {k: getattr(CONFIG, k) for k in kwargs}
    for k, v in kwargs.items():
        object.__setattr__(CONFIG, k, v)
    CONFIG.validate()
    return prev


def _restore_cfg(prev: dict) -> None:
    for k, v in prev.items():
        object.__setattr__(CONFIG, k, v)


def test_ask_proxy_at_basic():
    fills = [(100, 0.55, 5.0), (130, 0.62, 8.0), (160, 0.58, 4.0)]
    ask, fresh = bt2.ask_proxy_at(fills, t=140, freshness_sec=30)
    assert fresh
    assert abs(ask - 0.62) < 1e-9
    ask2, fresh2 = bt2.ask_proxy_at(fills, t=200, freshness_sec=30)
    assert not fresh2
    assert ask2 == 0


def test_replay_single_window_winning_trade():
    prev = _set_cfg(
        min_confidence=0.50, min_edge=0.01, min_delta_pct=0.0,
        trading_hours_block=frozenset(),
        starting_capital=30.0,
        max_consecutive_losses=10, max_daily_drawdown=100.0,
        min_reserve=5.0, kelly_fraction=0.25,
        min_bet_size=1.0, max_bet_size=5.0,
        min_edge_up=0.01, min_edge_down=0.01,
        signal_variant="default",
    )
    try:
        ws = 1735689600
        win = _mk_window(
            window_ts=ws, btc_open=80000.0, btc_close=80400.0,
            up_fills=[(ws + 256, 0.45, 10.0), (ws + 280, 0.45, 12.0)],
            down_fills=[],
        )
        trades, _ = bt2.replay([win], starting_balance=30.0,
                               fee_rate=0.0, fillability_required=False)
        assert len(trades) == 1
        tr = trades[0]
        assert tr.direction == "UP"
        assert tr.outcome == "WIN"
        assert tr.pnl > 0
    finally:
        _restore_cfg(prev)


def test_replay_unfillable_when_no_fill_at_or_below_limit():
    prev = _set_cfg(
        min_confidence=0.50, min_edge=0.01, min_delta_pct=0.0,
        trading_hours_block=frozenset(),
        starting_capital=30.0,
        max_consecutive_losses=10, max_daily_drawdown=100.0,
        min_reserve=5.0, kelly_fraction=0.25,
        min_bet_size=1.0, max_bet_size=5.0,
        min_edge_up=0.01, min_edge_down=0.01,
        signal_variant="default",
    )
    try:
        ws = 1735689600
        win = _mk_window(
            window_ts=ws, btc_open=80000.0, btc_close=80400.0,
            up_fills=[(ws + 256, 0.62, 5.0)],
            down_fills=[],
        )
        trades, _ = bt2.replay([win], starting_balance=30.0,
                               fee_rate=0.0, fillability_required=True)
        for o in [t.outcome for t in trades]:
            assert o == "UNFILLED"
    finally:
        _restore_cfg(prev)


def test_frozen_clock_crosses_utc_midnight():
    """Multi-day replay must use frozen replay timestamps for Trade.time_iso."""
    prev = _set_cfg(
        min_confidence=0.50, min_edge=0.01, min_delta_pct=0.0,
        trading_hours_block=frozenset(),
        starting_capital=30.0,
        max_consecutive_losses=10, max_daily_drawdown=100.0,
        min_reserve=5.0, kelly_fraction=0.25,
        min_bet_size=1.0, max_bet_size=5.0,
        min_edge_up=0.01, min_edge_down=0.01,
        signal_variant="default",
    )
    try:
        day1_ws = 1735603200  # 2024-12-31 00:00:00 UTC
        day2_ws = 1735689600  # 2025-01-01 00:00:00 UTC
        windows = [
            _mk_window(day1_ws, 80000.0, 80400.0,
                       up_fills=[(day1_ws + 256, 0.45, 12.0),
                                 (day1_ws + 280, 0.45, 12.0)],
                       down_fills=[]),
            _mk_window(day2_ws, 80000.0, 80400.0,
                       up_fills=[(day2_ws + 256, 0.45, 12.0),
                                 (day2_ws + 280, 0.45, 12.0)],
                       down_fills=[]),
        ]
        trades, _ = bt2.replay(windows, starting_balance=30.0,
                               fee_rate=0.0, fillability_required=False)
        settled = [t for t in trades if t.outcome in ("WIN", "LOSS")]
        assert len(settled) == 2, \
            f"expected 2 settled trades, got {len(settled)}"
        assert settled[0].time_iso.startswith("2024-12-31"), \
            f"day1 leaked: {settled[0].time_iso}"
        assert settled[1].time_iso.startswith("2025-01-01"), \
            f"day2 leaked: {settled[1].time_iso}"
    finally:
        _restore_cfg(prev)


def test_summary_zero_when_no_trades():
    s = bt2.summarize([], starting_balance=30.0)
    assert s["trades_settled"] == 0


if __name__ == "__main__":
    fns = [(k, v) for k, v in dict(globals()).items()
           if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
            failed += 1
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(0 if failed == 0 else 1)
