# Public Sentiment Trader

**An AI-powered trading bot that blends six market signals, asks Claude to decide, and lets you approve with one click.**

Built on the [Public.com API](https://public.com) and deployed to AWS Lambda. Runs autonomously on a schedule or interactively as a [Claude Agent Skill](public-sentiment-trader/SKILL.md).

---

## Investment Thesis

Markets overreact to short-term sentiment. A ticker that drops 3% on mildly negative news — while earnings are clean, analyst sentiment is neutral, and WSB is quiet — is more likely to mean-revert than continue falling. Conversely, a ticker with improving price action, a fresh earnings beat, and unusual social mention velocity often has further to run.

The bot operationalizes this thesis:
1. **Score** every ticker in the watchlist using six independent sentiment sources
2. **Decide** via a Claude Sonnet agent with live price, top options contracts, and macro context
3. **Gate** high-conviction trades behind a human approval email (HMAC-signed, one-click)
4. **Protect** capital with a PDT guard, daily loss limit, and VIX-aware sizing

No signal source alone is reliable. The edge comes from **blending** them with dynamic weight normalization — if Finnhub is down, its weight redistributes to MarketAux and Claude; if both are down, Claude scores from price action and macro context alone.

---

## How It Works

### Sentiment Scoring

Each ticker gets a blended score from **−1.0** (very bearish) to **+1.0** (very bullish):

| Source | Weight | Data |
|--------|--------|------|
| Price action (1-day) | 50% | % move vs prior close — Public.com real-time quotes + Polygon grouped bars |
| Finnhub news sentiment | 20% | Pre-computed bullish/bearish % per ticker |
| Claude (macro headlines) | 10% | NewsAPI top headlines → Claude Haiku interpretation |
| MarketAux entity sentiment | 10% | Company-specific news sentiment |
| Polygon keyword score | 5% | Prior-day market data tone |
| WallStreetBets pulse | 5% | r/wsb mention momentum (ApeWisdom, no key needed) |

**Earnings modifier**: If earnings are within 7 days (Alpha Vantage calendar), the buy threshold rises by 0.15 — reducing exposure to binary events.

**EPS surprise modifier**: Finnhub's most recent EPS beat/miss adds ±0.10–0.20 directly to the blended score.

### Trade Decision

| Score | Signal | Action |
|-------|--------|--------|
| ≥ 0.35 | Strong bullish | Email HMAC-signed call option approval link |
| ≥ 0.25 | Bullish | Buy stock at market |
| ≤ −0.20 | Bearish (ETFs only) | Bearish signal logged — no auto-sell |
| Between | Neutral | No action |

For call option approvals, clicking the email link:
1. Re-fetches current price from Public.com
2. Blocks if price moved >15% adverse since suggestion
3. Selects the best ATM call within budget (1 contract, 14–45 DTE)
4. Places a LIMIT order at mid-price

### Evening Suggestions

At **7:00 PM ET** weekdays, Claude Sonnet reviews today's scan log (signals, macro score, current portfolio) and emails 3 trade picks — each with an individual approve link and a one-click **Approve All**. Links expire in 20 hours. Nothing executes until you click.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   AWS Lambda (us-east-2)                    │
│                                                             │
│  EventBridge Scheduler (America/New_York)                   │
│  ├── 08:00 ET  Pre-market scan (no trades)                  │
│  ├── 09:35 ET  Market open — execute signals                │
│  ├── 12:00 ET  Midday — rotation check                      │
│  ├── 15:30 ET  EOD — stop-loss review + P&L email           │
│  ├── 19:00 ET  Evening suggestions (Mon–Fri)                │
│  └── 10:00 ET  Weekend suggestions (Saturday)               │
│                                                             │
│  Secrets Manager ← all API keys (no hardcoded secrets)     │
│  DynamoDB       ← trade log + PDT guard + carpet bagger     │
│  SNS            ← email delivery                            │
│  API Gateway    ← /approve endpoint (HMAC validation)       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────┐
│     Interactive (Claude Skill)      │
│                                     │
│  public-sentiment-trader/           │
│  ├── SKILL.md          ← entry point│
│  ├── scripts/          ← 5 tools    │
│  └── references/       ← 3 docs     │
└─────────────────────────────────────┘
```

---

## Claude Agent Skill

This repo ships as a [Claude Agent Skill](public-sentiment-trader/SKILL.md). Add it to Claude Code and use plain English to interact with your portfolio:

```
"Show me my current positions and P&L"
"Run a sentiment scan on the watchlist"
"What's the options chain on NVDA?"
"Check my risk limits before market open"
```

Claude reads `SKILL.md` and invokes the matching script with the right arguments. No manual flag-hunting.

---

## Quick Start

### Interactive (Claude Skill)

> Requires Python 3.10+ and VS Code with the Claude Code extension installed.

```bash
git clone https://github.com/RobKirkpatrick/trader-bot
cd trader-bot
code .                                          # open in VS Code
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                            # fill in your keys
```

**Verify your setup (no API keys needed):**
```bash
python -c "import requests, anthropic, boto3, dotenv; print('✅ All dependencies installed')"
```

Then use Claude Code with the `public-sentiment-trader/` skill directory.

### Autonomous (AWS Lambda)

1. Fill in `.env` with your API keys (see [.env.example](.env.example))
2. Create a Secrets Manager secret named `trading-bot/secrets` with the same keys
3. Run `./deploy.sh` — builds the zip, deploys to Lambda, and upserts all EventBridge schedules

```bash
./deploy.sh
```

That's it. The bot is live.

### Run scripts directly

```bash
source .venv/bin/activate

# Portfolio + P&L
python public-sentiment-trader/scripts/get_portfolio.py

# Live sentiment scan
python public-sentiment-trader/scripts/run_sentiment_scan.py

# Quotes (with optional options chain)
python public-sentiment-trader/scripts/get_quotes.py AAPL MSFT --options

# Risk check (VIX, PDT status, position limits)
python public-sentiment-trader/scripts/check_risk.py

# Place an order (always runs preflight first)
python public-sentiment-trader/scripts/place_order.py --symbol AAPL --side buy --dollars 50 --confirm
```

---

## Install as a Claude Skill

After cloning (step above), add the skill to Claude Code by pointing it at the local skill directory:

1. Open **Claude Code** in VS Code (Cmd+Shift+P → `Claude: Focus on Chat`)
2. Go to **Settings** → **Skills** → **Add Skill**
3. Paste the local path to the skill directory:
```
   /path/to/trader-bot/public-sentiment-trader
```
   *(replace `/path/to/` with wherever you cloned the repo)*
4. Claude will read `SKILL.md` automatically — try:
```
   "Show me my current positions and P&L"
   "Run a sentiment scan on the watchlist"
   "Check my risk limits before market open"
```

No flags, no scripts — just plain English.

---

## Configuration

All config lives in `.env` (local) or AWS Secrets Manager (Lambda). No hardcoded values.

### Required

```bash
PUBLIC_API_SECRET=       # Public.com API key (Account Settings → Security → API)
PUBLIC_ACCOUNT_ID=       # Brokerage account ID (e.g. 5OP12345)
ANTHROPIC_API_KEY=       # Claude agent decisions + macro scoring
```

### Market data (at least one)

```bash
POLYGON_API_KEY=         # News sentiment + prev-close bars (free tier: 5 req/min)
FINNHUB_API_KEY=         # Pre-computed news sentiment + EPS surprises (free: 60 req/min)
MARKETAUX_API_KEY=       # Entity-level news sentiment (free: 100 req/day)
NEWS_API_KEY=            # NewsAPI headlines for macro scoring (free: 100 req/day)
ALPHA_VANTAGE_API_KEY=   # Earnings calendar (free: 25 req/day)
```

### Risk profile (optional)

```bash
RISK_TOLERANCE=moderate          # conservative | moderate | aggressive
OPTIONS_CALLS_ENABLED=true       # set false to disable call option orders
CARPET_BAGGER_ENABLED=true       # set false to disable Kalshi sports trading
CARPET_BAGGER_MAX_POSITION=1.00  # max dollars per Kalshi game
TRADING_PAUSED=false             # kill switch — set true to halt all trading instantly
```

See [.env.example](.env.example) for the full list with descriptions.

---

## Kill Switch

To halt all trading without redeploying:

```bash
# Pause (edit the value in Secrets Manager)
aws secretsmanager get-secret-value \
  --secret-id trading-bot/secrets --region us-east-2 \
  --query 'SecretString' --output text | \
  python3 -c "import json,sys; d=json.load(sys.stdin); d['TRADING_PAUSED']='true'; print(json.dumps(d))" | \
  aws secretsmanager put-secret-value \
  --secret-id trading-bot/secrets --region us-east-2 \
  --secret-string file:///dev/stdin

# Resume: same command with 'false'
```

Takes effect on the next Lambda invocation. No redeploy needed.

---

## Risk Controls

| Control | Default | Notes |
|---------|---------|-------|
| Max position size | 15% of account | Configurable via `RISK_TOLERANCE` |
| Daily loss limit | 10% | Halts new buys if breached |
| Stock stop-loss | −7% | EOD auto-close (subject to PDT guard) |
| Options stop-loss | −50% | Auto-close on next scan |
| Options time exit | ≤ 14 DTE | Closes options approaching expiry |
| VIX > 25 | −20–40% sizing | Regime-aware position reduction |
| PDT guard | Blocks same-day sells | Prevents 90-day account freeze on accounts < $25k |

**PDT guard**: Accounts under $25,000 are subject to FINRA PDT rules — 4+ same-day round trips in 5 days triggers a 90-day freeze. The bot queries DynamoDB for today's buys before any sell and skips tickers purchased the same day. If DynamoDB is unreachable, **all sells are blocked** (fail-safe).

---

## Carpet Bagger (Kalshi Sports)

An optional strategy that trades Kalshi prediction markets on in-game sports outcomes. **The goal is to generate enough to cover the bot's compute costs** (AWS Lambda, Claude API) — so the Sentiment Trader effectively runs for free. The Kalshi float is kept small ($50–$100) and profits are withdrawn regularly, leaving only a working float at risk.

**Thesis**: When a pre-game favorite is winning mid-game, Kalshi markets briefly underreact — implied probability spikes above fair value for a short window. The bot buys at 80–90% implied probability, targeting a take-profit exit at 90–93% (sport-dependent), with a stop-loss if the odds deteriorate.

**Math**: Entry at 82%, take-profit exit at 92% = **+12.2% return** on the contract. Entry at 82%, settlement win = **+22.0%**. Settlement loss = −100% on that contract, but the position cap is $1.00.

```bash
CARPET_BAGGER_ENABLED=true
CARPET_BAGGER_MAX_POSITION=1.00   # max $ per game
KALSHI_API_KEY=your_key
KALSHI_RSA_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...
```

Supported: NBA, NHL, NCAA Men's Basketball, NCAA Women's Basketball. See [RISK_RULES.md](public-sentiment-trader/references/RISK_RULES.md) for blocked sports and position limits.

---

## Reference Docs

- [API_REFERENCE.md](public-sentiment-trader/references/API_REFERENCE.md) — Public.com endpoints, OSI symbol format, error handling
- [STRATEGIES.md](public-sentiment-trader/references/STRATEGIES.md) — Sentiment weights, thresholds, VIX regime sizing
- [RISK_RULES.md](public-sentiment-trader/references/RISK_RULES.md) — Hard limits, kill switch, PDT protection

---

## Project Structure

```
lambda_function.py              Lambda entry: secrets injection, window routing, HTTP approval
config/settings.py              Thresholds, watchlist, schedule times, API keys
sentiment/scanner.py            6-source blended sentiment score
sentiment/finnhub_news.py       Finnhub sentiment + EPS surprise modifier
sentiment/marketaux.py          MarketAux entity-level sentiment
sentiment/wsb_pulse.py          ApeWisdom WSB mention momentum
sentiment/news_macro.py         NewsAPI → Claude Haiku macro score
sentiment/earnings.py           Alpha Vantage earnings calendar → threshold modifier
sentiment/market_data.py        Public.com quotes + Polygon prev-close bars
core/agent.py                   Claude Sonnet trade decision agent
scheduler/jobs.py               Trade execution: stocks, calls, EOD stop-loss
scheduler/suggestions.py        Evening suggestion engine (3 picks + HMAC links)
api/approval_handler.py         HTTP handler: validates HMAC → places trade → HTML
carpet_bagger/scout.py          8am Kalshi game scout
carpet_bagger/monitor.py        5-min in-game monitor: buy/take-profit/stop-loss
carpet_bagger/kalshi_client.py  RSA-PSS signed Kalshi API v2 client
scripts/hormuz_trade.py         Configurable macro trade: stock + call option
scripts/hormuz_monitor.py       Monitor an open macro position
public-sentiment-trader/        Claude Agent Skill (SKILL.md + scripts + references)
deploy.sh                       Build + deploy Lambda + upsert EventBridge schedules
```

---

## API Keys (All Free Tiers)

| Service | Purpose | Free tier |
|---------|---------|-----------|
| [Public.com](https://public.com) | Brokerage (quotes, orders, options) | Required |
| [Anthropic](https://console.anthropic.com) | Claude agent decisions | Pay-per-use |
| [Polygon.io](https://polygon.io) | News sentiment + prev-close data | Unlimited historical, 5 req/min |
| [Finnhub](https://finnhub.io) | Pre-computed news sentiment | 60 req/min, no daily cap |
| [MarketAux](https://www.marketaux.com) | Entity-level news sentiment | 100 req/day |
| [NewsAPI](https://newsapi.org) | Macro headline scoring | 100 req/day |
| [Alpha Vantage](https://www.alphavantage.co) | Earnings calendar | 25 req/day |
| [Kalshi](https://kalshi.com) | Sports prediction markets | Required for Carpet Bagger |

ApeWisdom (WSB pulse) requires no API key.
