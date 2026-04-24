"""Unit test for PriceFeed._compute_trend_slope via _on_tick."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from price_feed import PriceFeed


def _tick(ts_ms: int, price: float) -> dict:
    return {"p": str(price), "q": "0.01", "T": ts_ms, "m": False}


def _fresh_feed(open_px: float, t0_ms: int) -> PriceFeed:
    feed = PriceFeed()
    feed.set_window_open(open_px, t0_ms // 1000)
    return feed


def test_positive_ramp() -> None:
    """+$1/s ramp over 60s -> positive slope ~ 1.0 / open_px per second."""
    open_px = 80000.0
    t0 = 1_700_000_000_000
    feed = _fresh_feed(open_px, t0)
    for i in range(60):
        feed._on_tick(_tick(t0 + i * 1000, open_px + i))
    slope = feed.state.trend_slope_2m
    expected = 1.0 / open_px
    assert slope > 0, f"positive ramp must give positive slope, got {slope}"
    # Allow 10% tolerance (least-squares on clean ramp is near-exact)
    assert abs(slope - expected) / expected < 0.1, \
        f"slope={slope:.3e} expected~{expected:.3e}"
    print(f"PASS positive_ramp slope={slope:.3e} expected~{expected:.3e}")


def test_flat_stream() -> None:
    """Constant price over 60s -> slope == 0."""
    open_px = 80000.0
    t0 = 1_700_000_000_000
    feed = _fresh_feed(open_px, t0)
    for i in range(60):
        feed._on_tick(_tick(t0 + i * 1000, open_px))
    slope = feed.state.trend_slope_2m
    assert slope == 0.0, f"flat stream must give zero slope, got {slope}"
    print(f"PASS flat_stream slope={slope}")


def test_insufficient_buckets() -> None:
    """<30 unique 1s-buckets -> guard returns 0.0."""
    open_px = 80000.0
    t0 = 1_700_000_000_000
    feed = _fresh_feed(open_px, t0)
    # 20 ticks spread across 20 seconds
    for i in range(20):
        feed._on_tick(_tick(t0 + i * 1000, open_px + i))
    slope = feed.state.trend_slope_2m
    assert slope == 0.0, f"<30 buckets must return 0, got {slope}"
    print(f"PASS insufficient_buckets slope={slope}")


def test_negative_ramp() -> None:
    """-$1/s ramp -> negative slope."""
    open_px = 80000.0
    t0 = 1_700_000_000_000
    feed = _fresh_feed(open_px, t0)
    for i in range(60):
        feed._on_tick(_tick(t0 + i * 1000, open_px - i))
    slope = feed.state.trend_slope_2m
    assert slope < 0, f"negative ramp must give negative slope, got {slope}"
    print(f"PASS negative_ramp slope={slope:.3e}")


def main() -> None:
    test_positive_ramp()
    test_flat_stream()
    test_insufficient_buckets()
    test_negative_ramp()
    print("\nAll trend-slope tests PASS ✓")


if __name__ == "__main__":
    main()
