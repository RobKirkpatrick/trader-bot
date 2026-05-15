# Architecture Documentation

## Overview

The `funding_rate` module implements delta-neutral funding rate arbitrage on Coinbase perpetual futures through two coordinated AWS Lambda functions orchestrated by EventBridge.

```
EventBridge (4h, 1h)
       ↓
   Lambda Invocation
       ↓
┌─────────────────────────────────────┐
│    Scanner (4h)   │   Monitor (1h)  │
│  Find opportunities │  Execute & manage │
└─────────────────────────────────────┘
       ↓                    ↓
   DynamoDB          Coinbase API
   Opportunities     (REST)
   Positions         ↓
       ↓             Place/manage
   SNS Alerts        orders
```

## Data Flow

### Phase 1: Opportunity Discovery (Scanner, every 4 hours)

```
┌─────────────────────────────────────────────┐
│ EventBridge: rate(4 hours)                  │
└─────────────────────┬───────────────────────┘
                      ↓
          ┌───────────────────────┐
          │ Lambda: Scanner       │
          │ (handler_scanner)     │
          └───────────┬───────────┘
                      ↓
      ┌───────────────────────────────┐
      │ For each pair in PERP_PAIRS:  │
      │ - Get current funding rate    │
      │ - Check if > MIN_FUNDING_APR  │
      │ - Check no open position      │
      │ - Write opportunity to DDB    │
      └───────────┬───────────────────┘
                  ↓
    ┌─────────────────────────────────┐
    │ DynamoDB: opportunities table    │
    │ {                               │
    │   perp_ticker: "BTC-PERP-INTX", │
    │   scanned_at: ISO-8601,         │
    │   status: "pending",            │
    │   funding_rate_8hr: 0.0003,     │
    │   ...                           │
    │ }                               │
    └────────────┬────────────────────┘
                 ↓
         ┌───────────────┐
         │ SNS: Summary  │
         │ Alert         │
         └───────────────┘
```

**Scanner Responsibilities:**
1. For each perpetual pair (BTC, ETH, SOL, ...):
   - Call `Coinbase.get_funding_rate(perp_ticker)`
   - Annualize: `rate_8hr × 3 × 365`
   - If annualized > 10% AND no existing position:
     - Fetch current spot/perp prices
     - Write `FundingOpportunity` to DynamoDB
     - Mark status = "pending"

2. Publish SNS summary with:
   - Pairs scanned
   - Opportunities found
   - Existing positions (skip)
   - Any errors

### Phase 2: Opportunity Execution (Monitor Phase A, every 1 hour)

```
┌──────────────────────────────────┐
│ EventBridge: rate(1 hour)        │
└────────────┬─────────────────────┘
             ↓
   ┌──────────────────────┐
   │ Lambda: Monitor      │
   │ (handler_monitor)    │
   │ Phase A: Execute     │
   └────────┬─────────────┘
            ↓
  ┌─────────────────────────────────┐
  │ Query DDB: pending opportunities │
  └────────┬────────────────────────┘
           ↓
    For each pending opp:
    ├─ Re-check funding rate
    ├─ Get spot/perp prices
    ├─ Get available balance
    ├─ Calc position size
    │  min(MAX_POSITION_USD, balance × MAX_PCT)
    ├─ Place spot BUY (market)
    │  └─ Coinbase.place_spot_buy()
    ├─ Place perp SHORT (market)
    │  └─ Coinbase.place_perp_short()
    ├─ Wait for fills (30s timeout)
    └─ Write FundingPosition to DDB
       {
         position_id: UUID,
         status: "open",
         entry_spot_price: 83000.0,
         entry_perp_price: 83050.0,
         entry_funding_rate_8hr: 0.0003,
         notional_usd: 100.0,
         spot_quantity: 0.001,
         perp_contracts: 0.001,
         ...
       }
```

**Phase A Responsibilities:**
1. Fetch all pending opportunities from DDB
2. For each:
   - Verify funding rate still qualifies (stale check)
   - Calculate position size based on available balance
   - Place market orders (spot BUY + perp SHORT)
   - Poll order status until filled (30s max)
   - Write new `FundingPosition` with status="open"
   - Alert via SNS: "POSITION OPENED: BTC @ 12.5% APR, $100 deployed"

**Important:** If spot fills but perp doesn't (or vice versa), the position is NOT created. This maintains delta-neutrality invariant.

### Phase 3: Position Monitoring (Monitor Phase B, every 1 hour)

```
For each open position:

1. FUNDING PAYMENT RECORDING
   ├─ Check if 8h since entry or last_funding_at
   ├─ If yes:
   │  ├─ payment_usd = notional × entry_rate_8hr
   │  ├─ funding_collected_usd += payment_usd
   │  ├─ funding_payments_count += 1
   │  └─ Update DDB
   └─ Continue

2. EXIT CONDITION CHECKS (priority order)
   ├─ A. Funding rate < EXIT_FUNDING_APR?
   │  └─ Close position (exit_reason="rate_too_low")
   ├─ B. Position age > MAX_HOLD_DAYS?
   │  └─ Close position (exit_reason="max_hold")
   └─ C. Spot/perp drift > REBALANCE_THRESHOLD?
      └─ Rebalance smaller leg

3. ON EXIT
   ├─ Place spot SELL (market)
   ├─ Place perp BUY-to-close (market)
   ├─ Wait for fills
   ├─ Calculate realized_pnl
   ├─ Update position: status="closed"
   └─ Alert via SNS: "CLOSED: BTC, Collected $X over Y days"
```

**Phase B Responsibilities:**
1. Query all open positions from DDB
2. For each position:
   - Fetch current funding rate
   - Record funding payment if 8h passed
   - Check exit conditions in priority order
   - If exiting: close both legs, calculate P&L, update status
   - If not exiting: check drift and rebalance if needed
3. Publish SNS summary with executed count, rebalanced count, closed count

## State Machine

```
         ┌──────────────────────┐
         │   No Position        │
         │                      │
         └─────────┬────────────┘
                   │
          Scanner finds opp
          (rate > 10% APR)
                   │
                   ↓
         ┌──────────────────────┐
         │   Opportunity        │
         │   status="pending"   │
         │                      │
         └─────────┬────────────┘
                   │
         Monitor: Execute phase
         (rate still > 10%?)
                   │
         ┌─────────┴──────────┐
         │ No (stale)         │ Yes
         │                    │
      (mark stale)         ↓
                   ┌──────────────────────┐
                   │   Position           │
                   │   status="open"      │
                   │                      │
                   └──────────┬───────────┘
                              │
                  Monitor: Check exit conditions
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
    Rate < 5%?     Age > 30d?     Drift > 2%?
         │                    │                    │
         Yes                  Yes                  Yes
         │                    │                    │
         ↓                    ↓                    ↓
    ┌────────┐         ┌────────┐         ┌───────────┐
    │ Close  │         │ Close  │         │ Rebalance │
    │        │         │        │         │           │
    └───┬────┘         └───┬────┘         └─────┬─────┘
        │                  │                    │
        └──────────┬───────┴──────────┬─────────┘
                   │                  │
                   ↓                  ↓
         ┌──────────────────────┐     │
         │   Position           │     │
         │   status="closed"    │     │
         │   realized_pnl=$X    │     │
         │                      │     │
         └──────────────────────┘     │
                                      │
                    (stay in "open")──┘
```

## Component Architecture

### CoinbaseClient (`coinbase_client.py`)

Async HTTP client for Coinbase Advanced Trade API.

```python
CoinbaseClient
├── __init__(api_key_name, private_key_pem)
├── _ensure_session()              # Lazy session creation
├── _build_jwt(method, path)       # ES256 JWT auth
├── _request(method, path, ...)    # Base HTTP with retry logic
│
├── get_funding_rate(perp_ticker)
├── get_best_bid_ask(product_id)
├── get_spot_balance(currency)
├── get_futures_position(perp_ticker)
├── get_order_status(order_id)
│
├── place_spot_buy(product_id, size_usd)
├── place_perp_short(perp_ticker, size_usd)
├── place_spot_sell(product_id, quantity)
├── place_perp_close(perp_ticker, contracts)
│
├── is_trading_active()
└── close()
```

**Design:**
- Async/await for non-blocking I/O
- JWT auth with ES256 (EC P-256)
- Exponential backoff on 429 (rate limit)
- Fail-fast on 401 (auth error)
- All market orders for fast fills

### Scanner (`scanner.py`)

Discovers arbitrage opportunities.

```python
FundingRateScanner
├── __init__(client, opportunities_table_name, positions_table_name)
├── run(sns_topic_arn)
│  └─ _execute_pending_opportunities(results)
│     └─ _scan_pair(perp_ticker, spot_ticker, results)
│        ├─ get_funding_rate()
│        ├─ _get_existing_position()
│        ├─ write_opportunity()
│        └─ publish_alert()
```

**Logic:**
1. Query all open/rebalancing positions (skip if any for this pair)
2. Fetch current 8hr funding rate
3. Annualize: `rate × 3 × 365`
4. If > 10%, write opportunity to DDB with status="pending"
5. Publish SNS summary

### Monitor (`monitor.py`)

Executes opportunities and manages positions.

```python
FundingRateMonitor
├── __init__(client, tables, max_position_usd, max_pct_balance)
├── run(sns_topic_arn)
│  ├─ _execute_pending_opportunities(results)
│  │  ├─ Query pending opportunities
│  │  └─ For each:
│  │     ├─ Re-check funding rate (stale check)
│  │     ├─ _wait_for_order_fill()
│  │     ├─ _write_position()
│  │     └─ SNS alert
│  │
│  └─ _monitor_open_positions(results)
│     ├─ Query open positions
│     └─ For each:
│        ├─ _maybe_record_funding_payment()
│        ├─ Check exit conditions:
│        │  ├─ rate < 5%?
│        │  ├─ age > 30 days?
│        │  └─ drift > 2%?
│        └─ If exiting: _close_position()
│           Or if drifting: _rebalance_position()
```

**Key Methods:**
- `_maybe_record_funding_payment()`: Increments funding collected if 8h passed
- `_calculate_drift()`: Computes |spot_value - perp_value| / avg
- `_close_position()`: Closes both legs, calculates P&L
- `_rebalance_position()`: Adjusts smaller leg to match larger

### Models (`models.py`)

Data structures for state persistence.

```python
FundingOpportunity
├─ perp_ticker: str
├─ spot_ticker: str
├─ scanned_at: str (ISO-8601, DDB sort key)
├─ funding_rate_8hr: float
├─ funding_apr: float
├─ status: str (pending | executed | stale | error)
├─ spot_price: float | None
└─ perp_price: float | None

FundingPosition
├─ position_id: str (UUID, DDB hash key)
├─ perp_ticker: str
├─ spot_ticker: str
├─ entry_spot_price: float
├─ entry_perp_price: float
├─ entry_funding_rate_8hr: float
├─ entry_funding_apr: float
├─ notional_usd: float
├─ spot_quantity: float
├─ perp_contracts: float
├─ spot_order_id: str
├─ perp_order_id: str
├─ status: str (open | rebalancing | closing | closed)
├─ opened_at: str (ISO-8601)
├─ closed_at: str | None
├─ funding_collected_usd: float
├─ funding_payments_count: int
├─ last_funding_at: str | None
├─ realized_pnl: float
└─ exit_reason: str | None
```

## DynamoDB Tables

### funding-rate-opportunities

```
PrimaryKey:
  Hash:  perp_ticker (S)
  Range: scanned_at (S)

Attributes:
  spot_ticker (S)
  funding_rate_8hr (N)
  funding_apr (N)
  status (S)
  spot_price (N, optional)
  perp_price (N, optional)

TTL: expires_at (optional, 7 days)
Billing: PAY_PER_REQUEST
```

Used by scanner to write pending opportunities. Monitor queries by status="pending".

### funding-rate-positions

```
PrimaryKey:
  Hash: position_id (S)

Attributes:
  perp_ticker (S)
  spot_ticker (S)
  entry_spot_price (N)
  entry_perp_price (N)
  entry_funding_rate_8hr (N)
  entry_funding_apr (N)
  notional_usd (N)
  spot_quantity (N)
  perp_contracts (N)
  spot_order_id (S)
  perp_order_id (S)
  status (S)
  opened_at (S)
  closed_at (S, optional)
  funding_collected_usd (N)
  funding_payments_count (N)
  last_funding_at (S, optional)
  realized_pnl (N)
  exit_reason (S, optional)

Billing: PAY_PER_REQUEST
```

Holds all active and closed positions. Queried by monitor to find open positions.

## Error Handling

### Lambda-level

```python
try:
    client = CoinbaseClient(...)
    scanner = FundingRateScanner(client)
    results = asyncio.run(scanner.run(...))
    return {"statusCode": 200, "body": json.dumps(results)}
except ValueError as e:           # Config error
    return {"statusCode": 400, "body": json.dumps({"error": str(e)})}
except Exception as e:            # Unexpected
    logger.error(..., exc_info=True)
    return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
```

### API-level (CoinbaseClient)

```python
async def _request(...):
    for attempt in range(max_retries):
        try:
            if response.status == 401:
                raise CoinbaseAuthError(...)     # Fail fast
            if response.status == 429:
                backoff = 2 ** attempt
                await asyncio.sleep(backoff)      # Exponential backoff
                continue
            if response.status >= 400:
                raise CoinbaseAPIError(...)
            return response.json()
        except asyncio.TimeoutError:
            if attempt == max_retries - 1:
                raise CoinbaseAPIError(...)
            await asyncio.sleep(1)
```

### Application-level

```python
# If spot fills but perp doesn't: don't create position
if not (spot_filled and perp_filled):
    logger.error("Incomplete fills — aborting trade")
    results["phase_a_errors"].append(...)
    continue

# If order fill timeout: exit from loop, retry on next cycle
if not await self._wait_for_order_fill(order_id, timeout_sec=30):
    logger.warning(f"Order {order_id} did not fill, skipping")
    return False
```

### DynamoDB-level

```python
# Eventual consistency: monitor re-checks data before acting
if not strategy.is_worth_entering(current_rate_8hr):
    logger.info("Rate dropped, skipping")
    continue

# Atomic updates where possible
table.update_item(
    Key={...},
    UpdateExpression="SET #status = :new, last_updated = :now",
)
```

## Security Considerations

1. **Credentials:**
   - Private key never logged
   - Stored in environment / AWS Secrets Manager
   - JWT tokens expire in 120s

2. **Permissions:**
   - Lambda role: DynamoDB + SNS only
   - No S3, no full IAM, no root access

3. **API:**
   - HTTPS only (enforced by boto3)
   - JWT auth (not bearer tokens)
   - Per-endpoint signing

4. **DynamoDB:**
   - No cross-table access
   - No sensitive data stored (prices only)

## Performance Characteristics

| Operation | Latency | Notes |
|-----------|---------|-------|
| Scan 3 pairs | 5-10s | Parallel API calls |
| Query opportunities | <100ms | Single DDB scan |
| Execute opportunity | 20-30s | Includes 30s fill timeout |
| Monitor 5 positions | 15-20s | Sequential per position |
| Record funding | <50ms | Single DDB update |

**Scaling:**
- Scanner: Linear with # of pairs (currently 3: BTC, ETH, SOL)
- Monitor: Linear with # of open positions (configurable max 3)
- DynamoDB: On-demand billing, no capacity planning needed

## Monitoring & Observability

### CloudWatch Logs

```
/aws/lambda/funding-rate-scanner
  INFO: Starting scan
  INFO: BTC-PERP-INTX: 8hr rate = 0.0003, APR = 32.85%
  INFO: Opportunity written to DynamoDB (APR=32.85%)
  ERROR: API error scanning ETH: ...

/aws/lambda/funding-rate-monitor
  INFO: Executing opportunity for BTC-PERP-INTX
  INFO: Spot BUY order placed: order-123 for 100.0 USD
  INFO: Position opened: pos-uuid for BTC-PERP-INTX (32.85% APR)
  INFO: Monitoring position pos-uuid (BTC-PERP-INTX)
  INFO: Recorded funding payment: $0.03 (total: $0.09)
  INFO: Position closed: pos-uuid | Collected: $X over Y days
```

### CloudWatch Metrics

Via SNS alerts:
- Opportunities found
- Positions opened
- Funding collected
- Positions closed
- Errors encountered

### Alarms

- Scanner error rate > 0
- Monitor error rate > 0
- DynamoDB throttling
- Lambda timeout

## Integration with Other Modules

Shares:
- **SNS topic** (`SENTINEL_SNS_ARN`) for alerts
- **AWS account** and region
- **Python 3.11** runtime

Separate:
- DynamoDB tables (funding-rate-* namespace)
- Lambda functions (separate scanner, monitor)
- EventBridge rules (own schedule)

Can coexist with `carpet_bagger` and `bracket_buster` without conflicts.

---

**Architecture Version:** 1.0
**Last Updated:** 2026-04-03
**Status:** Production-ready
