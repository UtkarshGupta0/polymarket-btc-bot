"""Counterfactually apply the 4 new gates to historical paper trades.

Reads logs/trades_2026*.json, prints three analyses to stdout.
Spec: docs/superpowers/specs/2026-04-24-gate-validation-design.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_trades(log_dir: Path) -> list[dict]:
    """Load WIN/LOSS trades from all logs/trades_2026*.json files."""
    out: list[dict] = []
    for path in sorted(log_dir.glob("trades_2026*.json")):
        data = json.loads(path.read_text())
        for t in data.get("trades", []):
            if t.get("outcome") in {"WIN", "LOSS"}:
                out.append(t)
    return out


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trades = load_trades(repo_root / "logs")
    print(f"Loaded {len(trades)} resolved trades")


if __name__ == "__main__":
    main()
