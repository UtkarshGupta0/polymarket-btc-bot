"""Daily-rolling, gzipped JSONL writer for book events."""
from __future__ import annotations

import gzip
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, IO

from polymarket_book_capture.schema import BookEvent

logger = logging.getLogger(__name__)


class JsonlWriter:
    """Appends BookEvent JSON lines to data/books/YYYYMMDD.jsonl.gz, rolling on UTC date.

    Not thread-safe. Single writer task should own one instance.
    """

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = Path(out_dir)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._cur_date: Optional[str] = None
        self._fh: Optional[IO[str]] = None

    @staticmethod
    def _date_key(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")

    def _ensure_open(self, date_key: str) -> None:
        if self._cur_date == date_key and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        path = self._out_dir / f"{date_key}.jsonl.gz"
        self._fh = gzip.open(path, "at", encoding="utf-8")
        self._cur_date = date_key
        logger.info("opened %s for append", path)

    def append(self, event: BookEvent) -> None:
        date_key = self._date_key(event.ts)
        self._ensure_open(date_key)
        assert self._fh is not None
        self._fh.write(event.to_json())
        self._fh.write("\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._cur_date = None
