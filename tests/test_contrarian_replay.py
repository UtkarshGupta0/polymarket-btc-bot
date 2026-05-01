"""Integration test: contrarian variant in backtester_v2.replay."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import backtester_v2 as bt2  # noqa: E402
from config import CONFIG  # noqa: E402


def _set_cfg(**kwargs) -> dict:
    prev = {k: getattr(CONFIG, k) for k in kwargs}
    for k, v in kwargs.items():
        object.__setattr__(CONFIG, k, v)
    CONFIG.validate()
    return prev


def _restore_cfg(prev: dict) -> None:
    for k, v in prev.items():
        object.__setattr__(CONFIG, k, v)


def _mk_window(window_ts, btc_open, btc_close, up_fills, down_fills,
               token_up="0xUP", token_down="0xDOWN") -> bt2.WindowRow:
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


def test_contrarian_places_underdog_trade_when_favourite_ask_high():
    """When ask_up >= threshold during entry window, a DOWN trade is placed at ask_down."""
    prev = _set_cfg(
        signal_variant="contrarian",
        contrarian_ask_threshold=0.90,
        min_confidence=0.50, min_edge=0.01, min_delta_pct=0.0,
        trading_hours_block=frozenset(),
        starting_capital=30.0,
        max_consecutive_losses=10, max_daily_drawdown=100.0,
        min_reserve=5.0, kelly_fraction=0.25,
        min_bet_size=1.0, max_bet_size=5.0,
        min_edge_up=0.01, min_edge_down=0.01,
    )
    try:
        ws = 1735689600  # 2025-01-01 00:00:00 UTC
        # btc went DOWN by $400 over the window -> DOWN wins -> our underdog (DOWN) wins.
        win = _mk_window(
            window_ts=ws, btc_open=80000.0, btc_close=79600.0,
            up_fills=[(ws + 256, 0.92, 5.0), (ws + 280, 0.92, 5.0)],
            down_fills=[(ws + 256, 0.08, 5.0), (ws + 280, 0.08, 5.0)],
        )
        trades, _ = bt2.replay([win], starting_balance=30.0,
                               fee_rate=0.0, fillability_required=False)
        assert len(trades) == 1
        tr = trades[0]
        assert tr.direction == "DOWN"
        assert abs(tr.entry_price - 0.08) < 1e-9
        assert tr.outcome == "WIN"
        assert tr.pnl > 0
    finally:
        _restore_cfg(prev)
