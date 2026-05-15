# Macro Trader Module

Production-quality bridge between the macro sentiment pipeline and Kalshi economic prediction markets.

## Overview

`macro_trader` implements a complete trading system that:

1. **Reads** macro sentiment signals from `sentiment/news_macro.py`
2. **Matches** signals to open Kalshi economic event markets (Fed rates, CPI, jobs, GDP)
3. **Calculates** edge: signal-implied probability vs. market price
4. **Executes** trades when edge is sufficient and signal/confidence thresholds met
5. **Manages** positions through market resolution or early exit

## Key Insight

The sentiment pipeline already analyzes macro news and scores macro events (-1.0 to +1.0). This module reuses those signals for prediction markets where:
- Liquidity exists (Kalshi has tight bid-ask spreads)
- Edges are larger (retail traders price less efficiently than equity markets)
- Resolution is binary (no shorting, no leverage complications)

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  sentiment/news_macro.py (every 6h)                 │
│  - Fetches macro news (NewsAPI)                      │
│  - Claude analysis → signal dict                     │
│  - NEW: Writes to macro-signal-cache DynamoDB       │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│  macro_trader/scanner.py (every 6h)                 │
│  - Reads signal from DynamoDB cache                  │
│  - Fetches open Kalshi markets (via KalshiClient)   │
│  - Matches signals to markets by keyword             │
│  - Calculates edge: implied_prob - market_price     │
│  - Writes qualifying opportunities to DynamoDB       │
│  - SNS summary: "2 Fed opportunities, 1 CPI"        │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│  macro_trader/monitor.py (every 4h)                 │
│  Phase A: Execute pending opportunities             │
│   - Re-validate market (still open, still has edge) │
│   - Re-validate signal (still actionable)            │
│   - Place order via Kalshi API                       │
│   - Write MacroPosition to DynamoDB                  │
│                                                      │
│  Phase B: Monitor open positions                     │
│   - Check if market resolved → close & record P&L   │
│   - Check signal reversal → exit early              │
│   - Check max hold days → exit                       │
│   - Hold to resolution by default                    │
│                                                      │
│  Phase C: Daily P&L summary (8pm ET)                │
│   - List open positions + signal values              │
│   - Report closed positions & daily P&L              │
│   - Send SNS summary                                 │
└─────────────────────────────────────────────────────┘
```

## Components

### strategy.py

Configuration: all thresholds and parameters.

```python
MIN_SIGNAL_STRENGTH = 0.50          # Only trade if |signal| >= 0.50
MIN_CONFIDENCE = 0.65               # Only trade if confidence >= 65%
MIN_EDGE = 0.08                     # Only trade if gap >= 8 cents
MAX_BID_ASK_SPREAD = 0.05           # Skip illiquid markets
POSITION_HOLD_MAX_DAYS = 14         # Exit if unresolved after 14 days
SIGNAL_REVERSAL_EXIT_THRESHOLD = -0.30  # Exit if signal reverses this far
```

**Tuning**: All values can be changed in `.env` without code changes.

### models.py

Data structures for DynamoDB:

- **MacroPosition**: An open or closed trade position
  - Hash key: `position_id` (UUID)
  - Stores: entry signal, direction, price, contracts, outcome, P&L
  - Status: "open" or "closed"

- **MacroOpportunity**: A discovered opportunity awaiting execution
  - Hash key: `opportunity_id` (UUID)
  - Stores: signal, market, edge, recommended size
  - Status: "pending" → "executed" or "skipped"

### signal_reader.py

Interface to sentiment signals:

```python
reader = MacroSignalReader()

# Read latest signal from cache
signal = reader.get_latest_signal()
# → {"overall_score": 0.65, "fed_signal": 0.72, "confidence": 0.80, ...}

# Check if signal is actionable
if reader.is_signal_actionable(signal, "fed_signal"):
    # Signal strength >= 0.50 AND confidence >= 0.65

# Convert signal to probability
prob = reader.estimate_implied_probability(0.72)
# 0.72 signal → 80% implied probability for YES outcome

# Calculate edge
edge = reader.calculate_edge(0.80, 0.65)
# Implied 80%, market priced 65% → +0.15 (15% edge)
```

**Key method**: `estimate_implied_probability(signal_value)`
- Maps signal (-1.0 to +1.0) to probability (0.0 to 1.0)
- Linear: `prob = 0.50 + (signal * 0.40)`
- Examples:
  - signal=+0.75 → 80% YES
  - signal=-0.50 → 30% YES (i.e., 70% NO)

### scanner.py

Discovers trading opportunities:

```python
await scanner.run()
# → {
#     "scan_id": "scan_2026-04-03T...",
#     "opportunities_found": 3,
#     "opportunities_by_signal": {"fed_signal": 2, "inflation_signal": 1},
#     "summary": "2 Fed opportunities, 1 CPI opportunity"
# }
```

**Flow**:
1. Load macro signal from DynamoDB
2. For each actionable signal (fed, inflation, employment, GDP):
   - Fetch markets from Kalshi
   - Filter by resolution window (1-30 days)
   - Filter by liquidity (bid-ask < 5 cents)
   - Calculate edge
   - Only qualify if edge >= 8 cents
3. Write opportunities to DynamoDB
4. Send SNS summary

**Output**: DynamoDB table `macro-opportunities` with pending trades.

### monitor.py

Executes opportunities and manages positions:

```python
await monitor.run()
# → {
#     "phase_a_executed": 2,
#     "phase_b_closed": 1,
#     "phase_b_exited_early": 0,
#     "phase_c_summary": "..."
# }
```

**Phase A: Execute pending opportunities**
- Re-validate market & signal
- Place order via Kalshi
- Create MacroPosition in DynamoDB
- SNS: "MACRO POSITION OPENED: Fed Funds, Bought YES @ 0.62, edge: +0.14"

**Phase B: Monitor open positions**
- Check if market resolved → close & record P&L
- Check if signal reversed > threshold → exit early
- Check if held > 14 days → exit
- Otherwise hold to resolution

**Phase C: Daily P&L summary (8pm ET)**
- List open positions with current signals
- Report closed positions & daily P&L
- SNS summary

## Usage

### Deploy

Copy `macro_trader/` to bot repo root and run integration steps in `CLAUDE_CODE_HANDOFF.md`.

### Enable/Disable

```bash
# In .env
MACRO_TRADER_ENABLED=false  # Start disabled, validate, then enable

# Disable anytime
export MACRO_TRADER_ENABLED=false
```

When disabled, both scanner and monitor exit immediately without doing anything.

### Monitor Operations

1. **Check signal caching**: Query DynamoDB `macro-signal-cache` table
   ```bash
   aws dynamodb scan --table-name macro-signal-cache
   ```

2. **Check discovered opportunities**: Query DynamoDB `macro-opportunities`
   ```bash
   aws dynamodb scan --table-name macro-opportunities --filter-expression "#status = :pending"
   ```

3. **Check open positions**: Query DynamoDB `macro-positions`
   ```bash
   aws dynamodb scan --table-name macro-positions --filter-expression "#status = :open"
   ```

4. **Check P&L**: Query closed positions
   ```bash
   aws dynamodb scan --table-name macro-positions --filter-expression "#status = :closed"
   ```

5. **Check logs**: CloudWatch Logs for scanner and monitor Lambda functions

6. **Check SNS**: Email or SMS alerts for position opens/closes/P&L

## Signal Mapping

Signals are matched to Kalshi series by keyword:

```python
SIGNAL_TO_SERIES_KEYWORDS = {
    "fed_signal": ["FED", "FEDFUNDS", "FOMC", "RATES"],
    "inflation_signal": ["CPI", "INFLATION", "PCE", "KXCPI"],
    "employment_signal": ["JOBS", "NFP", "UNEMPLOYMENT", "PAYROLL"],
    "gdp_signal": ["GDP", "GROWTH"],
}
```

For example, if `fed_signal=+0.72` and `confidence=0.80`:
- Keywords: ["FED", "FEDFUNDS", "FOMC", "RATES"]
- Fetch Kalshi markets with these keywords
- For each market:
  - Implied probability: 0.80 (80% for YES on bullish outcome)
  - Market YES price: 0.62 (Kalshi)
  - Edge: 0.80 - 0.62 = +0.18 (18 cents)
  - If edge >= 0.08, create opportunity
  - Direction: "yes" (signal is positive/bullish)

## Position Sizing

Conservative 25% of max position per market:

```python
MAX_POSITION_PER_MARKET = 10.00  # USD per trade
MAX_SIMULTANEOUS_POSITIONS = 5   # Limit exposure
MAX_PCT_BANKROLL = 0.20          # Max 20% per position

actual_position_size = MAX_POSITION_PER_MARKET * 0.25  # 25% of max
contracts = int(position_size_usd / market_price)
```

## Exit Conditions

Positions exit in this priority:

1. **Market resolves** (highest priority)
   - If outcome matches direction → WIN (full profit)
   - If outcome doesn't match → LOSS (full loss)

2. **Signal reversal** (dynamic)
   - If signal for this position moves > -0.30 from entry
   - Exit at current market price
   - Example: Fed signal entered at +0.72, falls to +0.35 → exit

3. **Max hold duration** (safety)
   - If position unresolved after 14 days → exit at market

4. **Manual** (operator override)
   - Manually update position status in DynamoDB

## P&L Tracking

All positions track:
- Entry price, contracts, position size
- Exit price (market resolution or exit signal)
- Outcome (won/lost/early_exit)
- P&L in USD
- Exit reason (resolved_win/resolved_loss/signal_reversal/max_hold_days)

Daily P&L summary aggregates closed positions and shows current exposure.

## Reusing carpet_bagger.kalshi_client

The module imports `KalshiClient` from `carpet_bagger/kalshi_client.py` to avoid duplication:

```python
from carpet_bagger.kalshi_client import KalshiClient

kalshi_client = KalshiClient()
markets = kalshi_client.get_market_series("KXFED")
market = kalshi_client.get_market_details("KXFED_20260515")
order = kalshi_client.place_order("KXFED_20260515", "yes", 5, 0.62)
```

Ensure the existing `KalshiClient` has these methods. If methods differ, update scanner/monitor to match.

## Logging

All modules use Python `logging` with module-specific loggers:

```python
logger = logging.getLogger(__name__)
```

Logs include:
- Signal validation (confidence, strength checks)
- Market discovery and filtering
- Edge calculations and qualified opportunities
- Order placement and position creation
- Position monitoring (resolution, reversals, exits)
- P&L tracking
- Errors with context

## Testing

To test without live trading:

1. Set `MACRO_TRADER_ENABLED=false` in .env
2. Manually call `scanner.run()` and `monitor.run()`
3. Inspect DynamoDB tables for data
4. Check CloudWatch logs for execution flow
5. Verify SNS alerts are sent
6. Once confident, set `MACRO_TRADER_ENABLED=true`

## Troubleshooting

**No signal cached**
- Check sentiment/news_macro.py has `cache_signal()` call
- Verify macro-signal-cache table exists
- Check IAM permissions

**No opportunities discovered**
- Check signal is actionable (strength >= 0.50, confidence >= 0.65)
- Check Kalshi API is responding (carpet_bagger.kalshi_client working)
- Check market keywords match (may need to expand keywords in strategy.py)

**Positions not executing**
- Check MACRO_TRADER_ENABLED=true
- Check monitor Phase A logs for errors
- Verify Kalshi account has balance
- Check DynamoDB write permissions

**P&L not shown**
- Check daily summary time window (8pm ET ±1 hour)
- Check SNS topic and subscribers
- Check monitor Phase C logs

## Production Checklist

Before enabling in production:

- [ ] Sentiment module caches signal to DynamoDB
- [ ] Scanner discovers opportunities correctly
- [ ] Monitor executes without errors
- [ ] SNS alerts are sent
- [ ] Positions are tracked in DynamoDB
- [ ] P&L calculations are correct
- [ ] Kalshi client integration works
- [ ] IAM permissions all correct
- [ ] DynamoDB tables exist and have correct schema
- [ ] Logging is enabled and readable
- [ ] Feature flag defaults to false
- [ ] All thresholds are in config/settings.py

## Future Enhancements

- **Multi-signal blending**: Combine macro signals with volatility, momentum
- **Risk management**: Per-signal exposure caps, portfolio drawdown limits
- **Smarter position sizing**: Kelly criterion, volatility scaling
- **Market expansion**: Add more Kalshi series (crypto, sports, commodities)
- **Execution optimization**: Market orders, tighter entry/exit
- **Signal feedback**: Learn which signals work better
- **Backtesting**: Historical analysis of signal efficacy

## Questions?

See the docstrings in each module for detailed implementation. Start with:
1. `strategy.py` - understand the parameters
2. `models.py` - understand data structures
3. `signal_reader.py` - understand signal flow
4. `scanner.py` - understand opportunity discovery
5. `monitor.py` - understand execution and management
