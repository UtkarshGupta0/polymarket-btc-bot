"""JsonlWriter — daily UTC roll-over + gzip integrity."""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymarket_book_capture.schema import BookEvent, BookLevel
from polymarket_book_capture.writer import JsonlWriter


def _ev(ts: float) -> BookEvent:
    return BookEvent(
        ts=ts, market_id="0x", token_id="t", side="UP",
        btc_window_ts=int(ts) // 300 * 300,
        bids=[BookLevel(0.5, 1.0)], asks=[BookLevel(0.6, 1.0)], n_levels=10,
    )


def test_writer_creates_file_named_by_utc_date(tmp_path) -> None:
    w = JsonlWriter(tmp_path)
    ts = datetime(2026, 5, 1, 0, 30, 0, tzinfo=timezone.utc).timestamp()
    w.append(_ev(ts))
    w.close()
    expected = tmp_path / "20260501.jsonl.gz"
    assert expected.exists(), f"expected {expected}, got {list(tmp_path.iterdir())}"


def test_writer_rolls_over_on_utc_date_change(tmp_path) -> None:
    w = JsonlWriter(tmp_path)
    ts1 = datetime(2026, 5, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    ts2 = datetime(2026, 5, 2, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    w.append(_ev(ts1))
    w.append(_ev(ts2))
    w.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["20260501.jsonl.gz", "20260502.jsonl.gz"]


def test_writer_gzipped_jsonl_round_trips(tmp_path) -> None:
    w = JsonlWriter(tmp_path)
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    w.append(_ev(ts))
    w.append(_ev(ts + 1))
    w.close()
    path = tmp_path / "20260501.jsonl.gz"
    with gzip.open(path, "rt") as f:
        lines = [json.loads(line) for line in f]
    assert len(lines) == 2
    assert lines[0]["side"] == "UP"
