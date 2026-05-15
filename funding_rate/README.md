# Funding Rate Arbitrage Module

**NEW:** Production-ready AWS Lambda module for delta-neutral funding rate arbitrage on Coinbase Advanced Trade INTX perpetual futures.

## Overview

This module exploits funding rate payments on Coinbase perpetual futures by maintaining a delta-neutral position:
- **Long:** Spot asset (e.g., BTC-USD)
- **Short:** Equal notional perpetual contract (e.g., BTC-PERP-INTX)

Since the two legs cancel each other's price exposure, the strategy is purely profit from **funding payments** paid every 8 hours.

### Why Funding Rate Arbitrage?

When a market is trending up (more longs than shorts), long traders must pay a "funding rate" to short traders to incentivize shorts. Coinbase pays this funding 3 times per day (every 8 hours). By holding both sides, you're delta-neutral but collect the funding.

**Example:**
- Current funding rate: 0.03% per 8 hours
- Annualized: 0.03% × 3 × 365 = 32.85% APR
- Deploy $100 in each leg
- Collect: $100 × 0.03% = $0.03 per 8-hour period
- Collect: ~$2.70 per month (all else equal)

## Architecture

### Components

| Component | Purpose | Schedule |
|-----------|---------|----------|
| `scanner.py` | Discovers funding opportunities | Every 4 hours (EventBridge) |
| `monitor.py` | Executes trades & manages positions | Every 1 hour (EventBridge) |
| `coinbase_client.py` | Async REST client with JWT auth | On-demand |
| `models.py` | Data structures (Position, Opportunity) | — |
| `strategy.py` | Configuration & calculations | — |

### State Management

All state is persisted in **DynamoDB**:

| Table | Key | Purpose |
|-------|-----|---------|
| `funding-rate-opportunities` | `perp_ticker` + `scanned_at` | Pending opportunities |
| `funding-rate-positions` | `position_id` | Active & closed positions |

### AWS Resources (via Terraform)

- **DynamoDB:** Two tables for opportunities and positions
- **Lambda:** Two functions (scanner & monitor)
- **EventBridge:** Two rules (every 4h and 1h)
- **SNS:** Alert integration (shared `SENTINEL_SNS_ARN`)
- **SQS:** Dead-letter queue for failed invocations
- **CloudWatch:** Alarms for errors

## Installation

### 1. Prerequisites

- Python 3.11+
- AWS account with permissions for Lambda, DynamoDB, EventBridge, SNS
- Coinbase Advanced Trade account with INTX (perpetual) access
- EC P-256 API key from Coinbase

### 2. Coinbase Setup

1. Generate an **EC P-256 API key** in Coinbase Cloud:
   - Settings → API Keys → Create new key
   - Ensure permissions include: `orders:create`, `orders:read`, `accounts:read`, `products:read`
   - Copy the **private key (PEM format)** and **key identifier**

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Environment Setup

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
COINBASE_API_KEY_NAME=organizations/xxx/apiKeys/yyy
COINBASE_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----
...
-----END EC PRIVATE KEY-----"
FUNDING_RATE_ENABLED=true
SENTINEL_SNS_ARN=arn:aws:sns:...
```

### 5. Deploy to AWS

#### Using Terraform

```bash
terraform init
terraform plan -var="sentinel_sns_topic_arn=arn:aws:sns:..."
terraform apply
```

#### Manual Deployment

1. **Build Lambda packages:**
   ```bash
   cd lambda
   zip -r lambda_scanner.zip .
   zip -r lambda_monitor.zip .
   ```

2. **Create DynamoDB tables:**
   ```bash
   aws dynamodb create-table \
     --table-name funding-rate-opportunities \
     --attribute-definitions \
       AttributeName=perp_ticker,AttributeType=S \
       AttributeName=scanned_at,AttributeType=S \
     --key-schema \
       AttributeName=perp_ticker,KeyType=HASH \
       AttributeName=scanned_at,KeyType=RANGE \
     --billing-mode PAY_PER_REQUEST

   aws dynamodb create-table \
     --table-name funding-rate-positions \
     --attribute-definitions AttributeName=position_id,AttributeType=S \
     --key-schema AttributeName=position_id,KeyType=HASH \
     --billing-mode PAY_PER_REQUEST
   ```

3. **Upload Lambda functions & create EventBridge rules** (see `terraform.tf` for IAM & rule config)

## Configuration

### Strategy Parameters

Edit `strategy.py` or override via environment:

```python
MIN_FUNDING_APR = 0.10          # 10% — only enter if rate annualizes to >10%
EXIT_FUNDING_APR = 0.05         # 5% — exit when rate drops below 5%
MAX_POSITION_USD = 100.00       # Max notional per position
MAX_SIMULTANEOUS_PAIRS = 3      # Max open positions across all pairs
MAX_PCT_BALANCE = 0.30          # Max 30% of spot balance per position
REBALANCE_THRESHOLD = 0.02      # Rebalance if legs drift >2%
MAX_HOLD_DAYS = 30              # Auto-exit after 30 days
```

### Supported Pairs

```python
PERP_PAIRS = {
    "BTC-PERP-INTX": "BTC-USD",   # Bitcoin
    "ETH-PERP-INTX": "ETH-USD",   # Ethereum
    "SOL-PERP-INTX": "SOL-USD",   # Solana
}
```

Add more pairs by extending `PERP_PAIRS` dict.

## Operation

### Scanner (Every 4 Hours)

```python
from funding_rate.scanner import FundingRateScanner
from funding_rate.coinbase_client import CoinbaseClient
import asyncio

client = CoinbaseClient(
    api_key_name="organizations/xxx/apiKeys/yyy",
    private_key_pem="-----BEGIN...",
)

scanner = FundingRateScanner(client)
results = asyncio.run(scanner.run(sns_topic_arn="arn:aws:sns:..."))
```

**Actions:**
1. For each pair in `PERP_PAIRS`:
   - Fetch current 8hr funding rate from Coinbase
   - Check if we already have an open position (skip if yes)
   - If rate > `MIN_FUNDING_APR`, write opportunity to DynamoDB
2. Publish SNS summary

**Output:** Creates "pending" opportunities in `funding-rate-opportunities` table

### Monitor (Every 1 Hour)

```python
from funding_rate.monitor import FundingRateMonitor
import asyncio

monitor = FundingRateMonitor(client)
results = asyncio.run(monitor.run(sns_topic_arn="arn:aws:sns:..."))
```

**Phase A — Execute Pending Opportunities:**
1. For each "pending" opportunity:
   - Re-check funding rate (is it still above threshold?)
   - Get current spot price & available balance
   - Calculate position size (min of `MAX_POSITION_USD`, `MAX_PCT_BALANCE` × balance)
   - Place spot BUY (market order)
   - Place perp SHORT (market order, same notional)
   - Wait for both fills (up to 30s)
   - Write `FundingPosition` to `funding-rate-positions` table
   - SNS alert: "POSITION OPENED: BTC @ 12.5% APR, $100 deployed"

**Phase B — Monitor Open Positions:**

For each open position:
1. Fetch current funding rate
2. Record funding payment if 8h has passed since entry/last payment
3. Check exit conditions:
   - **A.** If rate < `EXIT_FUNDING_APR` → close position
   - **B.** If position age > `MAX_HOLD_DAYS` → close position
   - **C.** If spot/perp legs drift > `REBALANCE_THRESHOLD` → rebalance
4. On close:
   - Place spot SELL
   - Place perp BUY-to-close (close short)
   - Calculate P&L (funding collected − spread costs)
   - Update DynamoDB with final state
   - SNS alert: "POSITION CLOSED: BTC — Collected $X over Y days (Z% APR)"

**Output:** Opens positions, records funding, rebalances, and closes positions

## Data Models

### FundingPosition

```python
@dataclass
class FundingPosition:
    position_id: str              # UUID (primary key)
    perp_ticker: str              # e.g., "BTC-PERP-INTX"
    spot_ticker: str              # e.g., "BTC-USD"

    entry_spot_price: float       # Fill price for spot buy
    entry_perp_price: float       # Fill price for perp short
    entry_funding_rate_8hr: float # 8hr rate at entry
    entry_funding_apr: float      # Annualized APR at entry

    notional_usd: float           # USD per leg
    spot_quantity: float          # Base units held
    perp_contracts: float         # Perp contracts short

    spot_order_id: str            # Order ID for spot buy
    perp_order_id: str            # Order ID for perp short

    status: str                   # "open" | "rebalancing" | "closing" | "closed"
    opened_at: str                # ISO-8601 timestamp
    closed_at: Optional[str]

    funding_collected_usd: float  # Cumulative funding payments
    funding_payments_count: int   # Number of 8h periods paid
    last_funding_at: Optional[str]

    realized_pnl: float           # P&L on close
    exit_reason: Optional[str]    # Why we exited
```

### FundingOpportunity

```python
@dataclass
class FundingOpportunity:
    perp_ticker: str              # e.g., "BTC-PERP-INTX"
    spot_ticker: str              # e.g., "BTC-USD"
    scanned_at: str               # ISO-8601 scan timestamp (sort key)
    funding_rate_8hr: float       # Current 8hr rate
    funding_apr: float            # Annualized APR
    status: str                   # "pending" | "executed" | "stale" | "error"
    spot_price: Optional[float]   # Current spot price
    perp_price: Optional[float]   # Current perp price
```

## Important Notes

### Delta Neutrality

**CRITICAL:** The entire strategy depends on delta neutrality. If one leg fails to fill, the other must be cancelled immediately. Never hold one leg alone — that's a directional bet, not arbitrage.

Monitor.py implements this by:
1. Placing both orders simultaneously
2. Polling for fills with a 30-second timeout
3. If either order doesn't fill, abandoning the trade and not creating a position

### Funding Payment Tracking

Coinbase doesn't send explicit "funding paid" events. We track it based on the 8-hour schedule:
- Record payment if `now - last_funding_at >= 8 hours`
- Payment amount = `notional_usd × entry_funding_rate_8hr`

To verify actual payments, check your account balance delta in Coinbase.

### Rebalancing

Over time, price moves cause the two legs to drift. If drift > 2%, we rebalance the smaller leg to match the larger. This keeps us delta-neutral.

### Spread Costs

The main cost of entry is the **bid-ask spread** on both spot and perp markets. We use market orders for fast fills, which is more expensive than limit orders but ensures both legs fill nearly simultaneously.

## Testing

```bash
# Run unit tests
pytest tests/

# Run with coverage
pytest --cov=funding_rate tests/

# Test async functions
pytest -v -s tests/test_monitor.py::test_execute_pending_opportunities
```

Example test:

```python
import pytest
from funding_rate.strategy import annualize_funding_rate, is_worth_entering

def test_annualize_funding_rate():
    # 0.03% per 8h = ~32.85% APR
    rate_8hr = 0.0003
    apr = annualize_funding_rate(rate_8hr)
    assert 0.30 < apr < 0.35  # ~32.85%

def test_is_worth_entering():
    # Below threshold
    assert not is_worth_entering(0.00001)  # ~0.6% APR

    # Above threshold
    assert is_worth_entering(0.0003)  # ~32.85% APR
```

## Monitoring & Alerts

All major events are logged to CloudWatch and sent to SNS:

| Event | Alert |
|-------|-------|
| Opportunity found | "FUNDING OPPORTUNITY: BTC @ 12.5% APR" |
| Position opened | "POSITION OPENED: BTC, $100 deployed" |
| Funding payment recorded | (Debug log only) |
| Position rebalanced | (Debug log only) |
| Position closed | "POSITION CLOSED: BTC — Collected $X" |
| Error | "ERROR: [message]" |

View logs:
```bash
# Stream scanner logs
aws logs tail /aws/lambda/funding-rate-scanner --follow

# Stream monitor logs
aws logs tail /aws/lambda/funding-rate-monitor --follow
```

## Troubleshooting

### "Authentication failed (401)"

- Check `COINBASE_API_KEY_NAME` format
- Verify EC private key is valid PEM format (no missing newlines)
- Ensure key has `orders:create`, `orders:read` permissions in Coinbase

### "Rate limited (429)"

- Scanner/monitor will backoff exponentially
- Check Coinbase API usage dashboard
- Reduce frequency if hitting limits

### "Order fill timeout"

- Market conditions are too volatile
- Spread is too wide; try during lower-volatility times
- Check order logs in Coinbase for partial fills

### "Drift check failed"

- Network timeout or Coinbase API error
- Monitor will retry on next cycle

## Performance & Costs

### Lambda Execution

- **Scanner:** ~5s, 128 MB
- **Monitor:** ~10-20s, 256 MB (depends on # of open positions)

Estimated cost: **~$5/month** for scanner + monitor at current AWS Lambda pricing.

### DynamoDB

- On-demand pricing: **~$0.25/million requests**
- Estimated 3 positions × 24 monitor runs/day × 30 days = 2,160 reads/month (~$0.0005)

### Network

- ~50 API calls per scanner run, ~100 per monitor run
- Negligible cost (no data transfer charges for API calls)

## Future Enhancements

- [ ] Support Limit orders instead of market orders (reduce spread cost)
- [ ] Multiple wallets/portfolios
- [ ] Pair-specific parameters (different MIN_APR per pair)
- [ ] Historical P&L dashboard (CloudWatch dashboard + S3)
- [ ] Slippage optimization (order splitting, TWAP)
- [ ] Manual position override (force close, manual entry)

## Security

- **Credentials:** Store in AWS Secrets Manager, not .env in production
- **Permissions:** Lambda role has minimal IAM (DynamoDB, SNS, CloudWatch only)
- **API Key:** EC P-256 private key never logged; access only via Secrets Manager
- **Network:** All traffic to Coinbase is over HTTPS with JWT auth

## Integration with Other Modules

This module follows the same patterns as `carpet_bagger` and `bracket_buster`:

- **DynamoDB:** One table per module
- **SNS:** Shared alert topic (`SENTINEL_SNS_ARN`)
- **EventBridge:** Independent schedules per module
- **Lambda:** Independent functions per module
- **Config:** Environment variables + `.env`

Example: Disable `funding_rate` without affecting others:

```bash
export FUNDING_RATE_ENABLED=false
```

## References

- [Coinbase Advanced Trade API Docs](https://docs.cloud.coinbase.com/advanced-trade-api/)
- [INTX Perpetual Futures Guide](https://www.coinbase.com/en-us/exchange/intx/)
- [Funding Rates Explained](https://www.investopedia.com/funding-rates-cryptocurrency-6743086)

---

**Status:** Production-ready (v1.0)
**Last Updated:** 2026-04-03
**Python:** 3.11+
**AWS Services:** Lambda, DynamoDB, EventBridge, SNS, Secrets Manager
