# TraderBot

An automated trading system with two independent strategies running on AWS Lambda (us-east-2):

1. **Sentiment Trader** — buys stocks and options on Public.com based on multi-source news sentiment
2. **Carpet Bagger** — trades Kalshi prediction markets on live sports game outcomes

Both strategies share the same Lambda function, EventBridge schedules, SNS email alerts, and Secrets Manager.

---

## Strategy 1: Sentiment Trader

### What it does

Runs 4 automated windows per weekday plus an evening suggestion engine. Each window:
1. Fetches account state from Public.com (buying power, open positions)
2. Scores each ticker in the watchlist using 6 sentiment sources
3. Places trades automatically for strong signals (no human confirmation)
4. Emails a summary of signals detected, trades placed, and current portfolio

### Sentiment scoring

Each ticker gets a blended score from −1.0 (very bearish) to +1.0 (very bullish):

| Source | Weight | What it measures |
|---|---|---|
| Price action (1-day) | 50% | % move vs prior close via Public.com real-time quotes |
| Finnhub news sentiment | 20% | Pre-computed bullish/bearish % per ticker + EPS surprise modifier |
| Claude (macro headlines) | 10% | AI interpretation of top NewsAPI headlines |
| MarketAux entity sentiment | 10% | Entity-level news sentiment (company-specific) |
| Polygon grouped bars | 5% | Prior-day market data keyword score |
| WallStreetBets pulse | 5% | Mention count momentum on r/wsb (ApeWisdom) |

**Dynamic weight normalization**: If a source returns no data, its weight is redistributed to other active sources. When both Finnhub and MarketAux fail, Claude scores tickers individually from price moves and macro context.

**Earnings modifier**: If a ticker reports earnings within 7 days (Alpha Vantage calendar), the buy/sell threshold is **raised by 0.15** — making it *harder* to trigger a trade near binary events (IV crush risk, earnings gap risk).

**EPS surprise modifier**: Finnhub's most recent EPS beat/miss adds ±0.10–0.20 directly to the blended score.

### Trade logic

| Score | Signal | Action |
|---|---|---|
| ≥ 0.35 | Strong bullish | Try to buy a **call option** (14–45 DTE, ATM or slightly OTM, 1 contract). Falls back to stock if no affordable contract exists |
| ≥ 0.25 | Bullish | Buy **stock** at market |
| ≤ −0.20 | Bearish (ETFs only) | Open a **bear put spread** — BUY ATM put + SELL put 2% lower, same expiration, 14–45 DTE. Restricted to SPY, QQQ, IWM only |
| Between | Neutral | No trade |

**No duplicate guard on adds**: The bot will buy more of a ticker it already holds if the signal is strong enough. The 10% position size cap per trade is the concentration control.

### Position sizing

- **Position size**: 10% of total account value per trade
- **Daily loss limit**: 10% of account — halts all new trades for the day if breached
- **Stop-loss**: 7% drawdown on any single position → auto-close at EOD (3:45pm scan), subject to PDT guard below

### ⚠️ Pattern Day Trader (PDT) protection — DO NOT REMOVE

FINRA PDT rules apply to accounts **under $25,000**: 4+ same-day round trips (buy + sell same security same day) within 5 business days triggers a PDT flag that can **freeze the account for 90 days**.

**The guard is automatic and self-disabling:**
- `_get_today_buy_symbols(account_equity)` is called before any intraday or EOD sell
- If `account_equity >= $25,000` → returns empty set immediately, no restriction, no DynamoDB query
- If `account_equity < $25,000` → queries DynamoDB `trading-bot-logs` for today's `order_placed` entries; those tickers are excluded from all sells that day
- If DynamoDB is unreachable on a sub-$25k account → **all intraday sells are blocked** (fail-safe)

**When adding any sell logic** (intraday rotation, stop-loss, take-profit, signal reversal, etc.), always call `_get_today_buy_symbols(portfolio_value)` first and skip tickers in that set. Never sell a position on the same calendar day it was purchased.

### Watchlist

```
AAPL, MSFT, TSLA, NVDA, AMD, META, AMZN, GOOGL   # Mega-cap tech
SPY, QQQ, IWM                                      # Broad market ETFs (also bear spread targets)
BAC, C, INTC                                       # Financials ($20–70)
PLTR, SOFI                                         # AI/data ($20–35)
SNAP, LYFT, F                                      # Consumer/social ($8–15)
PFE, MRNA                                          # Biotech ($25–45)
RIVN                                               # EV ($10–15)
```

### Evening suggestion engine (6:45pm ET weekdays, 10am Saturday)

After market close, Claude Haiku scans current macro headlines and generates 3 trade ideas ($2–$5 each), emailed with one-click approval links (HMAC-signed, 20-hour expiry).

**Approve All link**: A single link at the bottom of the email approves all 3 suggestions at once. Each link and the batch link are signed independently with HMAC-SHA256 so they can't be tampered with.

**Price check at approval time**: When a link is clicked, the handler fetches current price from Public.com and displays it in the confirmation page — so Rob can see if the stock has moved significantly since the suggestion was sent.

---

## Strategy 2: Carpet Bagger

### Core thesis

When a pre-game favorite is winning comfortably mid-game, Kalshi prediction markets underreact — probability spikes above fair value for a short window. The bot scouts favorites before games, waits for the game to start and the in-game probability to confirm outperformance, buys in, then exits at a take-profit or stop-loss.

### Supported sports

| Kalshi series | Sport |
|---|---|
| `KXNHLGAME` | NHL individual game winner |
| `KXNBAGAMES` / `KXNBAGAME` | NBA individual game winner |
| `KXNCAABGAME` / `KXNCAABBGAME` | NCAAB men's basketball (regular season + March Madness) |
| `KXNCAAWBGAME` | NCAAW women's basketball |
| `KXMLBGAME` | MLB individual game winner |

Excluded: golf (KXPGAH2H, KXLPGAH2H), racing (KXNASCAR, KXF1), MLB Spring Training (KXMLBSTGAME) — these are multi-competitor or non-head-to-head formats.

### Step 1 — Scout (8am ET daily, 7 days/week)

Scans all open Kalshi game markets for **today's games only** (calendar date filter, ET timezone).

**Filter**: `yes_ask` between **55%–75%** pre-game

- Below 55%: too close to a coin flip, not enough edge
- Above 75%: limited in-game upside; the market already agrees

Qualifying markets are written to DynamoDB (`carpet-bagger-watchlist`) with `status = watching`.

### Step 2 — Monitor (every 5 min, 11am–midnight ET, 7 days/week)

For each `watching` record, the monitor applies three sequential guards before buying:

1. **Game started**: `open_time` must have passed — no buying at pre-game prices
2. **In-game outperformance**: current `yes_ask` must be ≥ `pre_game_prob` — the team must be performing at least as well as expected
3. **Buy tier reached**: current `yes_ask` must cross a tier threshold

**Tiered position sizing** (all capped at $1.00/position):

| In-game probability | Fraction of available float |
|---|---|
| 80%–85% | 50% |
| 85%–90% | 75% |
| 90%–100% | 100% |

All three guards must pass before a buy order fires.

### Step 3 — Exit logic

On every 5-minute tick for each `bought` position:

**Settlement**: Market finalized → record win/loss, send email.
- Won: P&L = `(1.0 − entry_price) × contracts`
- Lost: P&L = `−entry_price × contracts`

**Take-profit** (sport-specific):

| Sport | Take-profit threshold |
|---|---|
| NHL, NBA | 92% |
| NCAAB, NCAAW | 93% |
| MLB | 90% |

**Stop-loss**: If `yes_ask` drops below `entry_price` (i.e., the in-game odds fall below what we paid), the position is sold for a near-zero loss. This is a break-even protection exit, not a fixed floor — prevents holding a deteriorating position while capping the max loss to a few cents per contract.

### The math

Kalshi contracts pay $1.00 if YES resolves, $0.00 if NO.

**Entry at 82%, exit at 92% (take-profit):**
- Gain: $0.10/contract = **+12.2% return**

**Entry at 82%, stop-loss (odds fall to 81%):**
- Loss: $0.01/contract = **−1.2% loss** (near break-even)

**Entry at 82%, settlement WIN:**
- Gain: $0.18/contract = **+22.0% return**

**Entry at 82%, settlement LOSS:**
- Loss: $0.82/contract = **−100% loss on that contract**

With $1.00 max position and 10 max positions, total exposure is $10.

### Risk parameters

- **Max positions**: 10 simultaneous open bets
- **Max position size**: $1.00 per position (hard cap regardless of float size)
- **Available float**: live Kalshi balance minus total exposure of open positions

### Position reconciliation

On every monitor tick, the bot reconciles Kalshi's live position list against DynamoDB. If Kalshi shows an open position that DDB has lost track of, the record is restored to `status = bought` so the monitor continues managing it. Golf, racing, and spring training positions are excluded from reconciliation.

---

## Infrastructure

```
AWS Lambda (us-east-2)       trading-bot-sentiment (Python 3.12, 300s timeout, 256MB)
EventBridge Scheduler        All cron triggers (DST-aware, America/New_York timezone)
DynamoDB (us-east-2)         carpet-bagger-watchlist (hash key: market_ticker)
Secrets Manager (us-east-2)  trading-bot/secrets (all API keys + SUGGESTION_TOKEN_SECRET)
SNS (us-east-1)              TraderBot topic → Rob's email
API Gateway HTTP API         trading-bot-approval (ID: 09h5xhojm2) → /approve endpoint
```

### Schedule

| Schedule name | Time (ET) | Action |
|---|---|---|
| `trading-bot-pre-market` | 8:00am Mon–Fri | Sentiment scan — no trades (market closed) |
| `trading-bot-market-open` | 9:35am Mon–Fri | Sentiment scan + trades |
| `trading-bot-midday` | 12:00pm Mon–Fri | Sentiment scan + trades |
| `trading-bot-eod` | 3:45pm Mon–Fri | Stop-loss review + EOD recap email with Claude narrative |
| `trading-bot-evening` | 6:45pm Mon–Fri | Evening suggestion engine (3 ideas + approve links) |
| `trading-bot-weekend` | 10:00am Saturday | Weekend suggestion engine |
| `carpet-bagger-scout` | 8:00am daily | Carpet Bagger: scan Kalshi for today's games |
| `carpet-bagger-monitor` | Every 5 min, 11am–midnight daily | Carpet Bagger: in-game probability check + trade |
| `carpet-bagger-summary` | 11:59pm daily | Carpet Bagger: nightly P&L digest |

### Emails sent

**Sentiment Trader**: Scan summary at each window (signals, trades, portfolio). EOD includes Claude Haiku narrative. Evening suggestion emails include 3 ideas with individual + batch approve links.

**Carpet Bagger**: Game settlement outcomes (won/lost). Nightly summary at 11:59pm. No emails for individual buys, sells, or sell errors — only results and the nightly digest.

---

## Key files

```
lambda_function.py              Lambda entry point: secrets injection, window routing, HTTP approval routing
config/settings.py              All thresholds, watchlist, schedule times, API keys
sentiment/scanner.py            6-source blended sentiment score with dynamic weight normalization
sentiment/earnings.py           Alpha Vantage earnings calendar → threshold modifier
sentiment/finnhub_news.py       Finnhub pre-computed sentiment + EPS surprise modifier
sentiment/marketaux.py          MarketAux entity-level news sentiment
sentiment/wsb_pulse.py          ApeWisdom WSB mention momentum
sentiment/market_data.py        Public.com quotes + Polygon prev-close bars
sentiment/news_macro.py         NewsAPI → Claude Haiku macro score
scheduler/jobs.py               Trade execution: stocks, calls, bear put spreads (ETFs only), EOD stop-loss
scheduler/suggestions.py        Evening suggestion engine: headlines + Claude → 3 ideas + HMAC URLs
scheduler/weekly_review.py      Sunday performance recap vs HYSA benchmark
api/approval_handler.py         HTTP handler: validates HMAC token → places trade → HTML response (single + batch)
carpet_bagger/scout.py          8am game scout: Kalshi series discovery, pre-game filter, DynamoDB write
carpet_bagger/monitor.py        5-min in-game monitor: game-started guard, outperformance check, tier buy, take-profit/stop-loss
carpet_bagger/kalshi_client.py  RSA-PSS signed Kalshi API v2 client
carpet_bagger/strategy.py       Sport rules, tier thresholds, PRE_GAME_MIN/MAX, take-profit by sport
carpet_bagger/models.py         WatchlistRecord dataclass + DynamoDB serialization
deploy.sh                       Builds zip, deploys Lambda, upserts all EventBridge schedules
watchlist.py                    Local CLI: shows current Carpet Bagger watchlist (uses AWS CLI, no boto3 needed)
test_scan.py                    Local sentiment scan test (no orders placed)
test_order.py                   Manual order test (places real orders — use carefully)
```

## Running locally

```bash
source .venv/bin/activate

# Sentiment scan (read-only)
python3 test_scan.py

# Carpet Bagger watchlist
python3 watchlist.py           # watching + bought
python3 watchlist.py --all     # include today's closed

# Deploy code + update all EventBridge schedules
./deploy.sh
```

## Manual operations

**Force-sell a Carpet Bagger position** (via AWS Lambda console or CLI):
```json
{"window": "carpet_bagger_force_sell", "ticker": "KXNCAABGAME-...", "contracts": 1, "entry_price": 0.82}
```

**Force-scout now**:
```json
{"window": "carpet_bagger_scout"}
```

**Force-run monitor tick**:
```json
{"window": "carpet_bagger_monitor"}
```
