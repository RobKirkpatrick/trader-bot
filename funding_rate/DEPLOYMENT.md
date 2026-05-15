# Deployment Guide: Funding Rate Arbitrage Module

This guide walks through deploying the `funding_rate` module to AWS Lambda.

## Prerequisites

- AWS account with CLI configured (`aws configure`)
- Coinbase Advanced Trade account with INTX perpetual access
- EC P-256 API key from Coinbase (see below)
- Python 3.11+
- Terraform 1.0+ (optional, but recommended)

## Step 1: Generate Coinbase API Key

### In Coinbase Cloud

1. Navigate to **Settings → API Keys**
2. Click **Create new key**
3. Select **Advanced Trading** scope
4. Permissions: Enable at minimum:
   - `orders:create` (place orders)
   - `orders:read` (check order status)
   - `accounts:read` (get balances)
   - `products:read` (get market data)
5. **Key type:** Select **EC key** (NOT RSA)
6. Copy and save:
   - **Key ID** (format: `organizations/xxx/apiKeys/yyy`)
   - **Private key** (PEM format, starts with `-----BEGIN EC PRIVATE KEY-----`)

### Verify Key Format

Your private key should look like:

```
-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIIGlVmKzw3Zq7lAf7mRJ+/H8lKz9pQ2rK5sM4xZ9vQ4JoAoGCCqGSM49
AwEHoUQDQgAE...
-----END EC PRIVATE KEY-----
```

**IMPORTANT:** Store this key securely. In production, use AWS Secrets Manager.

## Step 2: Set Up Environment

### Clone Module

```bash
cd /path/to/trading-bot
git clone <repo> funding_rate
cd funding_rate
```

### Install Dependencies

```bash
python3.11 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:

```bash
COINBASE_API_KEY_NAME="organizations/YOUR_ORG_ID/apiKeys/YOUR_KEY_ID"
COINBASE_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----
YOUR_ACTUAL_KEY_CONTENT_HERE
-----END EC PRIVATE KEY-----"

FUNDING_RATE_ENABLED=true
FUNDING_RATE_MAX_POSITION=100.00
FUNDING_RATE_MIN_APR=0.10
FUNDING_RATE_EXIT_APR=0.05
FUNDING_RATE_MAX_PCT_BALANCE=0.30

AWS_REGION=us-east-1
SENTINEL_SNS_ARN=arn:aws:sns:us-east-1:123456789012:trading-bot-alerts
```

### Test Locally

```bash
# Test strategy calculations
python3 -c "from funding_rate.strategy import annualize_funding_rate; print(annualize_funding_rate(0.0003))"

# Test Coinbase connectivity (requires COINBASE_* env vars set)
python3 -c """
import asyncio
import os
from funding_rate.coinbase_client import CoinbaseClient

client = CoinbaseClient(
    api_key_name=os.environ['COINBASE_API_KEY_NAME'],
    private_key_pem=os.environ['COINBASE_PRIVATE_KEY'],
)

async def test():
    active = await client.is_trading_active()
    print('Trading active:', active)
    rate = await client.get_funding_rate('BTC-PERP-INTX')
    print('BTC funding rate (8h):', rate)
    await client.close()

asyncio.run(test())
"""
```

Expected output:

```
Trading active: True
BTC funding rate (8h): 0.0003
```

## Step 3: Deploy to AWS

Choose one of two deployment options:

### Option A: Terraform (Recommended)

Terraform automates Lambda creation, DynamoDB tables, EventBridge rules, and IAM.

#### Set Up Terraform

```bash
cd terraform
terraform init
```

#### Create terraform.tfvars

```bash
cat > terraform.tfvars <<EOF
aws_region = "us-east-1"
lambda_timeout = 60
lambda_memory = 512
sentinel_sns_topic_arn = "arn:aws:sns:us-east-1:123456789012:trading-bot-alerts"
EOF
```

#### Plan & Apply

```bash
terraform plan
terraform apply
```

Terraform will:
1. Create DynamoDB tables
2. Create IAM role with minimal permissions
3. Build and upload Lambda functions
4. Create EventBridge rules (scanner @ 4h, monitor @ 1h)
5. Output Lambda function names & DynamoDB table names

#### Verify Deployment

```bash
# List created resources
aws dynamodb list-tables
aws lambda list-functions | grep funding-rate
aws events list-rules | grep funding-rate

# Check EventBridge rules
aws events describe-rule --name funding-rate-scanner
aws events describe-rule --name funding-rate-monitor
```

### Option B: Manual Deployment

If you prefer not to use Terraform:

#### 1. Create DynamoDB Tables

```bash
# Opportunities table
aws dynamodb create-table \
  --table-name funding-rate-opportunities \
  --attribute-definitions \
    AttributeName=perp_ticker,AttributeType=S \
    AttributeName=scanned_at,AttributeType=S \
  --key-schema \
    AttributeName=perp_ticker,KeyType=HASH \
    AttributeName=scanned_at,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --ttl-specification AttributeName=expires_at,Enabled=true

# Positions table
aws dynamodb create-table \
  --table-name funding-rate-positions \
  --attribute-definitions AttributeName=position_id,AttributeType=S \
  --key-schema AttributeName=position_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

#### 2. Create IAM Role

```bash
# Create role
aws iam create-role \
  --role-name funding-rate-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# Attach policies
aws iam attach-role-policy \
  --role-name funding-rate-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Add DynamoDB permissions
aws iam put-role-policy \
  --role-name funding-rate-lambda-role \
  --policy-name funding-rate-dynamodb \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": [
        "arn:aws:dynamodb:*:*:table/funding-rate-*"
      ]
    }]
  }'

# Add SNS permissions
aws iam put-role-policy \
  --role-name funding-rate-lambda-role \
  --policy-name funding-rate-sns \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "arn:aws:sns:*:*:*"
    }]
  }'
```

#### 3. Build Lambda Packages

```bash
# Create deployment packages
cd lambda
python3 -m pip install -r ../requirements.txt -t scanner_package/
cp -r ../funding_rate scanner_package/
cd scanner_package && zip -r ../funding-rate-scanner.zip . && cd ..

python3 -m pip install -r ../requirements.txt -t monitor_package/
cp -r ../funding_rate monitor_package/
cd monitor_package && zip -r ../funding-rate-monitor.zip . && cd ..
```

#### 4. Create Lambda Functions

```bash
# Scanner function
aws lambda create-function \
  --function-name funding-rate-scanner \
  --runtime python3.11 \
  --role arn:aws:iam::ACCOUNT_ID:role/funding-rate-lambda-role \
  --handler lambda_handlers.handler_scanner \
  --timeout 60 \
  --memory-size 512 \
  --zip-file fileb://funding-rate-scanner.zip \
  --environment Variables="{
    FUNDING_RATE_ENABLED=true,
    SENTINEL_SNS_ARN=arn:aws:sns:us-east-1:ACCOUNT_ID:trading-bot-alerts
  }"

# Monitor function
aws lambda create-function \
  --function-name funding-rate-monitor \
  --runtime python3.11 \
  --role arn:aws:iam::ACCOUNT_ID:role/funding-rate-lambda-role \
  --handler lambda_handlers.handler_monitor \
  --timeout 60 \
  --memory-size 512 \
  --zip-file fileb://funding-rate-monitor.zip \
  --environment Variables="{
    FUNDING_RATE_ENABLED=true,
    SENTINEL_SNS_ARN=arn:aws:sns:us-east-1:ACCOUNT_ID:trading-bot-alerts
  }"
```

Replace `ACCOUNT_ID` with your AWS account ID.

#### 5. Create EventBridge Rules

```bash
# Scanner rule (every 4 hours)
aws events put-rule \
  --name funding-rate-scanner \
  --schedule-expression "rate(4 hours)" \
  --state ENABLED

# Monitor rule (every 1 hour)
aws events put-rule \
  --name funding-rate-monitor \
  --schedule-expression "rate(1 hour)" \
  --state ENABLED

# Add Lambda targets
aws events put-targets \
  --rule funding-rate-scanner \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:ACCOUNT_ID:function:funding-rate-scanner","RoleArn"="arn:aws:iam::ACCOUNT_ID:role/service-role/funding-rate-eventbridge-role"

aws events put-targets \
  --rule funding-rate-monitor \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:ACCOUNT_ID:function:funding-rate-monitor","RoleArn"="arn:aws:iam::ACCOUNT_ID:role/service-role/funding-rate-eventbridge-role"

# Grant EventBridge permission to invoke Lambda
aws lambda add-permission \
  --function-name funding-rate-scanner \
  --statement-id AllowExecutionFromEventBridge \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/funding-rate-scanner

aws lambda add-permission \
  --function-name funding-rate-monitor \
  --statement-id AllowExecutionFromEventBridge \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/funding-rate-monitor
```

## Step 4: Configure Secrets (Production)

For production, store Coinbase credentials in AWS Secrets Manager:

```bash
aws secretsmanager create-secret \
  --name coinbase-api-credentials \
  --description "Coinbase API credentials" \
  --secret-string '{
    "api_key_name": "organizations/xxx/apiKeys/yyy",
    "private_key_pem": "-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"
  }'
```

Update Lambda environment to fetch from Secrets Manager (modify `lambda_handlers.py`):

```python
import boto3
import json

def _get_coinbase_credentials() -> tuple[str, str]:
    secrets = boto3.client('secretsmanager')
    response = secrets.get_secret_value(SecretId='coinbase-api-credentials')
    creds = json.loads(response['SecretString'])
    return creds['api_key_name'], creds['private_key_pem']
```

## Step 5: Test Deployment

### Manual Invocation

```bash
# Invoke scanner
aws lambda invoke \
  --function-name funding-rate-scanner \
  --log-type Tail \
  /tmp/scanner-result.json
cat /tmp/scanner-result.json

# Invoke monitor
aws lambda invoke \
  --function-name funding-rate-monitor \
  --log-type Tail \
  /tmp/monitor-result.json
cat /tmp/monitor-result.json
```

### Check Logs

```bash
# Follow scanner logs
aws logs tail /aws/lambda/funding-rate-scanner --follow

# Follow monitor logs
aws logs tail /aws/lambda/funding-rate-monitor --follow
```

### Verify DynamoDB Tables

```bash
# Check opportunities
aws dynamodb scan --table-name funding-rate-opportunities

# Check positions
aws dynamodb scan --table-name funding-rate-positions
```

## Step 6: Enable the Module

Once tested, enable the module:

```bash
# Update Lambda environment
aws lambda update-function-configuration \
  --function-name funding-rate-scanner \
  --environment Variables="{FUNDING_RATE_ENABLED=true,...}"

aws lambda update-function-configuration \
  --function-name funding-rate-monitor \
  --environment Variables="{FUNDING_RATE_ENABLED=true,...}"
```

Or via Terraform:

```bash
terraform apply -var="funding_rate_enabled=true"
```

## Monitoring

### CloudWatch Alarms

Create alarms for Lambda errors:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name funding-rate-scanner-errors \
  --alarm-description "Alert on scanner errors" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --dimensions Name=FunctionName,Value=funding-rate-scanner \
  --alarm-actions "arn:aws:sns:us-east-1:ACCOUNT_ID:trading-bot-alerts"
```

### SNS Alerts

All major events (position opened, closed, errors) are sent to `SENTINEL_SNS_ARN`. Subscribe to the SNS topic:

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:ACCOUNT_ID:trading-bot-alerts \
  --protocol email \
  --notification-endpoint your-email@example.com
```

## Troubleshooting

### Lambda Timeouts

If Lambda times out (>60s):

1. Increase timeout:
   ```bash
   aws lambda update-function-configuration \
     --function-name funding-rate-monitor \
     --timeout 120
   ```

2. Check logs for slow operations:
   ```bash
   aws logs tail /aws/lambda/funding-rate-monitor --follow --format short
   ```

### DynamoDB Throttling

If seeing "ProvisionedThroughputExceededException":

- Module uses PAY_PER_REQUEST (auto-scaling), so shouldn't throttle
- If using PROVISIONED billing, increase capacity:
  ```bash
  aws dynamodb update-table \
    --table-name funding-rate-positions \
    --billing-mode PAY_PER_REQUEST
  ```

### Coinbase API Errors

Check logs for 401 (auth) or 429 (rate limit) errors:

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/funding-rate-scanner \
  --filter-pattern "error\|Error"
```

Common issues:
- **401 Unauthorized:** Check API key format and private key encoding
- **429 Rate Limited:** Scanner/monitor will backoff automatically
- **Network timeout:** Rare; Lambda retries automatically via EventBridge

### Manual Position Management

If a position gets stuck, you can manually update it:

```bash
aws dynamodb update-item \
  --table-name funding-rate-positions \
  --key '{"position_id": {"S": "abc-123"}}' \
  --update-expression "SET #status = :closed, exit_reason = :reason" \
  --expression-attribute-names '{"#status": "status"}' \
  --expression-attribute-values '{":closed": {"S": "closed"}, ":reason": {"S": "manual_override"}}'
```

## Rollback

To disable the module without deleting resources:

```bash
# Disable EventBridge rules
aws events disable-rule --name funding-rate-scanner
aws events disable-rule --name funding-rate-monitor
```

To fully remove (Terraform):

```bash
terraform destroy
```

To fully remove (manual):

```bash
# Delete Lambda functions
aws lambda delete-function --function-name funding-rate-scanner
aws lambda delete-function --function-rate-monitor

# Delete EventBridge rules
aws events delete-rule --name funding-rate-scanner --force
aws events delete-rule --name funding-rate-monitor --force

# Delete DynamoDB tables
aws dynamodb delete-table --table-name funding-rate-opportunities
aws dynamodb delete-table --table-name funding-rate-positions

# Delete IAM role
aws iam delete-role-policy --role-name funding-rate-lambda-role --policy-name funding-rate-dynamodb
aws iam delete-role-policy --role-name funding-rate-lambda-role --policy-name funding-rate-sns
aws iam delete-role --role-name funding-rate-lambda-role
```

## Next Steps

1. **Monitor the first execution** — Check logs and SNS alerts
2. **Review first position** — Verify entry prices and funding rate
3. **Set up CloudWatch dashboard** — Track funding collected over time
4. **Enable other modules** — carpet_bagger and bracket_buster

---

**Deployment Summary**

| Component | Status |
|-----------|--------|
| Environment configured | ✓ |
| Coinbase API key tested | ✓ |
| Lambda functions deployed | ✓ |
| DynamoDB tables created | ✓ |
| EventBridge rules active | ✓ |
| SNS alerts configured | ✓ |
| Module enabled | ✓ |

---

For issues, check:
1. Lambda function logs in CloudWatch
2. DynamoDB tables for data
3. EventBridge rule history
4. Coinbase API documentation
