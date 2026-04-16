# POLYMARKET BTC 5-MINUTE MAKER BOT — FULL BUILD SPEC

> **Give this entire file to Claude Code.** It contains everything needed to build,
> test, and deploy a Polymarket trading bot from scratch on Arch Linux with Hyprland.

---

## TABLE OF CONTENTS

1. [CONTEXT & GOAL](#1-context--goal)
2. [SYSTEM ENVIRONMENT](#2-system-environment)
3. [THE STRATEGY (WHY THIS WORKS)](#3-the-strategy-why-this-works)
4. [ARCHITECTURE OVERVIEW](#4-architecture-overview)
5. [MODULE 1: MARKET FINDER](#5-module-1-market-finder)
6. [MODULE 2: PRICE FEED](#6-module-2-price-feed)
7. [MODULE 3: SIGNAL ENGINE](#7-module-3-signal-engine)
8. [MODULE 4: EXECUTOR](#8-module-4-executor)
9. [MODULE 5: RISK MANAGER](#9-module-5-risk-manager)
10. [MODULE 6: MAIN BOT LOOP](#10-module-6-main-bot-loop)
11. [MODULE 7: TELEGRAM ALERTS](#11-module-7-telegram-alerts)
12. [MODULE 8: TRADE LOGGER & ANALYTICS](#12-module-8-trade-logger--analytics)
13. [MODULE 9: SELF-IMPROVEMENT LOOP](#13-module-9-self-improvement-loop)
14. [CONFIGURATION & ENV](#14-configuration--env)
15. [BUILD ORDER (PHASE BY PHASE)](#15-build-order-phase-by-phase)
16. [CRITICAL GOTCHAS & BUG AVOIDANCE](#16-critical-gotchas--bug-avoidance)
17. [POLYMARKET API REFERENCE](#17-polymarket-api-reference)
18. [TESTING PLAN](#18-testing-plan)
19. [DEPLOYMENT ON ARCH LINUX](#19-deployment-on-arch-linux)

---

## 1. CONTEXT & GOAL

I have ~$50 in crypto (USDC on Polygon) and ~$20 in Anthropic API credits. I want a
Python bot that trades Polymarket's "BTC Up or Down — 5 minute" binary markets
automatically 24/7 on my ThinkPad running Arch Linux. The bot must:

- Run in PAPER MODE first with zero real money to validate the strategy
- Be switchable to LIVE MODE with a single env variable
- Use MAKER-ONLY limit orders (zero fees + daily maker rebates)
- Never place taker/market orders (taker fees on crypto are 1.8% peak as of March 2026)
- Manage risk conservatively (fractional Kelly sizing, drawdown limits)
- Send Telegram alerts for trades and daily summaries
- Log every trade to JSON for analysis
- Have a self-improvement loop that reviews performance weekly

The target is 55-65% win rate with 5-12% profit per winning trade, compounding the $30
initial allocation. Even modest success + potential $POLY airdrop from active trading
makes this worthwhile.

---

## 2. SYSTEM ENVIRONMENT

```
OS:           Arch Linux (rolling release)
WM:           Hyprland on Wayland
Terminal:     Kitty
Shell:        zsh with Starship prompt
GPU:          Intel UHD 630 + NVIDIA Quadro P2000 (Pascal)
Python:       Use system Python 3.12+
Packages:     Install via pacman/yay where possible, pip with --break-system-packages for Python-only packages
Network:      BITS Pilani campus WiFi (WPA2-Enterprise)
tmux:         Bot should run inside a tmux session for persistence
```

### Installation Commands

```bash
# Python dependencies
pip install py-clob-client==0.34.6 --break-system-packages
pip install python-dotenv websockets aiohttp numpy --break-system-packages

# Optional: for Telegram alerts
pip install python-telegram-bot --break-system-packages
```

**IMPORTANT:** Do NOT use venvs unless absolutely necessary. I prefer --break-system-packages
for Arch. Do NOT use `sudo pip`. Use `yay` for AUR packages if needed.

---

## 3. THE STRATEGY (WHY THIS WORKS)

### 3.1 The Market

Polymarket opens a new binary market every 5 minutes:
- "Will BTC be higher or lower than the opening price when this window closes?"
- 288 markets per day, each lasting exactly 300 seconds
- Resolution via Chainlink oracle on Polygon
- You buy "Up" or "Down" tokens at $0.01–$0.99, winning tokens pay $1.00

### 3.2 The Edge (THREE compounding advantages)

**Edge 1: Information Latency**
- Binance WebSocket delivers BTC price with sub-100ms latency
- Chainlink oracle updates every 10-30 seconds or on 0.5% deviations
- Polymarket's market prices lag real exchange data by 2-15 seconds
- At T-10 seconds before window close, ~85% of direction is determined
- But Polymarket odds often still price the winning side at $0.88-0.95 instead of $0.97+

**Edge 2: Maker Fee Advantage**
- Taker fees on crypto markets: up to 1.80% peak (at 50% probability)
- Maker fees: ZERO. Plus 20% daily rebate of taker fees collected from your fills
- A taker needs 51.5%+ win rate to break even
- A maker at $0.90 entry needs only ~47% win rate (plus rebates as bonus)

**Edge 3: Time Decay**
- As window close approaches, token prices should converge to $0.00 or $1.00
- But in practice, they converge slowly due to thin liquidity
- Placing maker orders at $0.90-0.95 with 15-30 seconds left captures this inefficiency

### 3.3 Why NOT to Use the Old Strategy

My previous bot was a taker-style directional bot using Claude API as a "price action reader."
Problems with that approach in 2026:
- Polymarket removed the 500ms order delay and added dynamic taker fees in Jan 2026
- Taker arbitrage is dead — average arb window is 2.7 seconds, 73% captured by sub-100ms bots
- Claude API calls add 1-3 seconds latency per decision — way too slow for 5-min windows
- The resolver in my old bot used BTC spot price instead of Polymarket's official resolution,
  creating a false-positive feedback loop that masked losses
- No per-window deduplication meant duplicate bets on the same window, amplifying losses

---

## 4. ARCHITECTURE OVERVIEW

```
polymarket-btc-bot/
├── bot.py                 # Main orchestrator loop
├── market_finder.py       # Deterministic 5-min market discovery
├── price_feed.py          # Binance WebSocket real-time BTC/USDT
├── signal_engine.py       # Direction + confidence computation
├── executor.py            # Paper + Live order execution (MAKER ONLY)
├── risk_manager.py        # Kelly sizing, drawdown limits, loss streaks
├── telegram_alerts.py     # Trade notifications + daily summary
├── trade_logger.py        # JSON logging + analytics
├── self_improver.py       # Weekly Claude API review of performance
├── config.py              # Centralized configuration from .env
├── .env                   # Secrets (never committed)
├── .env.example           # Template
├── requirements.txt       # Python deps
├── logs/                  # Auto-created trade logs
│   ├── trades_YYYYMMDD.json
│   └── daily_summary_YYYYMMDD.json
└── README.md
```

### Data Flow

```
Binance WebSocket ──→ price_feed.py ──→ PriceState object
                                            │
                                            ▼
market_finder.py ──→ MarketWindow ──→ bot.py (main loop)
                                            │
                                            ├── signal_engine.py → Signal (direction, confidence, price)
                                            │
                                            ├── risk_manager.py → position size or reject
                                            │
                                            ├── executor.py → place GTC maker order
                                            │
                                            ├── trade_logger.py → append to JSON
                                            │
                                            └── telegram_alerts.py → send notification
```

### Timing Diagram for One 5-Minute Window

```
T+0s     T+255s     T+270s            T+292s     T+300s    T+302s
  │         │          │                 │          │         │
  │         │    ┌──────┴────────┐       │    ┌─────┴───┐     │
  │         │    │ Entry Window   │       │    │ Resolve │     │
  │         │    │ T-45 to T-8   │       │    │         │     │
  │         │    │ Signal check   │       │    │ Check   │     │
  │         │    │ every 500ms    │       │    │ outcome │     │
  │         │    │ Place maker if │       │    │ Log P&L │     │
  │         │    │ conf > 0.60    │       │    └─────────┘     │
  │         │    └────────────────┘       │                    │
  │         │                             │                    │
  ▼         ▼                             ▼                    ▼
 Open     Start                        Stop              New Window
 Price    Looking                      Orders              Opens
 Recorded                                                   │
                                                            └── Reset
                                                                state
```

---

## 5. MODULE 1: MARKET FINDER (`market_finder.py`)

### Purpose
Deterministically find the current and next BTC 5-minute market on Polymarket using
Unix timestamps. No searching or scanning needed.

### Key Insight
BTC 5-min markets follow a deterministic slug pattern:
```
btc-updown-5m-{unix_timestamp}
```
Where `unix_timestamp` is the START of the 5-minute window, aligned to 300-second boundaries.

### Implementation

```python
INTERVAL_5M = 300

def get_current_window_start() -> int:
    now = int(time.time())
    return now - (now % INTERVAL_5M)

def build_slug(start_ts: int) -> str:
    return f"btc-updown-5m-{start_ts}"

def build_event_url(start_ts: int) -> str:
    return f"https://polymarket.com/event/{build_slug(start_ts)}"
```

### API Calls Needed

1. **Find market by event slug** (Gamma API, no auth):
   ```
   GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{ts}&limit=1
   ```
   Response contains `markets[0].clobTokenIds` — a JSON array of 2 token IDs.
   - `clobTokenIds[0]` = UP token ID (a very long integer as string)
   - `clobTokenIds[1]` = DOWN token ID

2. **If event not indexed yet** (happens for brand-new windows), fall back to:
   ```
   GET https://gamma-api.polymarket.com/markets?tag_id=crypto&active=true&limit=20&order=createdAt&ascending=false
   ```
   Filter results for the BTC 5-min market matching our timestamp.

3. **Get orderbook prices** (CLOB API, no auth for read):
   ```
   GET https://clob.polymarket.com/book?token_id={token_id}
   ```
   Returns `{ "bids": [{"price": "0.53", "size": "100"}, ...], "asks": [...] }`

### Data Class

```python
@dataclass
class MarketWindow:
    start_ts: int
    end_ts: int              # start_ts + 300
    slug: str
    event_url: str
    market_id: str | None
    condition_id: str | None
    up_token_id: str | None  # Very long integer string
    down_token_id: str | None
    up_best_bid: float | None
    up_best_ask: float | None
    down_best_bid: float | None
    down_best_ask: float | None
```

### IMPORTANT NOTES
- `clobTokenIds` may come as a JSON string from the API — always `json.loads()` it if it's a string
- Token IDs are enormous integers (76+ digits). Store as strings, never cast to int.
- The Gamma API has ~60 req/min rate limit. Cache market data; don't re-fetch every 500ms.
- There IS a delay between a window opening and the market appearing in the Gamma API.
  The deterministic slug approach bypasses this delay.

---

## 6. MODULE 2: PRICE FEED (`price_feed.py`)

### Purpose
Real-time BTC/USDT price from Binance WebSocket. This is our fastest data source — 
faster than Polymarket's oracle and faster than the market itself.

### WebSocket Connection

```python
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
```

The `aggTrade` stream sends every trade with:
```json
{
  "e": "aggTrade",
  "s": "BTCUSDT",
  "p": "84250.50",    // Price
  "q": "0.001",       // Quantity
  "T": 1713052800000, // Trade time (milliseconds)
  "m": false          // Is buyer maker? (false = buyer is taker = buy pressure)
}
```

### Use the `websockets` library (NOT `websocket-client`)

```python
import websockets
async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
    async for message in ws:
        data = json.loads(message)
        price = float(data["p"])
```

### State Object (updated on every tick)

```python
@dataclass
class PriceState:
    current_price: float = 0.0
    window_open_price: float = 0.0     # Set at T+0 of each window
    window_start_ts: int = 0
    vwap: float = 0.0                  # Volume-weighted average price (rolling 2 min)
    momentum: float = 0.0              # Price velocity ($/tick)
    trend_direction: int = 0           # +1, -1, or 0
    delta_from_open: float = 0.0       # (current - open) / open as ratio
    delta_from_open_abs: float = 0.0   # abs(delta_from_open)
    buy_volume: float = 0.0            # Accumulated buy-side volume
    sell_volume: float = 0.0           # Accumulated sell-side volume
    volume_imbalance: float = 0.0      # (buy - sell) / total, range -1 to +1
    tick_count: int = 0
    last_update: float = 0.0
```

### Key Methods

- `set_window_open(price, start_ts)` — Called at the START of each 5-min window.
  Resets all window-level stats (volume, delta, tick count).
- `on_tick(data)` — Called on every aggTrade. Updates price, accumulates volume,
  recalculates delta, VWAP (every tick), momentum (every 10 ticks).
- `get_current_price_rest()` — Fallback REST call if WebSocket drops.

### Momentum Calculation

Compare average of last 10 prices vs previous 10 prices:
```python
recent = prices[-10:]
earlier = prices[-20:-10]
momentum = mean(recent) - mean(earlier)
# Positive = price accelerating up, negative = accelerating down
```

### VWAP (Volume-Weighted Average Price)

```python
# Rolling 120-second window
vwap = sum(price * quantity for ticks in window) / sum(quantity for ticks in window)
```

### Reconnection Logic
- On disconnect, log a warning and retry after 2 seconds
- Keep a `_running` flag to stop cleanly on shutdown
- Have a REST fallback via `https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT`

### IMPORTANT
- Keep a deque of recent ticks (maxlen=10000) for VWAP and momentum
- Keep a separate deque of recent prices (maxlen=500) for momentum
- Use `data["m"]` to determine trade side: `True` = buyer is maker (seller initiated, bearish),
  `False` = seller is maker (buyer initiated, bullish). This is counterintuitive but correct.
- The WebSocket requires `ping_interval=20` or Binance will disconnect.

---

## 7. MODULE 3: SIGNAL ENGINE (`signal_engine.py`)

### Purpose
Takes the current PriceState and determines: direction (UP/DOWN), confidence (0.0-1.0),
and suggested maker order price ($0.88-$0.95).

### Signal Components (4 weighted inputs)

| Component | Weight | Input | Interpretation |
|-----------|--------|-------|---------------|
| Window Delta | 0.40 | `delta_from_open` | Primary signal. Directly answers "is BTC up or down?" |
| Momentum | 0.25 | `momentum` | Is the move accelerating? Confirms or contradicts delta. |
| Volume Imbalance | 0.20 | `volume_imbalance` | Buy vs sell pressure. Persistent buy = UP likely. |
| VWAP Position | 0.15 | `current_price vs vwap` | Above VWAP = bullish, below = bearish. |

### Delta → Base Confidence Mapping

The absolute price delta from window open maps to a base confidence:

```
|delta| < 0.005%  → 10% confidence   (noise, basically a coin flip)
|delta| ~ 0.01%   → 20%
|delta| ~ 0.02%   → 35%
|delta| ~ 0.05%   → 50%              (moderate move)
|delta| ~ 0.10%   → 65%
|delta| ~ 0.20%   → 75%              (strong move)
|delta| ~ 0.50%   → 85%
|delta| ~ 1.00%   → 92%              (very strong, near certain)
```

Use linear interpolation between thresholds. Cap at 95%.

### Composite Score Calculation

```python
composite = (
    0.40 * delta_confidence * delta_direction  # -1 or +1
  + 0.25 * momentum_alignment                 # -1 to +1 (positive = confirms delta)
  + 0.20 * volume_alignment                   # -1 to +1
  + 0.15 * vwap_alignment                     # -1 to +1
)

direction = "UP" if composite >= 0 else "DOWN"
confidence = abs(composite)
```

Each alignment signal:
- Is positive if it CONFIRMS the delta direction
- Is negative if it CONTRADICTS the delta direction
- Is scaled to -1.0 to +1.0 range

### Time Boost

Closer to window close = more reliable signal. Apply multiplier:
- T-15s or less: `confidence *= 1.15`
- T-30s or less: `confidence *= 1.08`
- Cap final confidence at 0.95

### Confidence → Maker Price Mapping

Higher confidence → we're willing to pay more (smaller profit margin):

```
Confidence 55%  → Price $0.88  (12% profit per share if win)
Confidence 65%  → Price $0.90  (10% profit)
Confidence 75%  → Price $0.92  (8% profit)
Confidence 85%  → Price $0.93  (7% profit)
Confidence 95%  → Price $0.95  (5% profit, minimum margin)
```

NEVER place an order above $0.95. The profit margin becomes too thin.

### Trade Decision Gate

```python
def should_trade(signal) -> bool:
    if signal.confidence < MIN_CONFIDENCE:  # default 0.60
        return False
    if signal.expected_value <= 0:          # EV = conf*profit - (1-conf)*cost
        return False
    if signal.seconds_to_close < 5:        # Too late, won't fill as maker
        return False
    return True
```

### Output

```python
@dataclass
class Signal:
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0 to 1.0
    suggested_price: float  # 0.88 to 0.95
    rationale: str          # Human-readable explanation
    timestamp: float
    window_delta: float
    seconds_to_close: float
    expected_value: float   # (confidence * profit) - ((1-confidence) * cost)
```

---

## 8. MODULE 4: EXECUTOR (`executor.py`)

### Purpose
Handles order placement in both paper and live modes. ONLY places maker (limit/GTC) orders.

### Paper Executor

Simulates trades without real money. On `place_order()`:
- Check per-window deduplication (ONE TRADE PER WINDOW, no exceptions)
- Deduct cost from simulated balance
- Mark as FILLED immediately (assume fill at our limit price)

On `resolve_trade(btc_went_up)`:
- If our direction matches outcome: payout = shares × $1.00, PnL = payout - cost
- If not: PnL = -cost
- Update simulated balance

### Live Executor (Polymarket CLOB API)

#### SDK Setup

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# For email/Magic login (most common):
client = ClobClient(
    HOST,
    key=PRIVATE_KEY,           # Export from reveal.polymarket.com
    chain_id=CHAIN_ID,
    signature_type=1,          # 1 = email/Magic
    funder=FUNDER_ADDRESS,     # The address holding your USDC
)

# For MetaMask / EOA:
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
# signature_type defaults to 0

# ALWAYS derive API creds before trading:
client.set_api_creds(client.create_or_derive_api_creds())
```

#### Placing a Maker Order

```python
order_args = OrderArgs(
    price=0.92,          # Our limit price (2 decimal places)
    size=5.43,           # Number of shares (2 decimal places)
    side=BUY,
    token_id="37821...", # The UP or DOWN token ID (very long string)
)

signed_order = client.create_order(order_args)

# GTC = Good-Till-Cancelled. This is a RESTING LIMIT ORDER (maker).
# It sits on the orderbook until filled or cancelled.
resp = client.post_order(signed_order, OrderType.GTC)
# resp = {"orderID": "0xabc...", "status": "LIVE", ...}
```

#### CRITICAL: Maker vs Taker

- `OrderType.GTC` = resting limit order = MAKER (zero fees + rebate)
- `OrderType.FOK` = fill-or-kill = TAKER (pays dynamic taker fee!)
- `OrderType.FAK` = fill-and-kill = TAKER
- We ONLY use GTC. Never FOK or FAK.
- If the price is marketable (would immediately match), GTC still fills but as a TAKER.
  To prevent this, our price should be BELOW the current best ask.
  For example, if UP best ask is $0.94, place our buy at $0.92.

#### Order Management

```python
# Cancel an order
client.cancel(order_id)

# Cancel all orders
client.cancel_all()

# Check order status
order = client.get_order(order_id)
# order["status"] in: "LIVE", "MATCHED", "CANCELLED"

# Get positions
positions = client.get_positions()
```

#### Precision Rules (CRITICAL — orders get rejected silently if wrong)

| Tick Size | Price Decimals | Size Decimals |
|-----------|---------------|---------------|
| 0.01      | 2             | 2             |
| 0.001     | 3             | 2             |
| 0.0001    | 4             | 2             |

BTC 5-min markets typically use tick_size=0.01. So:
- Price: round to 2 decimal places (`round(price, 2)`)
- Size: round to 2 decimal places (`round(shares, 2)`)
- Minimum size: 5 shares

### Per-Window Deduplication

```python
_traded_windows: set[int] = set()

def place_order(self, window_start_ts, ...):
    if window_start_ts in self._traded_windows:
        logger.warning(f"Already traded window {window_start_ts}")
        return None
    # ... place order ...
    self._traded_windows.add(window_start_ts)
```

This is NON-NEGOTIABLE. Without this, confidence oscillations cause multiple trades
per window, amplifying losses. One trade per window. Period.

---

## 9. MODULE 5: RISK MANAGER (`risk_manager.py`)

### Position Sizing: Fractional Kelly Criterion

```python
# Kelly % = (b*p - q) / b
# where b = (1/entry_price - 1), p = confidence, q = 1-confidence
# Use 0.25x Kelly (conservative)

b = (1.0 / entry_price) - 1.0  # e.g., $0.90 → b = 0.111
p = confidence
q = 1 - p
kelly_pct = (b * p - q) / b
target_pct = kelly_pct * 0.25   # Quarter Kelly

available = current_balance - min_reserve  # Always keep $5 reserve
size = available * target_pct
size = clamp(size, min=MIN_BET, max=MAX_BET)
```

### Risk Limits

| Limit | Default | Effect |
|-------|---------|--------|
| Max single trade | $5.00 | Hard cap per order |
| Min single trade | $1.00 | Below this, skip |
| Max daily drawdown | $5.00 | Bot pauses for the day |
| Max consecutive losses | 5 | Bot pauses, resets on next win |
| Min reserve | $5.00 | Never bet below this balance |

### State Tracking

```python
@dataclass
class RiskState:
    starting_balance: float
    current_balance: float
    daily_pnl: float
    daily_trades: int
    daily_wins: int
    daily_losses: int
    consecutive_losses: int
    max_drawdown_hit: bool
```

### Daily Reset
At midnight UTC, reset daily_pnl, daily_trades, wins, losses, consecutive_losses,
and max_drawdown_hit. Log the daily summary before reset.

---

## 10. MODULE 6: MAIN BOT LOOP (`bot.py`)

### The Core Loop (asyncio)

```python
async def trading_loop():
    while running:
        now = time.time()
        current_start = get_current_window_start()
        window_end = current_start + 300
        seconds_remaining = window_end - now

        # 1. NEW WINDOW DETECTED
        if current_start != last_window_ts:
            resolve_pending_trade()  # Resolve previous window
            record_window_open_price(price_feed.state.current_price)
            fetch_market_data(current_start)  # Get token IDs
            pending_trade = None
            last_window_ts = current_start

        # 2. ENTRY WINDOW (T-45s to T-8s)
        if 8 < seconds_remaining <= 45 and pending_trade is None:
            signal = compute_signal(price_feed.state, window_end)
            if signal and should_trade(signal):
                can, reason = risk_manager.can_trade()
                if can:
                    size = risk_manager.calculate_position_size(signal.confidence, signal.suggested_price)
                    if size > 0:
                        token_id = up_token if signal.direction == "UP" else down_token
                        trade = executor.place_order(current_start, signal.direction,
                                                     signal.confidence, signal.suggested_price,
                                                     size, token_id)
                        if trade:
                            pending_trade = trade
                            send_telegram_alert(trade)

        # 3. WINDOW CLOSED — RESOLVE
        if seconds_remaining <= 0 and pending_trade is not None:
            await asyncio.sleep(2)  # Wait for final price to settle
            btc_went_up = price_feed.state.current_price >= price_feed.state.window_open_price
            resolve_and_log(pending_trade, btc_went_up)
            pending_trade = None

        # 4. TICK RATE
        if seconds_remaining <= 50:
            await asyncio.sleep(0.5)  # Fast during entry window
        else:
            await asyncio.sleep(2.0)  # Slow otherwise
```

### Startup Sequence

1. Load `.env` configuration
2. Initialize all modules (price_feed, executor, risk_manager, market_finder, telegram)
3. Start Binance WebSocket in background task
4. Wait until first price tick received (timeout 10s)
5. Print startup banner with mode, capital, settings
6. Enter main trading loop

### Shutdown Sequence

1. Catch SIGINT/SIGTERM
2. Set `_running = False`
3. Cancel any pending orders (live mode)
4. Print final session report
5. Save trade log
6. Stop price feed
7. Exit cleanly

---

## 11. MODULE 7: TELEGRAM ALERTS (`telegram_alerts.py`)

### Messages to Send

1. **Trade placed:**
   ```
   🎯 BTC 5m | UP @ $0.92 | $3.50 | Conf: 72% | T-18s
   ```

2. **Trade resolved:**
   ```
   ✅ WON +$0.28 | Balance: $30.28 | WR: 62% (8/13)
   ❌ LOST -$3.50 | Balance: $26.50 | Streak: 2L
   ```

3. **Daily summary (midnight UTC):**
   ```
   📊 Daily Report | Apr 14
   Trades: 47 | Wins: 29 | WR: 62%
   PnL: +$2.15 | Balance: $32.15
   Best: +$0.42 | Worst: -$3.50
   ```

4. **Risk alert:**
   ```
   ⚠️ Bot paused: daily drawdown limit hit (-$5.00)
   ```

### Implementation
Use `python-telegram-bot` library (async version). Send via:
```python
import telegram
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
```

If Telegram credentials are not set, silently skip (don't crash).

---

## 12. MODULE 8: TRADE LOGGER (`trade_logger.py`)

### Log Format (JSON)

```json
{
  "session": {
    "mode": "paper",
    "start_time": 1713052800,
    "starting_balance": 30.0
  },
  "trades": [
    {
      "window_ts": 1713052800,
      "time": "2026-04-14T12:00:02Z",
      "direction": "UP",
      "confidence": 0.72,
      "entry_price": 0.92,
      "size_usdc": 3.50,
      "shares": 3.80,
      "outcome": "WIN",
      "pnl": 0.28,
      "btc_open": 84200.50,
      "btc_close": 84250.30,
      "delta_pct": 0.0592,
      "seconds_to_close_at_entry": 18.5,
      "balance_after": 30.28
    }
  ]
}
```

### Files
- `logs/trades_YYYYMMDD.json` — One file per day, append each trade
- `logs/daily_summary_YYYYMMDD.json` — Written at midnight UTC

---

## 13. MODULE 9: SELF-IMPROVEMENT LOOP (`self_improver.py`)

### Purpose
Weekly review of trading performance using Claude API. Analyze patterns in wins/losses
and suggest parameter adjustments.

### Implementation

```python
import anthropic

client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

def weekly_review(trade_log: list[dict]) -> str:
    prompt = f"""
    Analyze these Polymarket BTC 5-min bot trades from the past week.
    
    Trades: {json.dumps(trade_log)}
    
    Analyze:
    1. Win rate by confidence bucket (50-60%, 60-70%, 70-80%, 80-90%, 90%+)
    2. Win rate by time-of-day (which hours are most/least profitable?)
    3. Win rate by delta magnitude (small vs large moves)
    4. Are there patterns in consecutive losses?
    5. Is the entry timing optimal or should we adjust the entry window?
    
    Suggest specific parameter changes:
    - MIN_CONFIDENCE: currently {MIN_CONFIDENCE}
    - ENTRY_WINDOW_START: currently {ENTRY_WINDOW_START}
    - Signal weights: currently delta=0.40, momentum=0.25, volume=0.20, vwap=0.15
    - Kelly fraction: currently 0.25
    
    Be specific. Give exact numbers, not ranges.
    """
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

Budget: ~50 calls at $0.01-0.03 each = $0.50-1.50/week. Well within $20 budget.

Run this as a separate script: `python self_improver.py` (weekly, not automated in main loop).

---

## 14. CONFIGURATION & ENV

### `.env.example`

```bash
# === Polymarket Credentials (only needed for live mode) ===
POLYMARKET_PRIVATE_KEY=
POLYMARKET_FUNDER_ADDRESS=
POLYMARKET_SIGNATURE_TYPE=1

# === Trading Parameters ===
STARTING_CAPITAL=30.0
MAX_BET_SIZE=5.0
MIN_BET_SIZE=1.0
MIN_CONFIDENCE=0.60
ENTRY_WINDOW_START=45
ENTRY_WINDOW_END=8
MAX_DAILY_DRAWDOWN=5.0
MAX_CONSECUTIVE_LOSSES=5

# === Telegram (optional) ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# === Anthropic (for self-improvement) ===
ANTHROPIC_API_KEY=

# === Mode ===
TRADING_MODE=paper
```

### `config.py`

Centralize all config loading. Load from `.env` with defaults. Validate at startup.
Print all settings (except secrets) on boot.

---

## 15. BUILD ORDER (PHASE BY PHASE)

### Phase 1: Foundation (Get data flowing)
1. `config.py` — Load env, validate, print settings
2. `price_feed.py` — Connect to Binance WS, print prices every 50 ticks
3. `market_finder.py` — Compute current window, fetch token IDs, print results
4. **Test:** Run `python market_finder.py` and `python price_feed.py` independently

### Phase 2: Brain (Signal generation)
5. `signal_engine.py` — Implement all 4 signals, composite score, confidence mapping
6. **Test:** Feed synthetic PriceState data, verify output signals make sense

### Phase 3: Paper Trading
7. `executor.py` — PaperExecutor with dedup, balance tracking, resolution
8. `risk_manager.py` — Kelly sizing, drawdown limits
9. `trade_logger.py` — JSON logging
10. `bot.py` — Wire everything together, run the main loop
11. **Test:** Run paper mode for 1 hour. Check logs. Verify one-trade-per-window.

### Phase 4: Hardening
12. `telegram_alerts.py` — Send trade alerts
13. Reconnection logic for WebSocket drops
14. Graceful shutdown on SIGINT
15. Error handling everywhere (never crash on API errors)
16. **Test:** Run paper mode for 24 hours. Analyze trade logs.

### Phase 5: Live Trading
17. `executor.py` — LiveExecutor with py-clob-client
18. Order status checking (was our GTC order filled?)
19. **Test:** Place one tiny real trade ($1) manually to verify credentials work
20. Switch to live mode with small capital ($10 initially)

### Phase 6: Intelligence
21. `self_improver.py` — Weekly Claude API analysis
22. Analytics dashboard (optional — print summary stats on demand)

---

## 16. CRITICAL GOTCHAS & BUG AVOIDANCE

### Bug 1: Resolver Using Spot Price Instead of Oracle
**Wrong:** Check if BTC went up by comparing Binance spot at close vs open.
**Right:** For paper mode, Binance spot is a good-enough proxy (it's what the oracle
tracks). For live mode, the actual resolution comes from Polymarket's Chainlink oracle.
BUT in paper mode, DO use Binance spot — it's 95%+ correlated with the oracle.

The gotcha is: don't use an arbitrary "check time" — use the price at the exact window
close moment (T+300). Wait 2 seconds after close before reading the price.

### Bug 2: Per-Window Deduplication Failure
If the signal oscillates (e.g., confidence goes 0.58 → 0.62 → 0.57 → 0.63), without
dedup you'll place multiple trades on the same window. Use a `set[int]` of traded
window timestamps. Check BEFORE placing. Add IMMEDIATELY after placing.

### Bug 3: Token ID Confusion
- `conditionId` = identifies the market (hex string)
- `tokenId` / `clobTokenId` = identifies a specific outcome (very long integer string)
- Trading endpoints need TOKEN IDs, not condition IDs
- UP and DOWN have DIFFERENT token IDs

### Bug 4: Accidental Taker Orders
If your GTC buy price >= current best ask, your order fills immediately as a TAKER
(and pays fees). Always check the orderbook first:
```python
if suggested_price >= best_ask:
    suggested_price = best_ask - 0.01  # Stay below ask to be maker
```

### Bug 5: WebSocket Disconnects
Binance WS disconnects after 24 hours. The bot MUST auto-reconnect:
```python
while self._running:
    try:
        await self._connect_and_stream()
    except Exception:
        await asyncio.sleep(2)  # Reconnect
```

### Bug 6: Race Condition at Window Boundary
At T+300, the old window closes and the new window opens simultaneously.
Always resolve the previous window BEFORE setting up the new one.

### Bug 7: USDC Precision
USDC has 6 decimal places on-chain. The py-clob-client SDK handles this internally,
but be aware: balance values from the API are in wei (multiply by 10^-6 for USDC).
The SDK method `get_balance()` returns wei. Divide by 1_000_000 for human-readable USDC.

### Bug 8: Missing Token Allowances (Live Mode)
Before first trade, EOA/MetaMask users MUST approve the CTF contract:
```python
# Use web3.py to approve USDC and CTF contracts
# Or use Polymarket's UI to trade once manually — this auto-approves
```
Email/Magic wallet users are typically pre-approved.

### Bug 9: Stale Gamma API Data
The Gamma API's `outcomePrices` can lag the CLOB orderbook by seconds. For signal
generation, ALWAYS use the CLOB API (`/book`) or WebSocket for real-time prices.
Only use Gamma API for market discovery (token IDs, metadata).

### Bug 10: Python asyncio + WebSocket Threading
The Binance WebSocket runs in an async task. The bot loop runs in the same event loop.
Do NOT use `time.sleep()` — use `await asyncio.sleep()`. Do NOT use `requests` for
the price feed — use `aiohttp` for async HTTP if needed.

---

## 17. POLYMARKET API REFERENCE

### Gamma API (Market Discovery) — No Auth

```
Base: https://gamma-api.polymarket.com
Rate: ~60 req/min

GET /events?slug={slug}&limit=1
GET /markets?tag_id=crypto&active=true&limit=20&order=createdAt&ascending=false
GET /markets/{market_id}
```

### CLOB API (Trading) — Auth Required for Writes

```
Base: https://clob.polymarket.com
Rate: ~100 req/min (authenticated), ~10 req/sec for orders

# Read (no auth):
GET /book?token_id={token_id}
GET /price?token_id={token_id}&side=BUY
GET /midpoint?token_id={token_id}

# Write (auth required):
POST /order          # Place order
DELETE /order/{id}   # Cancel order
DELETE /orders       # Cancel all
GET /orders          # List your orders
GET /order/{id}      # Get order status
```

### py-clob-client SDK Methods

```python
# Read (no auth)
client.get_order_book(token_id)    # Returns {"bids": [...], "asks": [...]}
client.get_price(token_id, side)   # Returns best price string
client.get_midpoint(token_id)      # Returns midpoint string

# Auth required
client.create_order(OrderArgs(...))                  # Sign order
client.post_order(signed_order, OrderType.GTC)       # Submit
client.cancel(order_id)                              # Cancel one
client.cancel_all()                                  # Cancel all
client.get_order(order_id)                           # Status
client.get_orders()                                  # List all
client.get_positions()                               # Current positions
client.get_balance_allowance(params)                 # USDC balance
```

### Order Types

```python
OrderType.GTC  # Good-Till-Cancelled — MAKER (use this!)
OrderType.FOK  # Fill-Or-Kill — TAKER (never use)
OrderType.FAK  # Fill-And-Kill — TAKER (never use)
```

---

## 18. TESTING PLAN

### Unit Tests

1. **market_finder:** Given timestamp 1713052800, verify slug = "btc-updown-5m-1713052800"
2. **signal_engine:** Given delta=+0.05%, momentum=+2, vol_imb=+0.3, verify direction=UP, confidence>0.50
3. **signal_engine:** Given delta=-0.10%, verify direction=DOWN
4. **signal_engine:** Given delta=+0.001%, verify confidence < MIN_CONFIDENCE (skip trade)
5. **risk_manager:** Given balance=$10, verify position size never exceeds $5
6. **risk_manager:** Given 5 consecutive losses, verify can_trade() returns False
7. **executor:** Verify duplicate window_ts is rejected

### Integration Tests

1. Run `python price_feed.py` — verify price updates within 1 second
2. Run `python market_finder.py` — verify token IDs are fetched
3. Run `python bot.py` for 30 minutes in paper mode — verify:
   - No crashes
   - Exactly 0 or 1 trade per window
   - Trade log has correct format
   - Balance updates correctly
   - Risk limits enforced

### Paper Mode Validation (24-48 hours)

Before going live, verify:
- Win rate > 50% (ideally > 55%)
- No consecutive loss streaks > 5
- Daily PnL is positive on at least 2/3 of days
- No memory leaks (watch RSS memory over time)
- WebSocket reconnects cleanly after drops

---

## 19. DEPLOYMENT ON ARCH LINUX

### tmux Session

```bash
# Create persistent session
tmux new-session -d -s polybot

# Start the bot inside tmux
tmux send-keys -t polybot 'cd ~/polymarket-btc-bot && python bot.py' Enter

# Detach: Ctrl+B, then D
# Reattach: tmux attach -t polybot
```

### systemd Service (optional, for auto-restart)

```ini
# ~/.config/systemd/user/polybot.service
[Unit]
Description=Polymarket BTC 5-min Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/polymarket-btc-bot
ExecStart=/usr/bin/python %h/polymarket-btc-bot/bot.py
Restart=on-failure
RestartSec=10
Environment=TRADING_MODE=paper

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable polybot
systemctl --user start polybot
journalctl --user -u polybot -f  # View logs
```

### Monitoring

- Telegram alerts for real-time monitoring
- `tail -f logs/trades_$(date +%Y%m%d).json` for live trade log
- `python self_improver.py` weekly for strategy review

---

## SUMMARY FOR CLAUDE CODE

Build this bot in the exact phase order specified in Section 15. Start with Phase 1
(data flowing), verify each module works independently, then wire them together.

Key principles:
1. **MAKER ONLY** — Never place FOK or FAK orders. Only GTC limit orders.
2. **ONE TRADE PER WINDOW** — Per-window deduplication is mandatory.
3. **PAPER FIRST** — Start in paper mode. Go live only after 24+ hours of validation.
4. **ASYNC** — Everything is asyncio. Never use blocking calls in the main loop.
5. **FAIL SAFE** — Every API call wrapped in try/except. Never crash on network errors.
6. **Arch Linux** — Use `--break-system-packages` for pip. No venvs unless necessary.

The user's Polymarket credentials will be in `.env`. The bot should work in paper mode
with ZERO configuration (no Polymarket account needed for paper trading).
