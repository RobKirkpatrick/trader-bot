# Claude Code Handoff — trader-bot funding_rate Module

## What's here

A new **`funding_rate/`** module for perpetual futures funding rate arbitrage on Coinbase Advanced.

---

## How to integrate — paste this into Claude Code

### Step 1: Clone / navigate to repo
```
cd trader-bot
```

### Step 2: Copy funding_rate into the repo
```
cp -r [path-to-outputs]/funding_rate/ ./funding_rate/
```

### Step 3: Update lambda_function.py

Add these imports at the top:
```python
from funding_rate import scanner, monitor
```

Wire up the EventBridge handlers:
```python
def funding_rate_scanner_handler(event, context):
    """4-hour schedule for scanning opportunities."""
    if not settings.FUNDING_RATE_ENABLED:
        return {"statusCode": 200, "body": "funding_rate_scanner disabled"}
    return scanner.run()

def funding_rate_monitor_handler(event, context):
    """1-hour schedule for monitoring positions."""
    if not settings.FUNDING_RATE_ENABLED:
        return {"statusCode": 200, "body": "funding_rate_monitor disabled"}
    return monitor.run()
```

Add these EventBridge triggers to `serverless.yml`:
```yaml
  funding_rate_scanner:
    handler: lambda_function.funding_rate_scanner_handler
    events:
      - schedule: rate(4 hours)

  funding_rate_monitor:
    handler: lambda_function.funding_rate_monitor_handler
    events:
      - schedule: rate(1 hour)
```

### Step 4: Update config/settings.py

Add these lines:
```python
COINBASE_API_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_PRIVATE_KEY = os.getenv("COINBASE_PRIVATE_KEY", "")
FUNDING_RATE_ENABLED = os.getenv("FUNDING_RATE_ENABLED", "false").lower() == "true"
FUNDING_RATE_MAX_POSITION = float(os.getenv("FUNDING_RATE_MAX_POSITION", "100.00"))
FUNDING_RATE_MIN_APR = float(os.getenv("FUNDING_RATE_MIN_APR", "0.10"))
FUNDING_RATE_EXIT_APR = float(os.getenv("FUNDING_RATE_EXIT_APR", "0.05"))
```

### Step 5: Create DynamoDB tables

Run one of the following:

**Option A: AWS CLI**
```bash
aws dynamodb create-table \
  --table-name funding-rate-opportunities \
  --attribute-definitions AttributeName=perp_ticker,AttributeType=S AttributeName=scanned_at,AttributeType=N \
  --key-schema AttributeName=perp_ticker,KeyType=HASH AttributeName=scanned_at,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

aws dynamodb create-table \
  --table-name funding-rate-positions \
  --attribute-definitions AttributeName=position_id,AttributeType=S \
  --key-schema AttributeName=position_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

**Option B: Terraform** (add to your IaC)
```hcl
resource "aws_dynamodb_table" "funding_rate_opportunities" {
  name           = "funding-rate-opportunities"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "perp_ticker"
  range_key      = "scanned_at"

  attribute { name = "perp_ticker", type = "S" }
  attribute { name = "scanned_at", type = "N" }
}

resource "aws_dynamodb_table" "funding_rate_positions" {
  name           = "funding-rate-positions"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "position_id"

  attribute { name = "position_id", type = "S" }
}
```

### Step 6: Update .env.example

Add these (with example values):
```bash
COINBASE_API_KEY_NAME=organizations/xxx/apiKeys/yyy
COINBASE_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"
FUNDING_RATE_ENABLED=false
FUNDING_RATE_MAX_POSITION=100.00
FUNDING_RATE_MIN_APR=0.10
FUNDING_RATE_EXIT_APR=0.05
```

### Step 7: Install dependencies

```bash
pip install PyJWT cryptography aiohttp
```

Add to `requirements.txt`:
```
PyJWT>=2.8.0
cryptography>=41.0.0
aiohttp>=3.9.0
```

### Step 8: Deploy (disabled)

```bash
./deploy.sh
```

**IMPORTANT:** Keep `FUNDING_RATE_ENABLED=false` in `.env` for now. We'll validate API connectivity first.

### Step 9: Validate Coinbase API connection

Ask Claude Code to write and run a quick test script (`test_funding_rate_auth.py`):
```python
#!/usr/bin/env python3
import asyncio
from funding_rate.coinbase_client import CoinbaseClient

async def main():
    client = CoinbaseClient(
        api_key_name="YOUR_KEY_NAME",
        private_key="YOUR_PRIVATE_KEY"
    )
    rate = await client.get_funding_rate("BTC-PERP-INTX")
    print(f"BTC-PERP-INTX funding rate: {rate}")

if __name__ == "__main__":
    asyncio.run(main())
```

Run it locally with test credentials. Confirm you get a valid funding rate response.

### Step 10: Enable and monitor

Once validated:
1. Set `FUNDING_RATE_ENABLED=true` in your `.env`
2. Deploy again: `./deploy.sh`
3. Watch CloudWatch logs for the first scanner/monitor runs
4. Verify DynamoDB tables are being populated

---

## New environment variables

```bash
# Coinbase Advanced API (required for funding_rate)
COINBASE_API_KEY_NAME=organizations/xxx/apiKeys/yyy
COINBASE_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"

# Funding Rate strategy
FUNDING_RATE_ENABLED=false                  # Start false
FUNDING_RATE_MAX_POSITION=100.00            # Max USD per position
FUNDING_RATE_MIN_APR=0.10                   # Min APR to enter (10%)
FUNDING_RATE_EXIT_APR=0.05                  # Exit below this APR (5%)
```

---

## Key files added

| File | Purpose |
|------|---------|
| `funding_rate/__init__.py` | Package init |
| `funding_rate/models.py` | Data models (Opportunity, Position) |
| `funding_rate/coinbase_client.py` | Coinbase Advanced API wrapper |
| `funding_rate/scanner.py` | Scan perp products for high funding rates |
| `funding_rate/monitor.py` | Monitor active positions, exit on low rates |
| `funding_rate/strategy.py` | Core entry/exit logic |

---

## Rollout checklist

- [ ] Copy `funding_rate/` to repo
- [ ] Update `lambda_function.py` with handlers + EventBridge triggers
- [ ] Update `config/settings.py` with new env vars
- [ ] Create DynamoDB tables
- [ ] Update `.env.example`
- [ ] Install PyJWT, cryptography, aiohttp
- [ ] Deploy with `FUNDING_RATE_ENABLED=false`
- [ ] Test Coinbase API auth (run test script)
- [ ] Set `FUNDING_RATE_ENABLED=true` and redeploy
- [ ] Monitor first 24 hours of scanner/monitor runs
- [ ] Adjust `FUNDING_RATE_MIN_APR` / `FUNDING_RATE_MAX_POSITION` as needed
