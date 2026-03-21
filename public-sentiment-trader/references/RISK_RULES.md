# Risk Rules

## Hard Limits (Always Enforced)

| Rule | Default | Variable |
|------|---------|----------|
| Max position size | 15% of account | `MAX_POSITION_PCT` |
| Daily loss limit | 10% of account | `DAILY_LOSS_LIMIT_PCT` |
| Stock stop-loss | −7% | `STOP_LOSS_PCT` |
| Options stop-loss | −50% | `_OPTIONS_STOP_LOSS_PCT` |
| Options profit-take | +100% AND +$0.20/share | `_OPTIONS_PROFIT_TAKE_PCT` |
| Options time exit | ≤ 14 DTE | `_OPTIONS_PROFIT_TAKE_DTE` |

## Kill Switch

Set `TRADING_PAUSED=true` in AWS Secrets Manager → all Lambda scans return immediately
without placing any orders. Takes effect on next invocation. No redeploy needed.

```bash
# Pause
aws secretsmanager get-secret-value --secret-id trading-bot/secrets --region us-east-2 \
  --query 'SecretString' --output text | \
  python3 -c "import json,sys; d=json.load(sys.stdin); d['TRADING_PAUSED']='true'; print(json.dumps(d))" | \
  aws secretsmanager put-secret-value --secret-id trading-bot/secrets --region us-east-2 \
  --secret-string file:///dev/stdin

# Resume
# (same command with 'false')
```

## PDT Protection (Pattern Day Trader)

Accounts under $25,000 are subject to FINRA PDT rules: 4+ same-day round trips
in a 5-day rolling window triggers a 90-day account freeze.

The bot enforces this automatically:
- Before any intraday sell, queries DynamoDB for tickers bought today
- Skips the sell if the ticker was bought same calendar day
- Returns `None` on DynamoDB failure → blocks ALL sells (fail-safe)

## Approval-First Selling

The bot **never auto-sells** open positions. When a position triggers the
stop-loss, profit-take, or rotation logic:

1. An HMAC-signed sell link is emailed (4-hour expiry)
2. You click to approve
3. The handler re-verifies the token, then places a LIMIT sell at current bid

This prevents accidental sells and keeps a human in the loop.

## Options Approval Flow

Call option buys are also human-approved:
1. Bot detects strong bullish signal + emails HMAC-signed link (2-hour expiry)
2. At click time, bot re-fetches current price
3. If price moved >15% against the trade direction → blocks the order
4. Otherwise: selects best ATM call within budget, places LIMIT at mid-price

## Position Concentration

The duplicate guard uses base-ticker extraction to prevent doubling up:
- Opening a call on AAPL counts as AAPL exposure — no additional stock buy
- Options positions on the same underlying are blocked

## Carpet Bagger Risk

- Max $1.00 per game (configurable via `CARPET_BAGGER_MAX_POSITION`)
- Stop-loss at 0.45 implied probability (market has flipped)
- Only games resolving within 36 hours (no season futures)
- Blocked sports: MLB (capital lockup), NCAA Baseball (misidentified as basketball)
- BUY_CUTOFF_HOUR_ET = 23 (no new positions after 11 PM ET)
