# Macro Trader Integration Guide

This document describes how to integrate the `macro_trader` module into the existing AWS Lambda bot.

## Overview

`macro_trader` bridges the existing macro sentiment pipeline (`sentiment/news_macro.py`) to Kalshi economic prediction markets. It discovers trading opportunities based on Claude's macro analysis and executes them with position management to market resolution.

**Key insight**: The sentiment bot was already scoring macro events for equity trading. This reuses those signals for prediction markets where retail traders price less efficiently, creating larger edges.

---

## Integration Steps

### 1. Copy Module to Repo

Copy the `macro_trader/` directory to your bot's root:

```bash
cp -r macro_trader/ /path/to/bot/repo/
```

Expected structure:
```
repo/
├── macro_trader/
│   ├── __init__.py
│   ├── strategy.py
│   ├── models.py
│   ├── signal_reader.py
│   ├── scanner.py
│   └── monitor.py
├── sentiment/
├── carpet_bagger/
└── ...
```

### 2. Update Lambda Handler

In `lambda_function.py`, add imports and scheduling for the two macro trader jobs:

```python
# At top of lambda_function.py
from macro_trader import scanner, monitor
import os
import asyncio

# In your EventBridge/scheduled handler (or create one)
async def handle_macro_trader_scanner(event, context):
    """
    Scheduled event: Run every 6 hours, or immediately after sentiment scan completes.
    Alternative: Trigger via SNS from sentiment/news_macro after it completes.
    """
    if not os.getenv("MACRO_TRADER_ENABLED", "false").lower() == "true":
        logger.info("MACRO_TRADER_ENABLED is false; skipping scanner")
        return {"status": "disabled"}

    try:
        result = await scanner.run()
        logger.info(f"Macro scanner completed: {result}")
        return result
    except Exception as e:
        logger.error(f"Macro scanner failed: {e}", exc_info=True)
        return {"error": str(e)}


async def handle_macro_trader_monitor(event, context):
    """
    Scheduled event: Run every 4 hours.
    Executes pending opportunities and manages open positions.
    """
    if not os.getenv("MACRO_TRADER_ENABLED", "false").lower() == "true":
        logger.info("MACRO_TRADER_ENABLED is false; skipping monitor")
        return {"status": "disabled"}

    try:
        result = await monitor.run()
        logger.info(f"Macro monitor completed: {result}")
        return result
    except Exception as e:
        logger.error(f"Macro monitor failed: {e}", exc_info=True)
        return {"error": str(e)}
```

### 3. Configure EventBridge Schedules

Create two EventBridge rules to trigger the scanner and monitor:

#### Scanner (6-hour interval)

```
Name: macro-trader-scanner
Schedule: rate(6 hours)
Target: Lambda function (macro_trader_scanner handler)
```

#### Monitor (4-hour interval)

```
Name: macro-trader-monitor
Schedule: rate(4 hours)
Target: Lambda function (macro_trader_monitor handler)
```

Alternatively, trigger scanner immediately after sentiment scan completes by having `sentiment/news_macro.py` publish to SNS and subscribing the scanner to that topic.

### 4. Update config/settings.py

Add these environment variables:

```python
# Macro trader feature flag
MACRO_TRADER_ENABLED = os.getenv("MACRO_TRADER_ENABLED", "false").lower() == "true"

# Macro trader configuration (overrides strategy.py defaults if set)
MACRO_TRADER_MAX_POSITION = float(os.getenv("MACRO_TRADER_MAX_POSITION", "10.00"))
MACRO_TRADER_MIN_SIGNAL = float(os.getenv("MACRO_TRADER_MIN_SIGNAL", "0.50"))
MACRO_TRADER_MIN_CONFIDENCE = float(os.getenv("MACRO_TRADER_MIN_CONFIDENCE", "0.65"))
MACRO_TRADER_MIN_EDGE = float(os.getenv("MACRO_TRADER_MIN_EDGE", "0.08"))
MACRO_TRADER_MAX_BID_ASK_SPREAD = float(os.getenv("MACRO_TRADER_MAX_BID_ASK_SPREAD", "0.05"))

# DynamoDB table names
MACRO_SIGNAL_CACHE_TABLE = os.getenv("MACRO_SIGNAL_CACHE_TABLE", "macro-signal-cache")
MACRO_OPPORTUNITIES_TABLE = os.getenv("MACRO_OPPORTUNITIES_TABLE", "macro-opportunities")
MACRO_POSITIONS_TABLE = os.getenv("MACRO_POSITIONS_TABLE", "macro-positions")
```

### 5. CRITICAL: Update sentiment/news_macro.py

Add a DynamoDB write at the end of the `news_macro.py` main function to cache the signal:

```python
# At end of news_macro.py main() function, after generating signal dict

import boto3
import os
from datetime import datetime

def cache_signal(signal: dict) -> None:
    """Write macro signal to DynamoDB cache for macro_trader to read."""
    try:
        dynamodb = boto3.resource("dynamodb")
        cache_table_name = os.getenv("MACRO_SIGNAL_CACHE_TABLE", "macro-signal-cache")
        cache_table = dynamodb.Table(cache_table_name)

        # Add timestamp if not present
        signal["generated_at"] = signal.get("generated_at", datetime.utcnow().isoformat() + "Z")

        # Use today's date as hash key for easy daily queries
        signal_date = datetime.utcnow().date().isoformat()

        cache_table.put_item(
            Item={
                "signal_date": signal_date,
                **signal
            }
        )
        logger.info(f"Cached macro signal for date {signal_date}")
    except Exception as e:
        logger.error(f"Failed to cache macro signal: {e}")


# In main():
signal = {
    "overall_score": ...,
    "fed_signal": ...,
    # ... (rest of signal dict)
}

cache_signal(signal)  # NEW: Add this line
return signal
```

This one-liner enables `signal_reader.py` to read signals without re-running the full sentiment scan.

### 6. Create DynamoDB Tables

Create three new tables in DynamoDB:

#### Table 1: macro-signal-cache

```
Name: macro-signal-cache
Hash key: signal_date (String)
Billing: On-Demand (or provisioned as needed)
TTL: Enable on generated_at field (auto-expire old signals after ~7 days)
```

#### Table 2: macro-opportunities

```
Name: macro-opportunities
Hash key: opportunity_id (String)
Billing: On-Demand
Attributes: market_ticker, scanned_at, status (String)
```

#### Table 3: macro-positions

```
Name: macro-positions
Hash key: position_id (String)
Billing: On-Demand
Attributes: market_ticker, status (String), signal_key (String)
```

### 7. Add .env.example Entries

```bash
# Macro trader
MACRO_TRADER_ENABLED=false
MACRO_TRADER_MAX_POSITION=10.00
MACRO_TRADER_MIN_SIGNAL=0.50
MACRO_TRADER_MIN_CONFIDENCE=0.65
MACRO_TRADER_MIN_EDGE=0.08
MACRO_TRADER_MAX_BID_ASK_SPREAD=0.05

# Macro trader tables
MACRO_SIGNAL_CACHE_TABLE=macro-signal-cache
MACRO_OPPORTUNITIES_TABLE=macro-opportunities
MACRO_POSITIONS_TABLE=macro-positions
```

### 8. Start with Feature Flag OFF

In your deployment/testing:

1. Set `MACRO_TRADER_ENABLED=false` initially
2. Verify scanner runs and logs SNS summaries for a few cycles (verify signal matching, opportunities discovered)
3. Once confident, set `MACRO_TRADER_ENABLED=true` to enable actual position execution
4. Monitor Phase A and Phase B logs for order placement and position management

---

## Architecture Notes

### Signal Flow

```
sentiment/news_macro.py (every 6 hours)
    ↓
    ↓ (writes signal to DynamoDB cache)
    ↓
macro_trader/scanner.py (every 6 hours)
    ↓ (reads signal, matches to markets)
    ↓ (writes opportunities to DynamoDB)
    ↓
macro_trader/monitor.py (every 4 hours)
    ↓ (executes opportunities, manages positions)
    ↓ (resolves markets, calculates P&L)
```

### Position Lifecycle

1. **Scanner discovers** opportunity (signal + market + edge)
2. **Opportunity pending** in DynamoDB
3. **Monitor re-validates** (signal still actionable? market still open?)
4. **Monitor executes** (places order via Kalshi)
5. **Position open** in DynamoDB
6. **Monitor monitors** (checks for resolution, signal reversal, max hold)
7. **Position resolves** or exits early
8. **Position closed** with P&L recorded

### Reusing carpet_bagger.kalshi_client

The scanner and monitor import `KalshiClient` from `carpet_bagger/kalshi_client.py`:

```python
from carpet_bagger.kalshi_client import KalshiClient
```

This avoids duplicating Kalshi API integration. Ensure the existing `KalshiClient` implements:
- `get_market_series(ticker)` → list of markets
- `get_market_details(ticker)` → market dict with price, status, resolution_date
- `place_order(market_ticker, direction, contracts, price)` → order result with order_id

---

## Validation Checklist

Before enabling `MACRO_TRADER_ENABLED=true`:

- [ ] Sentiment module caches signal to DynamoDB (check macro-signal-cache table)
- [ ] Scanner runs and logs signal matching (check logs for "Processing signal: fed_signal=0.72")
- [ ] Scanner discovers opportunities (check macro-opportunities table)
- [ ] Monitor runs without errors (check logs for "Phase A: Executing pending")
- [ ] SNS alerts are sent (check email/SNS topic)
- [ ] DynamoDB tables have data (positions, opportunities, signals)
- [ ] Kalshi client integration works (successful get_market_series calls)

---

## Operational Notes

### Monitoring

- **Scan logs**: Look for opportunity discovery and edge calculations
- **Monitor logs**: Track position execution and resolution
- **SNS alerts**: Review for position opens, closes, and P&L
- **DynamoDB scans**: Query macro-positions with status=open to see current exposure
- **P&L tracking**: Daily summary includes realized and unrealized performance

### Tuning Strategy

All thresholds in `strategy.py` can be tuned without code changes:

- Lower `MIN_SIGNAL_STRENGTH` to trade weaker signals
- Lower `MIN_CONFIDENCE` to act on less certain analysis
- Raise `MIN_EDGE` to only take high-conviction trades
- Adjust `MAX_POSITION_PER_MARKET` and `MAX_PCT_BANKROLL` for risk tolerance
- Adjust `SIGNAL_REVERSAL_EXIT_THRESHOLD` to exit sooner if signals weaken

### Disabling Gracefully

To disable without removing code:

```python
# In lambda_function.py or environment
MACRO_TRADER_ENABLED=false
```

All scheduled jobs will log "disabled" and exit immediately.

---

## Troubleshooting

### Signal not cached

**Issue**: macro-signal-cache table is empty
**Fix**:
1. Verify `sentiment/news_macro.py` has `cache_signal()` call added
2. Check CloudWatch logs for "Cached macro signal"
3. Verify IAM role has DynamoDB write permissions to macro-signal-cache

### No opportunities discovered

**Issue**: Scanner runs but macro-opportunities table stays empty
**Fix**:
1. Check scanner logs: "Processing signal: fed_signal=..."
2. Verify signal strength >= 0.50 and confidence >= 0.65
3. Check Kalshi market availability (may be no open markets matching keywords)
4. Verify `carpet_bagger.kalshi_client` is working (check for API errors)

### Positions not executing

**Issue**: Opportunities exist but positions don't open
**Fix**:
1. Check monitor Phase A logs: "Executing pending opportunities"
2. Verify `MACRO_TRADER_ENABLED=true`
3. Check Kalshi API errors in logs (balance, market closed, order rejection)
4. Verify DynamoDB write permissions to macro-positions table

### P&L summary not sent

**Issue**: Daily 8pm ET summary not appearing
**Fix**:
1. Monitor runs at correct time (check EventBridge rule triggers)
2. Check `_is_pnl_summary_time()` logic (must be 8pm ET ±1 hour)
3. Verify SNS topic is configured and has subscribers

---

## Next Steps

Once enabled and validated:

1. **A/B test strategies**: Create multiple "signal readers" with different probability models
2. **Risk management**: Add per-signal exposure caps and drawdown limits
3. **Signal enhancement**: Blend macro_trader signals with other data (volatility, technicals)
4. **Market expansion**: Add more Kalshi series (crypto, sports, commodities)
5. **Execution optimization**: Add market order logic, improve position sizing

---

## Questions?

Reference the `macro_trader/` source code for detailed implementation. Each file has docstrings explaining logic.

Key files:
- `strategy.py`: All thresholds and mappings (start here for tuning)
- `models.py`: Data structures and DynamoDB serialization
- `signal_reader.py`: Signal caching and validation logic
- `scanner.py`: Market discovery and opportunity creation
- `monitor.py`: Execution, position management, P&L tracking
