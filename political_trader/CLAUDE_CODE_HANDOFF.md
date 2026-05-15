# Political Trader Integration Guide

## Overview

This `political_trader` package is a production-ready module for trading Kalshi political prediction markets using Claude-powered news sentiment analysis. It integrates with an existing AWS Lambda bot infrastructure and runs two autonomous processes:

1. **Scanner** (6h cycle): Discovers new political market opportunities
2. **Monitor** (8h cycle): Executes positions, monitors signals, handles exits

---

## Quick Start

### 1. Copy to Repository

Copy the `political_trader/` directory to your Lambda bot repository root:

```bash
cp -r political_trader/ /path/to/bot/repo/
```

### 2. Update Lambda Handler

Add to `lambda_function.py`:

```python
# NEW: Import political trader handlers
POLITICAL_TRADER_ENABLED = os.getenv("POLITICAL_TRADER_ENABLED", "false").lower() == "true"

if POLITICAL_TRADER_ENABLED:
    from political_trader.scanner import handler as political_scanner_handler
    from political_trader.monitor import handler as political_monitor_handler
```

Update the main handler dispatcher to route EventBridge events:

```python
async def lambda_handler(event, context):
    source = event.get("source", "")

    # NEW: Route political trader events
    if source == "aws.events" and event.get("detail-type") == "political-scanner":
        return await political_scanner_handler(event, context)
    elif source == "aws.events" and event.get("detail-type") == "political-monitor":
        return await political_monitor_handler(event, context)

    # ... rest of existing routing
```

### 3. Update Configuration

Add to `config/settings.py`:

```python
# NEW: Political Trader Configuration
POLITICAL_TRADER_ENABLED = os.getenv("POLITICAL_TRADER_ENABLED", "false").lower() == "true"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")  # Required for news sentiment
ACCOUNT_BANKROLL = float(os.getenv("ACCOUNT_BANKROLL", "10000.0"))

from political_trader.strategy import StrategyParams
POLITICAL_STRATEGY = StrategyParams()
```

Add to `.env.example`:

```bash
# Political Trader
POLITICAL_TRADER_ENABLED=false
NEWSAPI_KEY=<your_newsapi_key>
ACCOUNT_BANKROLL=10000.0
```

### 4. Create DynamoDB Tables

Create two new DynamoDB tables with these schemas:

#### `political-opportunities`

| Attribute | Type | Key |
|-----------|------|-----|
| market_ticker | String | Partition Key |
| opportunity_id | String | |
| market_title | String | |
| series | String | |
| resolution_date | String | |
| signal | String (JSON) | |
| combined_signal | Number | |
| edge_vs_market | Number | |
| status | String | |
| created_at | String | |
| entered_position_id | String | |

**Recommended settings:**
- Billing: On-demand
- TTL: status → 30 days (auto-clean old entries)

#### `political-positions`

| Attribute | Type | Key |
|-----------|------|-----|
| position_id | String | Partition Key |
| market_ticker | String | GSI Partition Key |
| status | String | |
| opened_at | String | |
| closed_at | String | |
| pnl | Number | |
| outcome | String | |

**Recommended settings:**
- Billing: On-demand
- GSI: market_ticker-status-index (for position lookups)

### 5. Create EventBridge Schedules

Create two EventBridge rules:

#### Rule: `political-scanner-6h`

**Schedule:** `cron(0 */6 * * ? *)` (every 6 hours UTC)

**Target:**
- Service: Lambda
- Function: your Lambda function
- Input (JSON):
  ```json
  {
    "source": "aws.events",
    "detail-type": "political-scanner",
    "action": "scan"
  }
  ```

#### Rule: `political-monitor-8h`

**Schedule:** `cron(0 */8 * * ? *)` (every 8 hours UTC)

**Target:**
- Service: Lambda
- Function: your Lambda function
- Input (JSON):
  ```json
  {
    "source": "aws.events",
    "detail-type": "political-monitor",
    "action": "monitor"
  }
  ```

### 6. Update Secrets Manager

If using Secrets Manager for API keys:

```json
{
  "kalshi_username": "...",
  "kalshi_password": "...",
  "newsapi_key": "...",
  "rsc_private_key": "..."
}
```

---

## Validation Checklist

Before enabling in production, validate:

1. **News Signal Quality**
   - Set `POLITICAL_TRADER_ENABLED=false` initially
   - Run scanner manually: check CloudWatch logs for signal assessment
   - Verify Claude is scoring political news sensibly (not all 0s)
   - Look for examples in SNS alerts

2. **Market Discovery**
   - Confirm scanner finds political markets in POLITICAL_SERIES
   - Verify bid-ask spread filtering works (check logs)
   - Monitor for false positives (unrelated markets tagged as political)

3. **Polling API Integration**
   - Test FiveThirtyEight/RealClearPolitics API calls
   - Verify momentum calculation (7-day lookback)
   - Check graceful fallback if polling unavailable

4. **Position Execution**
   - Dry-run: create a pending opportunity manually in DynamoDB
   - Verify monitor attempts to execute without `POLITICAL_TRADER_ENABLED`
   - Check order placement (small test position, real or live based on env)

5. **Signal + Edge Quality**
   - Review 10+ pending opportunities in DynamoDB
   - Verify combined_signal is not always extreme (calibration check)
   - Confirm edge calculations are reasonable (typical: 2-8 cents)

6. **Upstream Validation**
   - Test that existing macro_trader still works
   - Verify DynamoDB doesn't have conflicts (unique table names)
   - Check SNS alerts are not spammy (should be ~1 per 6h scan, ~1 per position)

---

## Enable Production Gradually

```bash
# Step 1: Scanner only (observe signal quality)
POLITICAL_TRADER_ENABLED=true
# Monitor SNS for 1-2 weeks, verify opportunities look good

# Step 2: Monitor + small positions
# Reduce MAX_POSITION_PER_MARKET to $2-5 for validation

# Step 3: Full deployment
# Increase position sizing per strategy.py thresholds
```

---

## Key Configuration Parameters

All in `strategy.py`:

| Parameter | Default | Notes |
|-----------|---------|-------|
| `MIN_COMBINED_SIGNAL` | 0.50 | Signal strength magnitude (0-1) |
| `MIN_EDGE` | 0.07 | Minimum edge in cents |
| `MAX_POSITION_PER_MARKET` | $15 | Per-position limit |
| `MAX_SIMULTANEOUS_POSITIONS` | 6 | Total open positions |
| `MAX_DAYS_TO_RESOLUTION` | 90 | Don't enter if resolves >90d |
| `MIN_DAYS_TO_RESOLUTION` | 2 | Don't enter if resolves <2d |
| `SIGNAL_REVERSAL_THRESHOLD` | -0.35 | Exit if signal flips this hard |
| `TRAILING_EDGE_EXIT` | 0.03 | Exit if edge compresses <3¢ |

---

## Architecture Notes

### Signal Integration

**News Sentiment** (50% weight)
- Fetches recent headlines from NewsAPI
- Claude Sonnet scores political momentum (-1.0 to +1.0)
- Confidence score indicates reliability

**Polling Momentum** (35% weight, optional)
- Attempts FiveThirtyEight and RealClearPolitics APIs
- Calculates 7-day trend
- Normalized to -1.0 to +1.0 scale

**Market Momentum** (15% weight, optional)
- Kalshi 24h price movement
- Indicates smart money flow
- Falls back gracefully if history unavailable

Combined signal is weighted average of available signals.

### Position Lifecycle

1. **Scanner discovers** → Creates `political-opportunities` record (status: pending)
2. **Monitor executes** → Places order, creates `political-positions` record (status: open)
3. **Monitor refreshes signal** → Checks exit conditions every 8h
4. **Position exits** → Via signal reversal, edge compression, or market resolution
5. **Closed position** → Recorded in DynamoDB with P&L, outcome, days held

### Hold Times & Sizing

Political markets have longer resolution windows than sports. Position sizing is conservative:

- Typical hold: 5-30 days (not hours like sports)
- Max position: $15 (vs $30-50 for sports due to longer decay risk)
- Max simultaneous: 6 (vs 10-15 for sports)
- Max bankroll allocation: 15% (vs 25% for sports)

---

## Troubleshooting

### Scanner finds no opportunities

1. Check `POLITICAL_SERIES` includes open markets
2. Verify `MIN_COMBINED_SIGNAL` isn't too high (0.50 is reasonable)
3. Check CloudWatch logs for news fetch errors
4. Validate NewsAPI key is correct

### Positions open but signal is weak

1. Review `signal_reader.py` weighting in `SIGNAL_WEIGHTS`
2. Check if polling API is failing (graceful fallback may be over-weighting news)
3. Verify Claude scoring makes sense (check logs)
4. Consider reducing `MIN_COMBINED_SIGNAL` slightly

### Monitor not executing pending opportunities

1. Check `MAX_SIMULTANEOUS_POSITIONS` limit
2. Verify position sizing calculation (may be <$1)
3. Check Kalshi client has sufficient balance
4. Review CloudWatch for order rejection reasons

### Orders failing

1. Verify liquidity exists (bid-ask spread check)
2. Check Kalshi API credentials in Secrets Manager
3. Review order limit price (may be missing the ask)
4. Check market hasn't resolved or closed

---

## Integration with Existing Bot

This module is designed for **minimal friction** with existing infrastructure:

- **Reuses** `carpet_bagger.kalshi_client` (no duplication)
- **Reuses** DynamoDB, SNS, Secrets Manager, EventBridge patterns
- **Follows** same async/await Lambda conventions
- **Logs** to CloudWatch (standard boto3 + logging module)
- **Alerts** via SNS (same as macro_trader)
- **Disabled by default** (`POLITICAL_TRADER_ENABLED=false`)

To disable without removing code:
```python
if not POLITICAL_TRADER_ENABLED:
    # Scanner and Monitor never fire
```

---

## Next Steps

1. Copy files to repo root
2. Update `lambda_function.py`, `config/settings.py`, `.env.example`
3. Create DynamoDB tables + EventBridge rules
4. Add NewsAPI key to Secrets Manager
5. Deploy with `POLITICAL_TRADER_ENABLED=false`
6. Monitor logs for 1 week
7. Enable scanner, validate opportunities
8. Enable monitor with small positions ($2-5)
9. Gradually increase position sizing

---

## Support & Monitoring

### Key Logs to Monitor

```
# Scanner healthy
"Scan complete: 3 qualifying opportunities in 12.4s"

# Monitor healthy
"Position opened: USCASEN23-D YES@0.58 signal=0.623"
"Position resolved: ... WON P&L=$4.50 (30.0%)"

# Issues
"Signal deteriorated for {ticker}; canceling execution"
"Market momentum error: {error}"
```

### SNS Alerts to Expect

- **POLITICAL SCAN SUMMARY** (every 6h)
- **POLITICAL POSITION OPENED** (when order filled)
- **POLITICAL TRADER WEEKLY DIGEST** (Sunday 8pm ET)

---

## Production Considerations

- **Cost**: DynamoDB on-demand + occasional Lambda invocations (~$5-10/month)
- **API limits**: NewsAPI (100 calls/day); FiveThirtyEight (rate-limited but public)
- **Latency**: Scanner takes ~2-3 min to assess 20-30 markets
- **Slippage**: Order execution uses limit price; may miss fast-moving opportunities
- **Regulatory**: Verify Kalshi account is in compliant jurisdiction

---

## File Structure

```
political_trader/
├── __init__.py              # Package exports
├── strategy.py              # Configuration & thresholds
├── models.py                # Data classes (PoliticalPosition, etc.)
├── signal_reader.py         # News sentiment + polling + market momentum
├── scanner.py               # Market discovery & opportunity creation
├── monitor.py               # Position execution & monitoring
└── CLAUDE_CODE_HANDOFF.md   # This file
```

All files are fully production-ready with:
- Complete type hints
- Comprehensive logging
- Error handling & graceful fallbacks
- DynamoDB serialization
- Async/await support
- PEP 8 compliance

Good luck! 🚀
