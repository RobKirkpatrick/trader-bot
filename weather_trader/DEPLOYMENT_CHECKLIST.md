# Weather Trader Deployment Checklist

Complete this checklist before enabling live trading with `WEATHER_TRADER_ENABLED=true`.

## Pre-Deployment Setup

- [ ] Copy `weather_trader/` directory to repository root
- [ ] Verify all Python files compile: `python3 -m py_compile weather_trader/*.py`
- [ ] Review strategy parameters in `strategy.py` (MIN_EDGE, MAX_POS, timing, etc.)
- [ ] Update `config/settings.py` with weather trader configuration variables
- [ ] Update `.env.example` with weather trader settings
- [ ] Create `.env` entry with `WEATHER_TRADER_ENABLED=false` initially

## AWS Infrastructure

### DynamoDB Tables

- [ ] Create `weather-opportunities` table
  ```bash
  aws dynamodb create-table \
    --table-name weather-opportunities \
    --attribute-definitions AttributeName=opportunity_id,AttributeType=S \
    --key-schema AttributeName=opportunity_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  ```

- [ ] Create `weather-positions` table
  ```bash
  aws dynamodb create-table \
    --table-name weather-positions \
    --attribute-definitions AttributeName=position_id,AttributeType=S \
    --key-schema AttributeName=position_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  ```

- [ ] (Optional) Create `nws-forecast-cache` table with TTL
  ```bash
  aws dynamodb create-table \
    --table-name nws-forecast-cache \
    --attribute-definitions AttributeName=cache_key,AttributeType=S \
    --key-schema AttributeName=cache_key,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST

  aws dynamodb update-time-to-live \
    --table-name nws-forecast-cache \
    --time-to-live-specification AttributeName=expires_at,Enabled=true
  ```

### Lambda & EventBridge

- [ ] Create Lambda function for `weather_scanner_handler`
- [ ] Create Lambda function for `weather_monitor_handler`
- [ ] Grant Lambda execution role access to DynamoDB tables (read/write)
- [ ] Grant Lambda execution role access to SNS (publish to SENTINEL_SNS_ARN)
- [ ] Create EventBridge rule `weather-scanner` (schedule: `rate(4 hours)`)
- [ ] Create EventBridge rule `weather-monitor` (schedule: `rate(6 hours)`)
- [ ] Attach Lambda targets to EventBridge rules
- [ ] Test Lambda functions manually:
  ```bash
  aws lambda invoke --function-name weather-scanner --payload '{}' /tmp/output.json
  cat /tmp/output.json
  ```

### IAM Permissions

- [ ] Lambda execution role has `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`
- [ ] Lambda execution role has `dynamodb:Scan` on weather-opportunities and weather-positions
- [ ] Lambda execution role has `sns:Publish` on SENTINEL_SNS_ARN
- [ ] Lambda has permission to call Kalshi client (if using role-based auth)

## Code Integration

- [ ] Import `weather_trader` in `lambda_function.py`:
  ```python
  from weather_trader import run_scanner, run_monitor
  ```

- [ ] Create scanner handler:
  ```python
  async def weather_scanner_handler(event, context):
      if not WEATHER_TRADER_ENABLED:
          return {"status": "disabled"}
      import boto3
      return await run_scanner(kalshi_client, boto3.resource("dynamodb"), boto3.client("sns"))
  ```

- [ ] Create monitor handler:
  ```python
  async def weather_monitor_handler(event, context):
      if not WEATHER_TRADER_ENABLED:
          return {"status": "disabled"}
      import boto3
      return await run_monitor(kalshi_client, boto3.resource("dynamodb"), boto3.client("sns"))
  ```

- [ ] Verify Kalshi client is correctly instantiated and passed to handlers
- [ ] Test handlers locally with sample event:
  ```python
  import asyncio
  result = asyncio.run(weather_scanner_handler({}, None))
  print(result)
  ```

## Validation Phase (3-5 days, WEATHER_TRADER_ENABLED=false)

### Day 1-2: NWS API Validation

- [ ] Run scanner manually, check CloudWatch logs
- [ ] Verify NWS API calls succeed (no 404/timeout errors)
- [ ] Spot-check 3-5 NWS forecasts against weather.gov web UI
  - Go to https://www.weather.gov/
  - Search NYC (or another city in CITY_COORDS)
  - Compare precip probability with what bot reports in SNS

- [ ] Verify log output shows:
  ```
  INFO: Found N potential weather markets
  INFO: Opportunity found: MARKET_TICKER — city weather_type @ side (NWS: X%, edge: +$Y)
  ```

### Day 2-3: Market Parsing Validation

- [ ] Monitor SNS alerts for market parsing accuracy
- [ ] Manually check 5-10 parsed markets:
  - Take SNS market_ticker
  - Go to Kalshi, search for that market
  - Verify parsed city/weather_type/threshold match actual market

- [ ] Check for parsing failures in CloudWatch logs:
  ```
  WARNING: Could not parse weather market: "TITLE"
  ```
  - If frequent, add regex pattern to `market_parser.py`

- [ ] Verify edge calculations make sense:
  - NWS prob should be 0-100%
  - Edge should be roughly (NWS - Kalshi ask)
  - Edges > $0.10 should be reported

### Day 3-4: Position Sizing Validation

- [ ] Verify position sizes scale reasonably with edge:
  - Small edge ($0.10-$0.15) → small position ($1-5)
  - Medium edge ($0.15-$0.25) → medium position ($5-15)
  - Large edge ($0.25+) → large position ($15-20)

- [ ] Check position count limits:
  - Never more than 8 simultaneous open opportunities
  - Respects bankroll constraints

- [ ] Manually verify 2-3 position sizes:
  ```python
  from weather_trader import (
      MIN_POSITION_SIZE, MAX_POSITION_PER_MARKET, MIN_EDGE
  )
  edge = 0.15  # Example
  edge_factor = min(1.0, (edge - MIN_EDGE) / 0.20)
  size = MIN_POSITION_SIZE + (MAX_POSITION_PER_MARKET - MIN_POSITION_SIZE) * edge_factor
  print(f"Edge ${edge:.2f} → Position ${size:.2f}")
  ```

### Day 4-5: Monitor Phase Validation

- [ ] Let monitor run for 1-2 cycles (every 6h)
- [ ] Check if any opportunities executed (should be 0 if ENABLED=false)
- [ ] Verify monitor logs show position monitoring checks:
  ```
  INFO: Found N open positions to monitor
  INFO: Phase B: Monitoring open positions...
  ```

- [ ] Manually test NWS reversal logic:
  - Take an open opportunity from DynamoDB
  - Manually fetch fresh NWS forecast
  - Verify reversal calculation is correct

## Go-Live Checklist

Only proceed if ALL validation steps passed successfully.

- [ ] All validation logs reviewed and approved
- [ ] Edge calculations match manual spot-checks
- [ ] Market parsing accuracy > 95%
- [ ] NWS API working reliably
- [ ] DynamoDB tables functioning
- [ ] SNS alerts being sent correctly
- [ ] No unhandled exceptions in logs
- [ ] Position sizing looks reasonable
- [ ] Risk/reward profile acceptable for current bankroll

### Final Steps

- [ ] Update `.env` to `WEATHER_TRADER_ENABLED=true`
- [ ] Deploy Lambda update
- [ ] Monitor SNS for first execution:
  ```
  WEATHER BET PLACED: CITY WEATHER_TYPE @ SIDE (NWS: X%, edge: +$Y)
  ```

- [ ] Check DynamoDB `weather-positions` table for new entry:
  ```bash
  aws dynamodb scan --table-name weather-positions --max-items 1
  ```

- [ ] Monitor first 3 positions to resolution
- [ ] Verify outcomes recorded correctly

## Post-Go-Live Monitoring (Weeks 1-4)

- [ ] Daily: Check SNS alerts for execution/monitoring
- [ ] Twice weekly: Review DynamoDB position table
  - Are positions staying open until resolution?
  - Are reversal exits happening as expected?

- [ ] Weekly: Pull P&L report
  ```bash
  aws dynamodb scan --table-name weather-positions \
    --filter-expression "attribute_exists(pnl)" \
    --projection-expression "market_ticker, entry_price, pnl, outcome"
  ```

- [ ] Weekly: Spot-check 2-3 resolved outcomes
  - Verify actual weather matches forecast
  - Confirm position P&L is correct

- [ ] Monitor for edge drift:
  - Is NWS still beating Kalshi prices?
  - Are edges holding as expected?

- [ ] Monitor for operational issues:
  - Lambda execution times
  - DynamoDB throttling
  - SNS delivery failures

## Rollback Plan

If issues discovered during go-live:

1. Set `WEATHER_TRADER_ENABLED=false` immediately
2. New trades will not execute (existing positions continue)
3. Monitor phase still runs to monitor existing positions
4. Investigate issue in CloudWatch logs
5. Update code if needed
6. Re-validate before re-enabling

To close all positions early:
- Extend `monitor.py` with manual exit endpoint
- Or use Kalshi web UI to close positions manually

## Post-Deployment Notes

After 1-2 weeks of successful operation:

- [ ] Document actual edge distribution (what edges are we seeing?)
- [ ] Calculate realized P&L vs. expected P&L
- [ ] Review position sizing effectiveness
- [ ] Consider adjustments to MIN_EDGE or other parameters
- [ ] Plan follow-up enhancements (wind markets, extended forecasts, etc.)

---

**Checklist Version**: 1.0
**Last Updated**: April 2026
**Author**: Claude Code
