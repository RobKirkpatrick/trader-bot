# Political Trader — Claude-Powered Political Market Trading

A production-quality Python module for trading Kalshi political prediction markets using Claude-powered news sentiment analysis combined with polling momentum and market momentum signals.

## Overview

**Core Edge:** Kalshi political markets are dominated by retail opinion traders. A systematic approach that scores recent political headlines via Claude, combined with polling trends and market momentum, offers structural edge over gut-feel pricing.

**Strategy:** Hold times are days to weeks (not hours like sports). Multiple simultaneous markets per political theme. Entries timed around fixed resolution dates (elections, votes).

**Three Signal Sources:**
1. **News sentiment** (50%) — Claude-scored political headlines
2. **Polling momentum** (35%) — 7-day trend from FiveThirtyEight/RealClearPolitics
3. **Market momentum** (15%) — Kalshi 24h price movement (smart money signal)

## Architecture

### Two-Process Automation

```
┌─────────────────────────────────┐
│   Scanner (every 6 hours)       │
│  - Discover all political mkt   │
│  - Filter by resolution window  │
│  - Assess news + polling signals│
│  - Write opportunities to DB    │
└──────────────┬──────────────────┘
               │
               v
        ┌──────────────┐
        │  DynamoDB:   │
        │ opportunities│
        └──────────────┘
               │
               v
┌─────────────────────────────────┐
│   Monitor (every 8 hours)       │
│  - Execute pending opportunities│
│  - Place orders, record trades  │
│  - Refresh signals on open pos  │
│  - Exit on signal reversal/edge │
│  - Handle resolutions & P&L     │
└──────────────┬──────────────────┘
               │
               v
        ┌──────────────┐
        │  DynamoDB:   │
        │  positions   │
        └──────────────┘
```

### Key Files

| File | Purpose |
|------|---------|
| `strategy.py` | All configuration, thresholds, weights |
| `models.py` | Dataclasses: Position, Signal, Opportunity |
| `signal_reader.py` | Claude news scoring + polling + market momentum |
| `scanner.py` | Market discovery & opportunity creation (6h) |
| `monitor.py` | Execution, monitoring, exits (8h) |
| `__init__.py` | Package exports |

## Quick Start

### Installation

1. **Copy to repo:**
   ```bash
   cp -r political_trader/ /path/to/bot/repo/
   ```

2. **Update Lambda handler** (`lambda_function.py`):
   ```python
   from political_trader.scanner import handler as political_scanner_handler
   from political_trader.monitor import handler as political_monitor_handler
   ```

3. **Add environment variables** (`.env.example`):
   ```bash
   POLITICAL_TRADER_ENABLED=false
   NEWSAPI_KEY=<your_key>
   ACCOUNT_BANKROLL=10000.0
   ```

4. **Create DynamoDB tables:**
   - `political-opportunities` (partition key: `market_ticker`)
   - `political-positions` (partition key: `position_id`)

5. **Create EventBridge schedules:**
   - Scanner: `cron(0 */6 * * ? *)` → calls scanner_handler
   - Monitor: `cron(0 */8 * * ? *)` → calls monitor_handler

6. **Deploy with `POLITICAL_TRADER_ENABLED=false`** and validate for 1 week

See **CLAUDE_CODE_HANDOFF.md** for complete integration guide.

## Signal Assessment

### News Sentiment Signal

Fetches recent political headlines via NewsAPI and scores them with Claude Sonnet:

```python
signal_reader = PoliticalSignalReader(kalshi_client, newsapi_key="...")
news_result = signal_reader.get_news_signal(
    market_title="Democrats win Senate majority — 2026",
    resolution_date="2026-11-03"
)
# Returns: {score: -0.67, confidence: 0.85, summary: "..."}
```

**Claude Prompt Focus:** Which party/candidate is gaining momentum? What policy developments help/hurt?

### Polling Momentum Signal

Calculates 7-day trend from FiveThirtyEight or RealClearPolitics:

```python
momentum = signal_reader.get_polling_momentum(
    candidate_or_party="Joe Biden",
    race="president"
)
# Returns: 0.15 (3-point gain normalized to ±10 = ±1.0 scale)
```

Gracefully falls back if API unavailable.

### Market Momentum Signal

Kalshi 24h price movement—indicates smart money flow:

```python
momentum = signal_reader.get_market_momentum("USCASEN23-D")
# Returns: 0.35 (market moved 3.5 cents toward YES in last 24h)
```

### Combined Signal & Implied Probability

Weighted average of available signals:

```python
combined = signal_reader.calculate_combined_signal(
    news=-0.45,
    polling=0.15,
    momentum=0.35
)
# Returns: -0.20 (slightly bearish)

implied_prob = signal_reader.get_implied_probability(-0.20)
# Returns: 0.42 (42% fair value for YES)
```

## Position Lifecycle

### 1. Opportunity Discovery (Scanner)

Scanner runs every 6 hours:

1. Fetches all markets in `POLITICAL_SERIES` + keyword matching
2. Filters: resolution date 2-90 days away, spread ≤8¢
3. Assesses news + polling + momentum signals
4. Checks thresholds: `MIN_COMBINED_SIGNAL=0.50`, `MIN_EDGE=0.07`
5. Writes qualifying opportunities to `political-opportunities` table

**Example alert:**
```
POLITICAL SCAN SUMMARY
Total opportunities: 3
KXSENATE: 2 markets
  - USCASEN23-D: signal=0.623 edge=0.082
  - USCASEN24-R: signal=-0.541 edge=0.063
```

### 2. Execution (Monitor Phase A)

Monitor runs every 8 hours and executes pending opportunities:

1. Fetches pending opportunities from DynamoDB
2. Re-checks signal freshness and edge
3. Checks position count limit (`MAX_SIMULTANEOUS_POSITIONS=6`)
4. Places limit order via Kalshi client
5. Creates `PoliticalPosition` record (status: `open`)
6. Sends SNS alert with full signal breakdown

**Position sizing:** Conservative for long hold times
- Base: $2-15 per position (vs $30-50 for sports)
- Scaled by signal confidence and edge magnitude
- Capped by account bankroll (15% max)

**Example alert:**
```
POLITICAL POSITION OPENED

Market: Democrats win Senate majority — 2026
Direction: YES
Entry Price: 0.58
Contracts: 25
Position Size: $14.50

Combined Signal: 0.623
News Sentiment: 0.67 (highly confident)
Polling Momentum: 0.15 (+3 points in 7 days)
Market Momentum: 0.35 (up 3.5¢ in 24h)
Fair Value (YES): 61.8%
Edge vs Market: 3.8¢
```

### 3. Monitoring (Monitor Phase B)

Every 8 hours, monitor refreshes all open positions:

1. **Check for resolution** → If market resolved, record P&L and close
2. **Refresh signals** → Re-assess news/polling/market momentum
3. **Check exit conditions:**
   - **Signal reversal** (threshold: -0.35) → Exit if signal flipped hard against us
   - **Edge compression** (threshold: <3¢) → Exit if opportunity compressed
4. **Update position** with latest signal and timestamp

Exit is via reverse order (close the position):

```
Position exited: USCASEN23-D
Reason: edge_compression
P&L: -$1.20 (-8.3%)
Days held: 3
```

### 4. Resolution & P&L

When market resolves:

- **Won:** P&L = (1.0 - entry_price) × contracts
- **Lost:** P&L = -(entry_price) × contracts
- **Early exit:** P&L = (exit_price - entry_price) × contracts

Example:
```
Position resolved: USCASEN23-D
Outcome: WON
P&L: +$8.50 (58.6%)
Days held: 12
```

## Configuration

All parameters in `strategy.py`:

```python
# Signal thresholds
MIN_NEWS_SIGNAL = 0.45               # Claude score magnitude
MIN_POLLING_MOMENTUM = 0.03          # 3-point swing in 7 days
MIN_COMBINED_SIGNAL = 0.50           # Weighted combo threshold
MIN_EDGE = 0.07                      # 7 cents minimum edge

# Position sizing (conservative for long holds)
MAX_POSITION_PER_MARKET = 15.00      # USD per position
MAX_SIMULTANEOUS_POSITIONS = 6       # Total open positions
MAX_PCT_BANKROLL = 0.15              # 15% max allocation

# Time windows
MAX_DAYS_TO_RESOLUTION = 90          # Don't enter if >90 days out
MIN_DAYS_TO_RESOLUTION = 2           # Don't enter if <2 days

# Exit rules
SIGNAL_REVERSAL_THRESHOLD = -0.35    # Exit if signal flips this much
TRAILING_EDGE_EXIT = 0.03            # Exit if edge <3 cents

# Weighting
SIGNAL_WEIGHTS = {
    "news_sentiment": 0.50,
    "polling_momentum": 0.35,
    "market_momentum": 0.15,
}
```

Adjust these based on validation results and market conditions.

## API Integration

### NewsAPI (for news sentiment)

Requires `NEWSAPI_KEY` environment variable (free tier: 100 calls/day).

```python
from political_trader.signal_reader import PoliticalSignalReader

signal_reader = PoliticalSignalReader(
    kalshi_client=kalshi_client,
    newsapi_key="sk-..."
)
```

Fetches last 7 days of headlines matching market topic, scores via Claude.

### FiveThirtyEight / RealClearPolitics (for polling)

Public APIs, no key required. Graceful fallback if unavailable.

- FiveThirtyEight: `https://projects.fivethirtyeight.com/polls/`
- RealClearPolitics: `https://api.realclearpolitics.com/json/rcp_poll_average.json`

Calculates 7-day momentum (current avg - 7-day-ago avg).

### Kalshi (for trades & market momentum)

Uses existing `carpet_bagger.kalshi_client.KalshiClient`. No changes needed.

## Monitoring & Alerts

### CloudWatch Logs

Key log lines to monitor:

```
# Scanner healthy
"Starting political market scan"
"Found 47 total political markets"
"Filtered to 18 tradeable markets"
"Scan complete: 3 qualifying opportunities in 12.4s"

# Monitor healthy
"Position opened: USCASEN23-D YES@0.58 signal=0.623"
"Monitoring position: USCASEN23-D"
"Position resolved: ... WON P&L=$4.50 (30.0%)"

# Issues to watch for
"Market momentum error for {ticker}: {error}"
"Signal deteriorated for {ticker}; canceling execution"
"Position limit reached (6); skipping {ticker}"
"Order failed for {ticker}: {reason}"
```

### SNS Alerts

**Scan Summary** (every 6h)
```
POLITICAL SCAN SUMMARY
Timestamp: 2026-04-05T08:00:00
Total opportunities: 3
KXSENATE: 2 markets
  - USCASEN23-D: signal=0.623 edge=0.082
```

**Position Opened** (when order fills)
```
POLITICAL POSITION OPENED

Market: Democrats win Senate majority — 2026
Direction: YES
Entry Price: 0.58
Signal Assessment: [detailed breakdown]
```

**Weekly Digest** (Sunday 8pm ET)
```
POLITICAL TRADER WEEKLY DIGEST

OPEN POSITIONS: 3, Avg 14 days to resolution
CLOSED THIS WEEK: 2, Win rate: 100%, P&L: +$12.30
```

## Data Models

### PoliticalPosition

Represents an open or closed position:

```python
@dataclass
class PoliticalPosition:
    position_id: str               # e.g., "USCASEN23-D-2026-04-05T..."
    market_ticker: str             # "USCASEN23-D"
    series: str                    # "KXSENATE"
    market_title: str              # Full market description

    # Signals at entry
    news_signal: float             # [-1.0, +1.0]
    polling_momentum: float        # [-1.0, +1.0] or 0.0
    market_momentum: float         # [-1.0, +1.0] or 0.0
    combined_signal: float         # Weighted combo
    entry_summary: str             # Claude's 1-sentence summary

    # Trade
    direction: str                 # "yes" or "no"
    entry_price: float             # Kalshi ask price (0.00-0.99)
    contracts: int                 # Number of contracts
    position_size_usd: float       # contracts × entry_price
    order_id: str                  # Kalshi order ID

    # Lifecycle
    status: str                    # "open" | "closed"
    opened_at: str                 # ISO datetime
    closed_at: Optional[str]       # ISO datetime or None
    resolution_date: str           # ISO date

    # P&L (when closed)
    pnl: float                     # Realized profit/loss (USD)
    outcome: Optional[str]         # "won" | "lost" | "early_exit"
```

Serializable to/from DynamoDB.

### PoliticalSignal

Complete signal assessment for a market:

```python
@dataclass
class PoliticalSignal:
    market_ticker: str
    market_title: str

    # Component signals
    news_signal: float             # [-1.0, +1.0]
    news_confidence: float         # [0.0, 1.0]
    news_summary: str              # 1-sentence Claude output

    polling_momentum: Optional[float]
    market_momentum: Optional[float]

    # Derived
    combined_signal: float         # Weighted combo
    implied_probability: float     # Fair value P(YES)
    edge_vs_market: float          # Fair value - current price

    # Market state
    current_yes_price: float
    bid_ask_spread: float
    last_24h_volume: int

    recommendation: str            # "BUY_YES" | "BUY_NO" | "HOLD"
```

### PoliticalOpportunity

Market that passed scanner thresholds, pending execution:

```python
@dataclass
class PoliticalOpportunity:
    opportunity_id: str
    market_ticker: str
    market_title: str

    signal: PoliticalSignal
    combined_signal: float
    edge_vs_market: float

    status: str                    # "pending" | "entered" | "skipped"
    created_at: str
    entered_position_id: Optional[str]
```

## Best Practices

### Entry

1. **Minimum edge:** Don't enter if edge <7¢ (political markets are thinner)
2. **Signal confidence:** Prefer news + polling confirmation (not just one signal)
3. **Liquidity check:** Bid-ask spread ≤8¢
4. **Time window:** 2-90 days to resolution (avoids thin near-term + high uncertainty long-term)

### Holding

1. **Monitor at 8h cadence:** Political markets move slower than sports, but refresh daily
2. **Watch polling releases:** Fridays often bring new polling; re-assess
3. **Track market movement:** 5¢+ moves in 24h may signal smart money
4. **Set mental stops:** Exit if signal flips by >35% against entry logic

### Exiting

1. **Signal reversal:** If combined signal flips hard, exit immediately
2. **Edge compression:** If Kalshi price moves your way significantly, take profit early
3. **Calendar:** 1-2 weeks before resolution, liquidity thins—consider exiting
4. **News shock:** Major political news can flip markets; don't hold through biggest surprises

## Troubleshooting

### No opportunities found

- Check `POLITICAL_SERIES` includes markets (log: "Found X total political markets")
- Verify `MIN_COMBINED_SIGNAL=0.50` isn't too high
- Check NewsAPI key is valid
- Review Claude scoring (should not all be 0s or ±1.0)

### Orders failing to execute

- Verify Kalshi account balance
- Check spread isn't >8¢ (liquidity constraint)
- Review limit prices (may be missing ask)
- Confirm market hasn't resolved/closed

### Signal is weak/noisy

- Verify polling API is working (check logs for "polling unavailable")
- Review `SIGNAL_WEIGHTS`—if polling missing, news dominates (may be too volatile)
- Consider reducing `MIN_COMBINED_SIGNAL` to 0.45

### P&L negative

- **Expected:** Political markets are structural bets, win rate ≠ profitability
- **Check:** Are you sizing too large? (Max $15 is conservative)
- **Validate:** Is Claude score actually correlating with market? (Do sample manual checks)

## Production Rollout

### Phase 1: Validation (1-2 weeks)

```bash
POLITICAL_TRADER_ENABLED=false
```

- Deploy code, monitor logs only
- Check scanner finds reasonable opportunities
- Verify Claude scoring makes sense
- Validate DynamoDB tables

### Phase 2: Scanner + Small Positions (1-2 weeks)

```bash
POLITICAL_TRADER_ENABLED=true
MAX_POSITION_PER_MARKET=2.00  # Reduce from $15
```

- Enable scanner, let opportunities accumulate
- Enable monitor with tiny positions
- Monitor P&L (should be breakeven-ish at this size)
- Check for execution issues

### Phase 3: Full Deployment

```bash
MAX_POSITION_PER_MARKET=15.00  # Back to normal
```

- Increase position sizing gradually
- Monitor weekly digest P&L
- Adjust `SIGNAL_WEIGHTS` based on post-hoc analysis

## Performance Expectations

- **Scan time:** 2-3 minutes (20-30 markets, Claude scoring)
- **Opportunities per week:** 3-8 (varies with political calendar)
- **Position hold time:** 5-30 days (median ~10 days)
- **Win rate:** 50-60% (edge-adjusted, not raw count)
- **Avg P&L per position:** $2-8 (small positions by design)

Cost of inference: ~$0.01-0.02 per scan (Claude Sonnet), NewsAPI (100 calls/day free tier).

## License & Support

This module is part of the Sentinel trading bot infrastructure. Integration questions: see **CLAUDE_CODE_HANDOFF.md**.

---

**Status:** Production-ready, 2026-04-05

**Last updated:** 2026-04-05

**Tested against:** Claude 3.5 Sonnet, Kalshi API, NewsAPI, Boto3 DynamoDB
