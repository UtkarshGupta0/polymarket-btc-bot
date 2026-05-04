# 10 · Polymarket Book Capture (Front 1A MVP)

A long-running script that subscribes to Polymarket's WebSocket market
channel for active `btc-updown-5m-*` markets and writes top-10 L2
book snapshots to a daily-rolling, gzip-compressed JSONL file.

## Why

Front 1B (fair-value-vs-market residual signal) needs live book depth.
Polymarket book snapshots are forward-only; Goldsky stores fills only.
This MVP starts the recording so future backtests can replay against
a tape that combines our existing fills with new book snapshots.

## How to run

```bash
nohup python scripts/capture_polymarket_books.py \
    --out data/books > logs/capture.log 2>&1 &
echo $! > logs/capture.pid
```

To stop:

```bash
kill "$(cat logs/capture.pid)"
```

For first-time validation against real WS frames (already done as part
of Task 6, schema confirmed — keep this command around for sanity
checks if Polymarket changes its API):

```bash
timeout 60 python scripts/capture_polymarket_books.py \
    --out data/books \
    --dump-raw logs/ws_raw.jsonl
head logs/ws_raw.jsonl
```

## Output

`data/books/YYYYMMDD.jsonl.gz` (one file per UTC day; daily roll-over
inside the running process). Each line is one BookEvent:

```json
{
  "ts": 1735689812.345,
  "market_id": "0x...",
  "token_id": "1234567890",
  "side": "UP",
  "btc_window_ts": 1735689600,
  "bids": [{"price": 0.92, "size": 50.0}],
  "asks": [{"price": 0.94, "size": 30.0}],
  "n_levels": 10
}
```

`btc_window_ts` is the unix-second of the 5-min window open, parsed
from the slug suffix `btc-updown-5m-<unix_ts>`.

`side` is `"UP"` if `token_id == market.token_up`, else `"DOWN"`.

## How the pipeline works

Three asyncio tasks coordinate via two queues:

1. **Discovery loop** — every 60s, queries gamma `/events?slug=btc-updown-5m-<ts>`
   for the current 5-minute window plus the next five future windows.
   Diffs against an in-memory registry; pushes `("add", token_id)` /
   `("remove", token_id)` deltas onto `sub_queue`.
2. **WS runner** — single `BookWSClient.run()` connection. On connect,
   debounces the initial `sub_queue` burst (1s) so the full asset set is
   known before any subscribe is sent, then sends ONE batch subscribe of
   the form `{"type": "MARKET", "assets_ids": [...]}`. Polymarket rejects
   any second subscribe on the same socket (`INVALID OPERATION`), so any
   subsequent registry change closes the socket and the outer loop
   reconnects with the new full set.
3. **Writer loop** — drains the event queue, appends each `BookEvent`
   as a gzip-JSONL line, rolls over to a new daily file at UTC midnight,
   and emits an INFO heartbeat every 1000 events.

## Operational notes

- **Disk:** ~30KB compressed for 1700 events in a 60s smoke run with
  ~12 active windows. A 24h capture on 12 windows is therefore on the
  order of 40MB, well under the 500MB envelope. Run `df` weekly anyway.
- **Restart safety:** No persistent state. After `kill`+restart, lost
  data is only the down-time window. Discovery and WS subscription
  re-bootstrap on startup.
- **Log cadence:** INFO on connect/subscribe (with asset count), INFO
  heartbeat every 1000 events written, WARNING on gamma fetch errors,
  WARNING on WS disconnects with backoff timing.
- **`data/` is gitignored** — captured files stay local.

## Spec / plan

- Spec: `docs/superpowers/specs/2026-05-01-polymarket-book-capture-mvp-design.md`
- Plan: `docs/superpowers/plans/2026-05-01-polymarket-book-capture-mvp.md` (gitignored)

## Smoke-test calibration (2026-05-04)

The plan's initial WS schema was a best-guess. First-run validation
surfaced two corrections, both already merged:

1. Discovery uses `/events?slug=...` (per-window), not `/markets?active=true`
   (which does not return BTC 5-min markets in its first 1500 rows).
2. WS subscribes are batched on connect; per-asset subscribes after the
   first one return `"INVALID OPERATION"`.

Parser shape (`event_type=book` + `asset_id` + `market` + `timestamp` +
`bids` + `asks`) was correct on first guess and required no change.
