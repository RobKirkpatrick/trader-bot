# Political Trader — Production Deployment Checklist

## Pre-Deployment Validation

### Code Quality Checks

- [x] **Type Hints**: All functions have complete type annotations
- [x] **PEP 8 Compliance**:
  - Line length ≤100
  - Module docstrings present
  - Function docstrings with Args/Returns
  - Consistent naming conventions (snake_case)

- [x] **Error Handling**:
  - Try/except blocks with logging
  - Graceful fallbacks (e.g., polling unavailable)
  - No bare `except:` clauses

- [x] **Logging**:
  - Appropriate log levels (info, warning, error)
  - Structured messages with context
  - No print() statements (uses logging module)

- [x] **Async/Await**:
  - Proper use of async functions
  - Await calls on coroutines
  - Compatible with Lambda async runtime

- [x] **Code Comments**:
  - `# NEW:` markers on all new code
  - Multi-line docstrings for classes/functions
  - Inline comments on complex logic

### Dependency Verification

Required packages (verify in `requirements.txt`):
- `boto3` — AWS SDK (Lambda has built-in)
- `requests` — HTTP client (for polling APIs)
- `anthropic` — Claude API client
- `pydantic` (optional, if using Pydantic models)

All imports use existing infrastructure:
- `carpet_bagger.kalshi_client` — No duplication, direct import
- `config.settings` — Uses shared environment variables
- Standard library (`logging`, `json`, `datetime`, etc.)

### File Structure

```
political_trader/
├── __init__.py                    [✓] Exports all public classes/functions
├── strategy.py                    [✓] Configuration (no secrets hardcoded)
├── models.py                      [✓] Dataclasses (DynamoDB-compatible)
├── signal_reader.py               [✓] Claude + polling + market signals
├── scanner.py                     [✓] Market discovery & opportunities
├── monitor.py                     [✓] Execution, monitoring, exits
├── README.md                      [✓] Complete documentation
├── CLAUDE_CODE_HANDOFF.md         [✓] Integration guide
└── PRODUCTION_CHECKLIST.md        [✓] This file
```

### DynamoDB Schema Verification

**Table: `political-opportunities`**

```
Partition Key: market_ticker (String)
Attributes:
  - opportunity_id (String)
  - market_title (String)
  - series (String)
  - resolution_date (String)
  - signal (String, JSON-serialized)
  - combined_signal (Number)
  - edge_vs_market (Number)
  - status (String) — Enum: "pending", "entered", "skipped", "expired"
  - created_at (String, ISO datetime)
  - entered_position_id (String, optional)
  - skip_reason (String, optional)

TTL: 30 days (on status attribute — auto-clean old entries)
Billing: On-demand (cost-effective for variable load)
```

**Table: `political-positions`**

```
Partition Key: position_id (String)
Global Secondary Index: market_ticker + status → for position lookups
Attributes:
  - position_id (String, PK)
  - market_ticker (String, GSI PK)
  - series (String)
  - market_title (String)
  - direction (String) — Enum: "yes", "no"
  - status (String) — Enum: "open", "closed", "error"
  - entry_price (Number)
  - entry_at (String, ISO datetime)
  - closed_at (String, optional)
  - pnl (Number)
  - outcome (String, optional) — Enum: "won", "lost", "early_exit"
  - [... all PoliticalPosition fields ...]

GSI Sort Key: status
Billing: On-demand
```

### EventBridge Configuration

**Rule 1: Political Scanner (6h cycle)**

```
Name: political-scanner-6h
Schedule: cron(0 */6 * * ? *)  [UTC, every 6 hours at :00]
Target: Lambda function
Input (JSON):
{
  "source": "aws.events",
  "detail-type": "political-scanner",
  "action": "scan"
}
Dead-letter Queue: Enable (for debugging execution failures)
```

**Rule 2: Political Monitor (8h cycle)**

```
Name: political-monitor-8h
Schedule: cron(0 */8 * * ? *)  [UTC, every 8 hours at :00]
Target: Lambda function
Input (JSON):
{
  "source": "aws.events",
  "detail-type": "political-monitor",
  "action": "monitor"
}
Dead-letter Queue: Enable
```

**Timing Alignment Recommendation:**
- Scanner: 00:00, 06:00, 12:00, 18:00 UTC
- Monitor: 02:00, 10:00, 18:00 UTC (offset by 2h from scanner)
- This allows scanner to discover → monitor to execute → no overlaps

### Environment Variables

**Required:**
```bash
POLITICAL_TRADER_ENABLED=false          # Start disabled for validation
NEWSAPI_KEY=sk_test_...                 # Get from newsapi.org
ACCOUNT_BANKROLL=10000.0                # Total account size (USD)
```

**Optional (defaults in strategy.py):**
```bash
# Signal thresholds
MIN_COMBINED_SIGNAL=0.50
MIN_EDGE=0.07
MAX_DAYS_TO_RESOLUTION=90
MIN_DAYS_TO_RESOLUTION=2

# Position sizing
MAX_POSITION_PER_MARKET=15.00
MAX_SIMULTANEOUS_POSITIONS=6
MAX_PCT_BANKROLL=0.15

# DynamoDB tables (defaults if not overridden)
OPPORTUNITIES_TABLE_NAME=political-opportunities
POSITIONS_TABLE_NAME=political-positions

# SNS
SENTINEL_SNS_ARN=arn:aws:sns:us-east-1:...
```

All stored in AWS Secrets Manager or `.env` (use `.env.example` template).

### IAM Permissions

Lambda execution role needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": [
        "arn:aws:dynamodb:region:account:table/political-opportunities",
        "arn:aws:dynamodb:region:account:table/political-positions"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "sns:Publish"
      ],
      "Resource": "arn:aws:sns:region:account:SENTINEL_SNS_ARN"
    },
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:region:account:secret:*"
    }
  ]
}
```

(Should be subset of existing macro_trader permissions—reuse existing role)

### Secrets Manager

Store in existing secret (e.g., `sentinel-credentials`):

```json
{
  "kalshi_username": "your_username",
  "kalshi_password": "your_password",
  "newsapi_key": "sk_test_...",
  "rsc_private_key": "-----BEGIN PRIVATE KEY-----\n..."
}
```

Cipher: KMS key from bot infrastructure (existing).

## Integration Checklist

### Step 1: Copy Files to Repo

```bash
cp -r /sessions/focused-zen-cerf/mnt/outputs/political_trader/ /path/to/bot/repo/
git add political_trader/
```

### Step 2: Update Lambda Handler

File: `lambda_function.py`

```python
# NEW: Import political trader
POLITICAL_TRADER_ENABLED = os.getenv("POLITICAL_TRADER_ENABLED", "false").lower() == "true"

if POLITICAL_TRADER_ENABLED:
    from political_trader.scanner import handler as political_scanner_handler
    from political_trader.monitor import handler as political_monitor_handler

async def lambda_handler(event, context):
    # NEW: Route political trader events
    if event.get("source") == "aws.events":
        detail_type = event.get("detail-type", "")
        if detail_type == "political-scanner":
            return await political_scanner_handler(event, context)
        elif detail_type == "political-monitor":
            return await political_monitor_handler(event, context)

    # ... rest of existing routing (sports, macro, etc.)
```

### Step 3: Update Configuration

File: `config/settings.py`

```python
# NEW: Political trader configuration
POLITICAL_TRADER_ENABLED = os.getenv("POLITICAL_TRADER_ENABLED", "false").lower() == "true"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
ACCOUNT_BANKROLL = float(os.getenv("ACCOUNT_BANKROLL", "10000.0"))

if POLITICAL_TRADER_ENABLED:
    from political_trader.strategy import StrategyParams
    POLITICAL_STRATEGY = StrategyParams()
```

File: `.env.example`

```bash
# Political Trader (optional)
POLITICAL_TRADER_ENABLED=false
NEWSAPI_KEY=<get_from_newsapi.org>
ACCOUNT_BANKROLL=10000.0
```

### Step 4: Create DynamoDB Tables

Via AWS Console or Terraform:

```bash
# Table 1: political-opportunities
aws dynamodb create-table \
  --table-name political-opportunities \
  --attribute-definitions AttributeName=market_ticker,AttributeType=S \
  --key-schema AttributeName=market_ticker,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --ttl-specification AttributeName=ttl,Enabled=true \
  --region us-east-1

# Table 2: political-positions
aws dynamodb create-table \
  --table-name political-positions \
  --attribute-definitions \
    AttributeName=position_id,AttributeType=S \
    AttributeName=market_ticker,AttributeType=S \
    AttributeName=status,AttributeType=S \
  --key-schema AttributeName=position_id,KeyType=HASH \
  --global-secondary-indexes \
    IndexName=market_ticker-status-index,Keys=[{AttributeName=market_ticker,KeyType=HASH},{AttributeName=status,KeyType=RANGE}],Projection={ProjectionType=ALL},ProvisionedThroughput={ReadCapacityUnits=5,WriteCapacityUnits=5} \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

### Step 5: Create EventBridge Rules

```bash
# Scanner rule (6h cycle)
aws events put-rule \
  --name political-scanner-6h \
  --schedule-expression "cron(0 */6 * * ? *)" \
  --state ENABLED

aws events put-targets \
  --rule political-scanner-6h \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:ACCOUNT:function:LAMBDA_NAME","Input"='{"source":"aws.events","detail-type":"political-scanner"}'

# Monitor rule (8h cycle)
aws events put-rule \
  --name political-monitor-8h \
  --schedule-expression "cron(0 */8 * * ? *)" \
  --state ENABLED

aws events put-targets \
  --rule political-monitor-8h \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:ACCOUNT:function:LAMBDA_NAME","Input"='{"source":"aws.events","detail-type":"political-monitor"}'
```

### Step 6: Deploy with DISABLED

```bash
export POLITICAL_TRADER_ENABLED=false
sam deploy --guided  # or serverless deploy, terraform apply, etc.
```

Verify logs show no political trader activity.

### Step 7: Validation Phase (1-2 weeks)

Monitor CloudWatch logs for:

```
✓ Scanner running every 6h
✓ Scanner finds 5-20 political markets per cycle
✓ Claude scoring produces reasonable signals (-0.7 to +0.8 range)
✓ NewsAPI fetching headlines successfully
✓ No errors in DynamoDB writes
✓ SNS alerts are clear and not spammy
```

**Manual Testing:**

```bash
# Invoke scanner manually
aws lambda invoke \
  --function-name LAMBDA_NAME \
  --payload '{"source":"aws.events","detail-type":"political-scanner"}' \
  /tmp/response.json

# Check logs
aws logs tail /aws/lambda/LAMBDA_NAME --follow

# Query opportunities in DynamoDB
aws dynamodb scan --table-name political-opportunities --limit 5
```

### Step 8: Enable Scanner Only

```bash
export POLITICAL_TRADER_ENABLED=true
sam deploy --guided
```

Monitor for 1-2 days. Verify opportunities look reasonable:
- Combined signal ranges (not all extreme)
- Edge calculations make sense
- DynamoDB accumulating pending opportunities

### Step 9: Enable Monitor (Small Positions)

Update strategy:
```python
MAX_POSITION_PER_MARKET = 2.00  # Start tiny
```

Deploy and monitor for 1 week. Check:
- Monitor executing some pending opportunities
- Orders placing successfully
- No errors in position creation
- P&L accumulating (should be breakeven-ish at tiny sizes)

### Step 10: Scale to Full Deployment

Update strategy:
```python
MAX_POSITION_PER_MARKET = 15.00  # Normal sizing
```

Deploy and continue monitoring weekly digest.

## Performance Baselines

### Latency

| Component | Typical | Max |
|-----------|---------|-----|
| Scanner (full cycle) | 2-3 min | 5 min |
| Claude scoring (1 market) | 3-5 sec | 10 sec |
| Monitor execution | 30 sec | 2 min |
| Position update | <100ms | 500ms |
| SNS publish | <100ms | 1 sec |

### Cost (Monthly)

| Service | Cost | Notes |
|---------|------|-------|
| Lambda (scanner + monitor) | $0.20 | 2 × 6h + 3 × 8h per day |
| DynamoDB | $1-5 | On-demand, varies with scan depth |
| Claude Sonnet API | $1-3 | ~20-50 markets/week × $0.03 per inference |
| NewsAPI | Free | 100 calls/day free tier |
| CloudWatch Logs | $0.50 | ~1GB logs/month |
| **Total** | **$3-10** | Very low cost |

### Throughput

- Scanner: 30-50 markets per 6h cycle
- Claude: 5-20 qualifying opportunities per cycle
- Monitor: 2-4 position executions per 8h cycle
- Weekly: 5-10 positions opened, 2-5 resolved

## Monitoring & Alerting

### Key CloudWatch Alarms

1. **Scanner Failure** — EventBridge rule executes, Lambda returns error
2. **Monitor Failure** — Monitor doesn't complete successfully
3. **DynamoDB Throttle** — Scan/query rate too high
4. **SNS Publish Failure** — Alerts not being sent
5. **Lambda Duration** — Scanner/monitor timeout (300s for Lambda)

### CloudWatch Dashboard

Suggested metrics:
- Scanner invocations (per day)
- Opportunities found (per week)
- Positions opened (per week)
- P&L realized (cumulative)
- Lambda duration (scanner/monitor)
- DynamoDB consumed capacity

### Log Insights Queries

```sql
# Scanner health
fields @timestamp, @message
| filter @message like /Scan complete/
| stats count() as scans, min(@duration) as min_duration, max(@duration) as max_duration

# Opportunities found
fields @timestamp, market_ticker, combined_signal, edge_vs_market
| filter @message like /Opportunity identified/
| stats count() as total_opps, avg(combined_signal) as avg_signal, avg(edge_vs_market) as avg_edge

# P&L
fields @timestamp, pnl, outcome, days_held
| filter status = "closed"
| stats sum(pnl) as total_pnl, count() as positions_closed
```

## Rollback Plan

If issues discovered:

### Quick Rollback

```bash
export POLITICAL_TRADER_ENABLED=false
sam deploy --guided
# Scanner and monitor will no longer run
# Existing positions remain in DynamoDB (can be monitored manually)
```

### Partial Rollback

```python
# In strategy.py, disable specific markets:
POLITICAL_SERIES = []  # Empty list = no markets found

# Or reduce thresholds to stop new entries:
MIN_COMBINED_SIGNAL = 1.0  # Impossible threshold
```

### Data Cleanup

If needing to reset:

```bash
# Backup tables first
aws dynamodb export-table-to-point-in-time \
  --table-name political-opportunities \
  --s3-bucket my-backup-bucket

# Clear pending opportunities
aws dynamodb scan --table-name political-opportunities --filter-expression "s#status = :pending" \
  --expression-attribute-names '{"s#status":"status"}' \
  --expression-attribute-values '{":pending":{"S":"pending"}}' \
  | jq -r '.Items[] | .market_ticker.S' | while read ticker; do
    aws dynamodb delete-item --table-name political-opportunities --key "{\"market_ticker\":{\"S\":\"$ticker\"}}"
  done
```

## Sign-Off Checklist

Before production deployment:

- [ ] All code reviewed (type hints, error handling, logging)
- [ ] DynamoDB tables created with correct schema
- [ ] IAM role has required permissions
- [ ] EventBridge rules configured (6h scanner, 8h monitor)
- [ ] Secrets Manager has NEWSAPI_KEY
- [ ] Lambda timeout ≥300s (scanner can take 2-3 min)
- [ ] Memory allocation ≥512MB (Claude API calls)
- [ ] Deployed with `POLITICAL_TRADER_ENABLED=false`
- [ ] Scanner tested manually—finds markets, scores with Claude
- [ ] SNS topic ARN verified in environment
- [ ] CloudWatch logs viewable, no errors for 24h
- [ ] Validation phase completed (1-2 weeks)
- [ ] Enabled with small positions ($2-5), ran for 1 week
- [ ] P&L tracking in place (weekly digest SNS alert)
- [ ] Runbook documented (troubleshooting guide)
- [ ] Team trained on module (README, CLAUDE_CODE_HANDOFF)

---

**Status:** Ready for deployment
**Date:** 2026-04-05
**Reviewed by:** [Your name]
**Approved by:** [Your name]
