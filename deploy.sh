#!/usr/bin/env bash
# deploy.sh — Build and deploy the trading bot to AWS Lambda (us-east-2)
#
# Usage:
#   ./deploy.sh [function-name]
#   Default function name: trading-bot-sentiment
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials / role
#   - Python 3.11+ installed
#   - Lambda function already created (or use --create flag logic below)

set -euo pipefail

FUNCTION_NAME="${1:-trading-bot-sentiment}"
REGION="us-east-2"
RUNTIME="python3.12"
HANDLER="lambda_function.handler"
DEPLOY_DIR="$(pwd)"
# Build in /tmp to avoid iCloud Drive sync interference
BUILD_DIR="/tmp/trader-bot-build"
PACKAGE_ZIP="/tmp/trader-bot.zip"
# Resolve AWS account ID early (needed for S3 bucket name and IAM ARNs)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
DEPLOY_BUCKET="trading-bot-deploy-${ACCOUNT_ID}"

echo "=== Trading Bot Deploy ==="
echo "Function : ${FUNCTION_NAME}"
echo "Region   : ${REGION}"
echo ""

# ---------------------------------------------------------------------------
# 1. Clean build directory
# ---------------------------------------------------------------------------
echo "[1/5] Cleaning build directory..."
rm -rf "${BUILD_DIR}" "${PACKAGE_ZIP}"
mkdir -p "${BUILD_DIR}"

# ---------------------------------------------------------------------------
# 2. Install dependencies into build dir root (not a python/ subdir)
# ---------------------------------------------------------------------------
echo "[2/5] Installing dependencies..."
pip3 install \
    --quiet \
    --target "${BUILD_DIR}" \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --python-version 3.12 \
    -r "${DEPLOY_DIR}/requirements.txt"

# ---------------------------------------------------------------------------
# 3. Copy source packages into build root
# ---------------------------------------------------------------------------
echo "[3/5] Copying source files..."
cp "${DEPLOY_DIR}/lambda_function.py" "${BUILD_DIR}/"

for pkg in config broker core sentiment scheduler api carpet_bagger data; do
    cp -r "${DEPLOY_DIR}/${pkg}" "${BUILD_DIR}/"
done

# ---------------------------------------------------------------------------
# 4. Zip everything
# ---------------------------------------------------------------------------
echo "[4/5] Creating deployment package..."
cd "${BUILD_DIR}"
zip -r "${PACKAGE_ZIP}" . -x "*.pyc" -x "*/__pycache__/*" -x "*.dist-info/*"
cd "${DEPLOY_DIR}"

PACKAGE_SIZE=$(du -sh "${PACKAGE_ZIP}" | cut -f1)
echo "    Package size: ${PACKAGE_SIZE} → ${PACKAGE_ZIP}"

# ---------------------------------------------------------------------------
# 5. Deploy to Lambda
# ---------------------------------------------------------------------------
echo "[5/5] Deploying to Lambda..."

# Bump timeout to 300s (new sources add ~30s; Polygon alone takes ~130s on free tier)
aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --timeout 300 \
    --region "${REGION}" \
    --output text > /dev/null 2>&1 || true

# Check if function exists
if aws lambda get-function \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}" \
        --query "Configuration.FunctionName" \
        --output text 2>/dev/null | grep -q "${FUNCTION_NAME}"; then

    echo "    Uploading package to S3 (${DEPLOY_BUCKET})..."
    aws s3 cp "${PACKAGE_ZIP}" "s3://${DEPLOY_BUCKET}/lambda.zip" \
        --region "${REGION}" > /dev/null

    echo "    Updating existing function code from S3..."
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --s3-bucket "${DEPLOY_BUCKET}" \
        --s3-key "lambda.zip" \
        --region "${REGION}" \
        --output json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'    Updated: {d[\"FunctionName\"]} ({d[\"CodeSize\"]} bytes)')
print(f'    Last modified: {d[\"LastModified\"]}')
"

else
    echo "    Function '${FUNCTION_NAME}' not found."
    echo "    Create it first in the AWS console or with:"
    echo ""
    echo "    aws lambda create-function \\"
    echo "      --function-name ${FUNCTION_NAME} \\"
    echo "      --runtime ${RUNTIME} \\"
    echo "      --handler ${HANDLER} \\"
    echo "      --role arn:aws:iam::<ACCOUNT_ID>:role/<LAMBDA_ROLE> \\"
    echo "      --zip-file fileb://${PACKAGE_ZIP} \\"
    echo "      --region ${REGION} \\"
    echo "      --timeout 60 \\"
    echo "      --memory-size 256 \\"
    echo "      --environment 'Variables={AWS_SECRET_NAME=trading-bot/secrets,SNS_TOPIC_ARN=<SNS_ARN>,ACCOUNT_SIZE=1000}'"
    echo ""
    exit 1
fi

# ---------------------------------------------------------------------------
# 6. Migrate to EventBridge Scheduler (DST-aware, America/New_York timezone)
#    Cron times are in ET — no manual updates needed after DST changes.
# ---------------------------------------------------------------------------
echo ""
echo "=== Updating EventBridge Scheduler ==="

LAMBDA_ARN=$(aws lambda get-function \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query "Configuration.FunctionArn" \
    --output text)

# Grant the Lambda execution role permission to read its own CloudWatch logs
# (needed for EOD recap — _fetch_todays_log_events pulls today's scan events)
EXEC_ROLE=$(aws lambda get-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query "Role" \
    --output text | sed 's|.*/||')

aws iam put-role-policy \
    --role-name "${EXEC_ROLE}" \
    --policy-name "trading-bot-read-own-logs" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": [\"logs:DescribeLogStreams\", \"logs:GetLogEvents\"],
        \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${FUNCTION_NAME}:*\"
      }]
    }" \
    --output text > /dev/null 2>&1 && echo "    Lambda log-read policy applied to: ${EXEC_ROLE}" || \
    echo "    (Skipped log-read policy — check IAM role manually if EOD recap is blank)"

# Disable old EventBridge Rules (replaced by Scheduler — idempotent)
for OLD_RULE in trading-bot-pre-market trading-bot-market-open trading-bot-midday; do
    aws events disable-rule --name "${OLD_RULE}" --region "${REGION}" \
        --output text > /dev/null 2>&1 || true
done

# IAM role that allows EventBridge Scheduler to invoke Lambda
SCHED_ROLE="trading-bot-scheduler-role"
SCHED_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SCHED_ROLE}"

if ! aws iam get-role --role-name "${SCHED_ROLE}" --output text > /dev/null 2>&1; then
    aws iam create-role \
        --role-name "${SCHED_ROLE}" \
        --assume-role-policy-document \
          '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        --output text > /dev/null
    aws iam put-role-policy \
        --role-name "${SCHED_ROLE}" \
        --policy-name "invoke-trading-bot-lambda" \
        --policy-document \
          "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"${LAMBDA_ARN}\"}]}" \
        --output text > /dev/null
    echo "    Created IAM role: ${SCHED_ROLE} (waiting for IAM propagation...)"
    sleep 15
fi

# Create or update a schedule.
# Each schedule passes {"window": "<key>"} as the Lambda event payload.
_upsert_schedule() {
    local NAME="$1"
    local CRON="$2"
    local WINDOW_KEY="$3"
    local DESC="$4"

    # Build target JSON using python3 to handle escaping safely
    local TARGET
    TARGET=$(python3 -c "
import json
print(json.dumps({
    'Arn':     '${LAMBDA_ARN}',
    'RoleArn': '${SCHED_ROLE_ARN}',
    'Input':   json.dumps({'window': '${WINDOW_KEY}'}),
}))
")

    if aws scheduler get-schedule --name "${NAME}" --region "${REGION}" \
            --output text > /dev/null 2>&1; then
        aws scheduler update-schedule \
            --name "${NAME}" \
            --schedule-expression "cron(${CRON})" \
            --schedule-expression-timezone "America/New_York" \
            --flexible-time-window '{"Mode":"OFF"}' \
            --target "${TARGET}" \
            --region "${REGION}" \
            --output text > /dev/null
    else
        aws scheduler create-schedule \
            --name "${NAME}" \
            --schedule-expression "cron(${CRON})" \
            --schedule-expression-timezone "America/New_York" \
            --flexible-time-window '{"Mode":"OFF"}' \
            --target "${TARGET}" \
            --description "${DESC}" \
            --region "${REGION}" \
            --output text > /dev/null
    fi
    echo "    ${NAME}: cron(${CRON}) [America/New_York — DST-aware]"
}

_upsert_schedule "trading-bot-pre-market"  "0 8 ? * MON-FRI *"  "pre_market"  "TraderBot pre-market scan 08:00 ET"
_upsert_schedule "trading-bot-market-open" "35 9 ? * MON-FRI *" "market_open" "TraderBot market-open scan 09:35 ET"
_upsert_schedule "trading-bot-midday"      "0 12 ? * MON-FRI *" "midday"      "TraderBot midday scan 12:00 ET"
_upsert_schedule "trading-bot-eod"         "30 15 ? * MON-FRI *" "end_of_day"  "TraderBot end-of-day review 15:30 ET"
_upsert_schedule "trading-bot-evening"     "0 19 ? * MON-FRI *"  "suggestions"    "TraderBot evening suggestions 19:00 ET (5pm MT)"
_upsert_schedule "trading-bot-weekend"     "0 10 ? * SAT *"      "suggestions"    "TraderBot weekend suggestions 10:00 ET Saturday"
_upsert_schedule "trading-bot-weekly"      "0 18 ? * SUN *"      "weekly_review"  "TraderBot weekly performance review 18:00 ET Sunday"

# Carpet Bagger — Kalshi sports prediction market schedules (daily, all days)
_upsert_schedule "carpet-bagger-scout"   "0 8 ? * MON-SUN *"   "carpet_bagger_scout"   "Carpet Bagger pre-game scout 08:00 ET daily"
_upsert_schedule "carpet-bagger-monitor" "0/5 10-23 ? * MON-SUN *" "carpet_bagger_monitor" "Carpet Bagger in-game monitor every 5 min 11am-midnight ET"
_upsert_schedule "carpet-bagger-summary" "59 23 ? * MON-SUN *"  "carpet_bagger_summary" "Carpet Bagger nightly summary 11:59 PM ET"

# EDGAR 8-K monitor — stock catalyst detection, every 5 min during market hours
_upsert_schedule "edgar-monitor" "0/5 8-16 ? * MON-FRI *" "edgar_scan" "EDGAR 8-K monitor every 5 min 8am-4pm ET weekdays"

# Hormuz macro trade monitor — daily P&L check at 4:00 PM ET (after market close)
_upsert_schedule "hormuz-monitor" "0 16 ? * MON-FRI *" "hormuz_monitor" "Hormuz trade P&L monitor 16:00 ET weekdays"

# Hormuz one-shot trade execution — 9:35 AM ET 2026-03-14, fires once then self-deletes
HORMUZ_TARGET=$(python3 -c "
import json
print(json.dumps({
    'Arn':     '${LAMBDA_ARN}',
    'RoleArn': '${SCHED_ROLE_ARN}',
    'Input':   json.dumps({'window': 'hormuz_trade'}),
}))
")
if ! aws scheduler get-schedule --name "hormuz-trade-exec" --region "${REGION}" \
        --output text > /dev/null 2>&1; then
    aws scheduler create-schedule \
        --name "hormuz-trade-exec" \
        --schedule-expression "at(2026-03-13T09:35:00)" \
        --schedule-expression-timezone "America/New_York" \
        --flexible-time-window '{"Mode":"OFF"}' \
        --action-after-completion "DELETE" \
        --target "${HORMUZ_TARGET}" \
        --description "Hormuz trade one-shot execution at market open 2026-03-13" \
        --region "${REGION}" \
        --output text > /dev/null
    echo "    Created one-shot schedule: hormuz-trade-exec (fires 2026-03-13 09:35 ET, then deletes)"
else
    echo "    hormuz-trade-exec already exists — skipping (delete it first to reschedule)"
fi

# ---------------------------------------------------------------------------
# 6b. Carpet Bagger — DynamoDB table + Lambda IAM permissions
# ---------------------------------------------------------------------------
echo ""
echo "=== Carpet Bagger DynamoDB table ==="

CB_TABLE="carpet-bagger-watchlist"

if aws dynamodb describe-table --table-name "${CB_TABLE}" --region "${REGION}" \
        --output text > /dev/null 2>&1; then
    echo "    Table '${CB_TABLE}' already exists — skipping"
else
    echo "    Creating DynamoDB table '${CB_TABLE}'..."
    aws dynamodb create-table \
        --table-name "${CB_TABLE}" \
        --attribute-definitions AttributeName=market_ticker,AttributeType=S \
        --key-schema AttributeName=market_ticker,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "${REGION}" \
        --output text > /dev/null
    echo "    Waiting for table to become active..."
    aws dynamodb wait table-exists --table-name "${CB_TABLE}" --region "${REGION}"
    echo "    Table '${CB_TABLE}' created (PAY_PER_REQUEST)"
fi

# ---------------------------------------------------------------------------
# 6b2. trading-bot-logs table — stores every Claude agent decision
# ---------------------------------------------------------------------------
LOG_TABLE="trading-bot-logs"

if aws dynamodb describe-table --table-name "${LOG_TABLE}" --region "${REGION}" \
        --output text > /dev/null 2>&1; then
    echo "    Table '${LOG_TABLE}' already exists — skipping"
else
    echo "    Creating DynamoDB table '${LOG_TABLE}'..."
    aws dynamodb create-table \
        --table-name "${LOG_TABLE}" \
        --attribute-definitions AttributeName=trade_id,AttributeType=S \
        --key-schema AttributeName=trade_id,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "${REGION}" \
        --output text > /dev/null
    echo "    Waiting for table to become active..."
    aws dynamodb wait table-exists --table-name "${LOG_TABLE}" --region "${REGION}"
    echo "    Table '${LOG_TABLE}' created (PAY_PER_REQUEST)"
fi

# Grant Lambda execution role access to the logs table
aws iam put-role-policy \
    --role-name "${EXEC_ROLE}" \
    --policy-name "trading-bot-decision-log" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": [\"dynamodb:PutItem\", \"dynamodb:GetItem\", \"dynamodb:Scan\"],
        \"Resource\": \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${LOG_TABLE}\"
      }]
    }" \
    --output text > /dev/null 2>&1 && \
    echo "    DynamoDB policy applied to Lambda role: ${EXEC_ROLE} (logs table)" || \
    echo "    (Skipped DynamoDB logs policy — check IAM role manually)"

# Grant Lambda execution role DynamoDB access for the Carpet Bagger table
aws iam put-role-policy \
    --role-name "${EXEC_ROLE}" \
    --policy-name "carpet-bagger-dynamodb" \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:PutItem\",
          \"dynamodb:GetItem\",
          \"dynamodb:UpdateItem\",
          \"dynamodb:DeleteItem\",
          \"dynamodb:Scan\",
          \"dynamodb:Query\"
        ],
        \"Resource\": \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${CB_TABLE}\"
      }]
    }" \
    --output text > /dev/null 2>&1 && \
    echo "    DynamoDB policy applied to Lambda role: ${EXEC_ROLE}" || \
    echo "    (Skipped DynamoDB policy — check IAM role manually)"

# ---------------------------------------------------------------------------
# 6c. API Gateway HTTP API — for one-click trade approval links
#     (Lambda Function URLs blocked at account level; API Gateway is reliable)
# ---------------------------------------------------------------------------
echo ""
echo "=== API Gateway approval endpoint ==="

API_NAME="trading-bot-approval"

# Reuse existing API if it exists
API_ID=$(aws apigatewayv2 get-apis \
    --region "${REGION}" \
    --query "Items[?Name=='${API_NAME}'].ApiId" \
    --output text 2>/dev/null)

if [ -z "${API_ID}" ]; then
    echo "    Creating HTTP API..."
    API_ID=$(aws apigatewayv2 create-api \
        --name "${API_NAME}" \
        --protocol-type HTTP \
        --region "${REGION}" \
        --query ApiId --output text)
fi
echo "    API ID: ${API_ID}"

# Create a fresh integration (idempotent: create new one each deploy)
INT_ID=$(aws apigatewayv2 create-integration \
    --api-id "${API_ID}" \
    --integration-type AWS_PROXY \
    --integration-uri "${LAMBDA_ARN}" \
    --payload-format-version "2.0" \
    --region "${REGION}" \
    --query IntegrationId --output text)

# Upsert routes (idempotent — create-route is a no-op if it already exists)
for ROUTE_KEY in "GET /approve" "GET /balance" "GET /orders" "POST /orders/new" "POST /orders/{orderId}/edit"; do
    aws apigatewayv2 create-route \
        --api-id "${API_ID}" \
        --route-key "${ROUTE_KEY}" \
        --target "integrations/${INT_ID}" \
        --region "${REGION}" \
        --output text > /dev/null 2>&1 || true
    echo "    Route: ${ROUTE_KEY}"
done

# Configure CORS — required so the settings UI can call /orders with Authorization header
aws apigatewayv2 update-api \
    --api-id "${API_ID}" \
    --cors-configuration \
      "AllowOrigins='*',AllowHeaders='Authorization,Content-Type',AllowMethods='GET,POST,OPTIONS'" \
    --region "${REGION}" \
    --output text > /dev/null && echo "    CORS configured on API Gateway"

# Ensure a $default stage exists with auto-deploy
aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name '$default' \
    --auto-deploy \
    --region "${REGION}" \
    --output text > /dev/null 2>&1 || true

# Grant API Gateway permission to invoke Lambda
aws lambda add-permission \
    --function-name "${FUNCTION_NAME}" \
    --statement-id "allow-apigw-approval" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    --region "${REGION}" \
    --output text > /dev/null 2>&1 || true

API_URL=$(aws apigatewayv2 get-api \
    --api-id "${API_ID}" \
    --region "${REGION}" \
    --query ApiEndpoint --output text)

# Store API URL as LAMBDA_FUNCTION_URL so suggestions.py can build approval links
CURRENT_ENV=$(aws lambda get-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query "Environment.Variables" \
    --output json 2>/dev/null || echo "{}")
UPDATED_ENV=$(python3 -c "
import json, sys
env = json.loads(sys.argv[1])
env['LAMBDA_FUNCTION_URL'] = sys.argv[2] + '/'
print(json.dumps({'Variables': env}))
" "${CURRENT_ENV}" "${API_URL}")
aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --environment "${UPDATED_ENV}" \
    --region "${REGION}" \
    --output text > /dev/null 2>&1 && \
    echo "    LAMBDA_FUNCTION_URL set on Lambda"
echo "    Approval endpoint: ${API_URL}/approve?ticker=AAPL&..."

# ---------------------------------------------------------------------------
# 6d. Suggestion token secret — generate once, store in Secrets Manager
# ---------------------------------------------------------------------------
echo ""
echo "=== Suggestion token secret ==="

EXISTING_SECRET_JSON=$(aws secretsmanager get-secret-value \
    --secret-id "trading-bot/secrets" \
    --region "${REGION}" \
    --query "SecretString" \
    --output text 2>/dev/null || echo "{}")

if echo "${EXISTING_SECRET_JSON}" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'SUGGESTION_TOKEN_SECRET' in d else 1)" 2>/dev/null; then
    echo "    SUGGESTION_TOKEN_SECRET already in Secrets Manager — skipping generation"
else
    echo "    Generating new SUGGESTION_TOKEN_SECRET..."
    NEW_TOKEN_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    UPDATED_SECRET=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
d['SUGGESTION_TOKEN_SECRET'] = sys.argv[2]
print(json.dumps(d))
" "${EXISTING_SECRET_JSON}" "${NEW_TOKEN_SECRET}")
    aws secretsmanager put-secret-value \
        --secret-id "trading-bot/secrets" \
        --region "${REGION}" \
        --secret-string "${UPDATED_SECRET}" \
        --output text > /dev/null
    echo "    SUGGESTION_TOKEN_SECRET added to Secrets Manager"
fi

# ---------------------------------------------------------------------------
# 7. Verify deployed version
# ---------------------------------------------------------------------------
echo ""
echo "=== Deploy complete ==="
aws lambda get-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}" \
    --query "{FunctionName:FunctionName,Runtime:Runtime,Handler:Handler,LastModified:LastModified,State:State}" \
    --output table
