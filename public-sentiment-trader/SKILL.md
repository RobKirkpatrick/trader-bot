---
name: public-sentiment-trader
description: >
  AI-powered trading bot for Public.com that monitors your watchlist, scores
  tickers across six sentiment signals, and trades on your behalf. Use when
  you want to: check portfolio positions and P&L, run a sentiment scan, fetch
  real-time quotes, automatically place stock orders when conviction is high,
  place call or put options after one-click HMAC-signed email approval, receive
  Claude-curated evening trade suggestions with an approve-all link, or verify
  risk limits before market open. Signals blend price action, Finnhub news,
  Claude macro analysis, MarketAux entity sentiment, Polygon keywords, and
  Reddit/WSB pulse. A companion Kalshi sports strategy trades prediction markets
  to offset compute costs. VIX-aware sizing, PDT guard, daily loss limits, and
  a kill switch protect capital.
---

# Public Sentiment Trader

An autonomous multi-source sentiment trading bot built on the Public.com API.
It runs on AWS Lambda (scheduled scans) and as an interactive Claude skill
(on-demand queries). Supports stocks, ETFs, and call options.

## Architecture

**Autonomous mode** — AWS Lambda fires 4× daily on EventBridge Scheduler:
- `08:00 ET` Pre-market: macro read, watchlist score
- `09:35 ET` Market open: execute bullish signals, send call approval links
- `12:00 ET` Midday: rotation check, EDGAR 8-K scan
- `15:30 ET` EOD: stop-loss review, P&L email, Claude recap

**Interactive mode** — Use the scripts below to query/act on demand.

## Required Environment Variables

```
# Public.com (required)
PUBLIC_API_SECRET=        # Account Settings → Security → API Key
PUBLIC_ACCOUNT_ID=        # Your brokerage account ID (e.g. 5OP12345)

# AI / Sentiment (required for full scan)
ANTHROPIC_API_KEY=        # Claude Sonnet agent decisions + macro scoring

# Market data (at least one required)
POLYGON_API_KEY=          # News sentiment + prev-close data (free tier OK)
FINNHUB_API_KEY=          # Pre-computed news sentiment + EPS surprises (free)
MARKETAUX_API_KEY=        # Entity-level news sentiment (free, 100 req/day)
NEWS_API_KEY=             # NewsAPI headlines for macro scoring (free)

# AWS (required for autonomous Lambda mode)
SNS_TOPIC_ARN=            # SNS topic ARN for email alerts
AWS_REGION=us-east-2

# Optional: approval links for human-in-the-loop trades
SUGGESTION_TOKEN_SECRET=  # Random secret for HMAC-signed approval links
LAMBDA_FUNCTION_URL=      # API Gateway URL for approval handler

# User risk profile (optional — see Risk Rules)
RISK_TOLERANCE=moderate   # conservative | moderate | aggressive
OPTIONS_CALLS_ENABLED=true
CARPET_BAGGER_ENABLED=true
CARPET_BAGGER_MAX_POSITION=1.00
```

See `.env.example` for a complete list with descriptions.

## Quick Start (Interactive)

```bash
git clone https://github.com/RobKirkpatrick/trader-bot
cd trader-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
```

**Portfolio check:**
```bash
python public-sentiment-trader/scripts/get_portfolio.py
```

**Live sentiment scan:**
```bash
python public-sentiment-trader/scripts/run_sentiment_scan.py
```

**Get quotes:**
```bash
python public-sentiment-trader/scripts/get_quotes.py AAPL MSFT TSLA
```

## Core Workflows

### 1. Portfolio + Risk Check
Run `get_portfolio.py` → returns positions, P&L, buying power, open orders.
Then run `check_risk.py` → shows daily loss remaining, position concentration,
PDT round-trip count, VIX regime.

### 2. Sentiment Scan → Trade Decision
Run `run_sentiment_scan.py` → blends 6 sources per ticker → scores −1.0 to +1.0.
Scores above `SENTIMENT_BUY_THRESHOLD` (default 0.25) trigger bullish signals.
Scores above `SENTIMENT_OPTIONS_CALL_THRESHOLD` (default 0.35) also send a
call option approval link via email.

### 3. Place an Order
Run `place_order.py --symbol AAPL --side buy --dollars 50` → preflight checks
buying power → confirm → places market order. Always runs preflight first.

### 4. Options Approval Flow
When a strong bullish signal fires, the bot emails an HMAC-signed approval link.
Clicking it re-evaluates the current price (blocks if >15% adverse drift),
selects the best ATM call within budget, and places a LIMIT order.

### 5. Evening Suggestions
At 7:00 PM ET, Claude Sonnet reviews today's scan log and portfolio, then
emails 3 trade picks with individual approve links + a one-click "Approve All."
Links expire in 20 hours. No trade executes until you click.

## Safety Rules

- Kill switch: set `TRADING_PAUSED=true` in AWS Secrets Manager → all scans halt instantly, no redeploy needed
- HMAC-signed approval links expire in 2 hours (call options) or 20 hours (suggestions)
- PDT guard: never sells a position bought same calendar day (prevents pattern-day-trader flag on accounts < $25k)
- VIX regime: at VIX > 25 position sizes automatically reduce 20–40%
- Daily loss limit: stops new buys if account is down > `DAILY_LOSS_LIMIT_PCT` (default 10%)
- Options stop-loss: auto-closes if option is down > 50% (stocks: 7%)
- All API keys in environment variables — never hardcoded

## Reference Materials

- Full Public.com API reference: `references/API_REFERENCE.md`
- Sentiment blending logic + thresholds: `references/STRATEGIES.md`
- Risk rules + position limits: `references/RISK_RULES.md`

## Carpet Bagger (Kalshi)

An optional module that trades Kalshi sports prediction markets.
Buys in-game favorites (55–80% implied probability) during the momentum window,
sets a resting sell limit at the take-profit threshold, and exits on stop-loss.

- `CARPET_BAGGER_ENABLED=true` to activate
- `CARPET_BAGGER_MAX_POSITION=1.00` — max $1 per game (configurable)
- Supports: NBA, NHL, NCAA Men's + Women's Basketball
- Kalshi credentials: `KALSHI_API_KEY` + `KALSHI_RSA_PRIVATE_KEY`
