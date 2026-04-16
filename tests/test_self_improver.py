"""Exercise self_improver.build_summary with synthetic trades."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from self_improver import build_summary, _bucket_conf, _bucket_delta, _bucket_hour


def synth_trades(n: int = 120, seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        conf = rng.uniform(0.55, 0.95)
        # higher conf => more likely to win (50% + (conf-0.5) * 0.6)
        p_win = 0.5 + (conf - 0.5) * 0.6
        won = rng.random() < p_win
        entry = round(rng.uniform(0.88, 0.95), 2)
        size = rng.uniform(1, 5)
        shares = round(size / entry, 2)
        pnl = round((shares - size) if won else -size, 2)
        hour = rng.randint(0, 23)
        out.append({
            "window_ts": 1000 + i,
            "time": f"2026-04-{8+i//288:02d}T{hour:02d}:03:00+00:00",
            "time_iso": f"2026-04-{8+i//288:02d}T{hour:02d}:03:00+00:00",
            "direction": rng.choice(["UP", "DOWN"]),
            "confidence": conf,
            "entry_price": entry,
            "size_usdc": size,
            "shares": shares,
            "outcome": "WIN" if won else "LOSS",
            "pnl": pnl,
            "delta_pct": rng.uniform(-0.003, 0.003),
            "seconds_to_close_at_entry": rng.uniform(8, 45),
        })
    return out


def main() -> None:
    trades = synth_trades(n=150)
    s = build_summary(trades)
    print(json.dumps(s, indent=2))

    # Sanity
    assert s["trades_total"] == 150
    assert sum(g["n"] for g in s["by_confidence"].values()) == 150
    assert set(s["by_direction"].keys()) <= {"UP", "DOWN"}
    # Higher-confidence bucket should win-rate >= lower-conf bucket on average for synthetic
    bc = s["by_confidence"]
    if "60-70" in bc and "90+" in bc and bc["60-70"]["n"] >= 5 and bc["90+"]["n"] >= 5:
        assert bc["90+"]["win_rate"] >= bc["60-70"]["win_rate"] - 0.15
    print("\nPASS ✓ build_summary sanity")

    # Bucket helpers
    assert _bucket_conf(0.55) == "<60"
    assert _bucket_conf(0.65) == "60-70"
    assert _bucket_conf(0.95) == "90+"
    assert _bucket_delta(0.0001) == "0.01-0.05%"
    assert _bucket_hour("2026-04-14T12:03:00+00:00") == "12"
    print("PASS ✓ bucket helpers")


if __name__ == "__main__":
    main()
