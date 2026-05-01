# Design — Polymarket book-capture MVP (Front 1A)

**Date:** 2026-05-01
**Status:** Approved (awaiting plan)
**Front:** Profitability improvement — Front 1A (book-depth data capture)

## Problem

The bot's current direction-prediction signal has negative edge in real markets (per `docs/HANDOVER/08_backtest_v2.md`). The contrarian-fade backtest experiment (`docs/HANDOVER/09_contrarian_experiment.md`) showed a directionally-promising pattern (edge_pp monotonically improves with threshold; +1.7pp at threshold=0.94) but the n=13 sample is below the 30-trade decision rule.

Both follow-ups need more data than the existing Goldsky tape provides:

- Front 1B (fair-value-vs-market residual signal) cannot be built without book depth — Goldsky stores fills, not book state.
- Front 2 expansion to ≥30 trades requires more BTC 5-min market days. Goldsky has fills but not book snapshots, so even when its archive grows, fair-value features will remain unavailable retroactively.

Polymarket book snapshots are forward-only — they exist while a market is alive on the order book, not retroactively. This spec captures them going forward, into a tape format that future backtests can join with the existing fills tape.

## Why this front, why now

- Without book data, Front 1B is impossible and Front 2 stays sample-starved.
- Book capture is independent of strategy choice — once the tape exists, multiple experiments can replay against it.
- MVP scope (run-and-forget for N days, throw away if no edge) caps risk at ≤1 day of effort.

## Decision

Single Python script `scripts/capture_polymarket_books.py` that:

1. Polls the Polymarket gamma API every 60s for upcoming and active `btc-updown-5m-<unix_ts>` markets.
2. Maintains a single WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribing to each discovered market's two outcome tokens. Auto-reconnects with exponential backoff (cap 30s) on disconnect.
3. On every book event, parses to a canonical schema and appends one JSON line to a daily-rolling, gzip-compressed file at `data/books/YYYYMMDD.jsonl.gz`.

Output schema (one JSON object per line):

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

`btc_window_ts` is derived from the slug suffix `btc-updown-5m-<unix_ts>` so the backtester can join book snapshots to existing windows without re-querying gamma.

## Architecture

### Components

| File | Responsibility |
|---|---|
| `polymarket_book_capture/__init__.py` | Package marker |
| `polymarket_book_capture/schema.py` | `BookLevel`, `BookEvent` dataclasses + JSON serialization |
| `polymarket_book_capture/discovery.py` | `find_btc_5m_markets() -> list[MarketInfo]` — gamma fetch + slug regex filter |
| `polymarket_book_capture/ws_client.py` | `BookWSClient` — connect, send sub/unsub, parse incoming book events to `BookEvent` |
| `polymarket_book_capture/writer.py` | `JsonlWriter(out_dir).append(event)` — daily-rolling, gzip append |
| `scripts/capture_polymarket_books.py` | Entry point — wires three asyncio tasks via `asyncio.gather` |
| `tests/test_book_capture.py` | Unit tests: schema round-trip, parser handles known event shapes, writer rolls on UTC date change, discovery filter rejects non-BTC slugs |

The package directory `polymarket_book_capture/` is new. It exists because this is a new subsystem with multiple cooperating files; lumping it into the repo root would clutter the existing flat layout. The package is internal-only — nothing imports it except the entry-point script and tests.

### Three asyncio tasks (in entry script)

- **discovery_loop** — every 60s: call `find_btc_5m_markets()`, diff against current subscription set, emit add/remove deltas to ws_loop via an `asyncio.Queue`.
- **ws_loop** — owns the websocket. On startup, connect; on subscription delta, send sub/unsub. On book event, parse and push to writer queue. On disconnect, exponential-backoff reconnect (start 1s, cap 30s); on reconnect, re-subscribe entire current set.
- **writer_loop** — drains writer queue, calls `JsonlWriter.append(event)`. Logs INFO every 1000 events: `events_written, current_subs, ws_uptime_sec`.

Inter-task coordination uses `asyncio.Queue` exclusively. No shared mutable state outside queues.

### Data flow

```
gamma API ──poll──▶ discovery_loop ──Queue──▶ ws_loop ──events──▶ writer_loop ──▶ JSONL.gz
                                                  ▲
                                                  │
                                          Polymarket WS
```

### Error handling

- **gamma 5xx / timeout:** log warning, retry next cycle. Don't tear down ws.
- **WS disconnect:** ws_loop exception path; cancel current connection, sleep `min(2^attempt, 30)`, reconnect, re-subscribe whole set.
- **Parse error on book event:** log error with raw payload (truncated to 500 chars), increment `parse_errors_total` counter, drop event.
- **Writer disk full:** unrecoverable. Crash with stack trace. Caller's `nohup` wrapper handles restart.
- **Slug regex non-match:** silently filtered in discovery (other market types are not our concern).

## Operational

- Run: `nohup python scripts/capture_polymarket_books.py > capture.log 2>&1 &`
- Stop: `kill <pid>`
- No persistent state — restart re-discovers active markets, loses only data during downtime.
- Logs: INFO at startup, on subscription deltas, every 1000 events. WARNING on gamma errors, parse errors, ws disconnects. ERROR on writer crash.

## Acceptance

- `pytest tests/test_book_capture.py` exits 0 (≥6 unit tests covering schema, parser, writer roll-over, discovery filter).
- Manual: `python scripts/capture_polymarket_books.py` runs for ≥1h without crash, writes ≥1MB to `data/books/YYYYMMDD.jsonl.gz`, log shows ≥3 subscription delta events and ≥1 INFO heartbeat.
- After 24h run: JSONL contains events for ≥200 distinct `btc_window_ts` values (BTC 5-min market roll = 288/day; ≥70% coverage tolerated for MVP).
- Output JSONL parses cleanly by a one-liner (`gunzip -c data/books/YYYYMMDD.jsonl.gz | jq -c 'select(.side == "UP") | .btc_window_ts' | sort -u | wc -l`).
- All existing tests still pass.

## Risk + caveats

- **WS schema not formally published.** First run may surface schema mismatches. Plan: capture script supports `--dump-raw` flag that writes raw WS frames alongside parsed events for the first session, so we can iterate the parser against real data.
- **Rate limits / connection caps unknown.** Polymarket may restrict subs per connection. Mitigation if hit: shard subscriptions across multiple WS connections (one per ~10 markets). Out of MVP scope; revisit if rate-limit errors observed.
- **gamma API new-market detection lag.** If a market appears <60s before window-open, discovery_loop misses early book activity. Acceptable for MVP — entry-window-only book state (T-45..T-8) captures the strategy-relevant period; pre-window book state is bonus.
- **Disk pressure.** Estimate ~500MB/day compressed. Sustainable for a week. Operator must `df` periodically.
- **Survivorship in eventual backtests.** Markets we capture during run = markets that existed during run. Same bias as Goldsky.
- **No backtester integration.** Tape will sit unused until Front 1B writes a separate spec to integrate.

## Out of scope

- Process supervision (systemd unit, restart-on-fail). Manual `nohup` is fine for MVP.
- Backtester integration — separate spec, blocked on this tape existing.
- Schema versioning — fix v1, evolve only if Front 1B needs it.
- Gap detection / Prometheus metrics / alerting.
- Authentication — Polymarket WS public market data does not require auth.
- Capturing market types other than BTC 5-min.
- Compression beyond gzip; no parquet, no DuckDB. Convert to columnar later if data is actually used.
- Multi-WS connection sharding. Single connection until rate limits force change.

If the captured tape proves useful (Front 1B works, Front 2 sample expansion produces edge), a follow-up "Front 1A-prod" spec promotes this to a production-supervised service.
