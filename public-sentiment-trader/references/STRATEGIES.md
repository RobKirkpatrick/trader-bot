# Trading Strategies

## Sentiment Blending (6 Sources)

Each ticker is scored on a −1.0 to +1.0 scale by blending:

| Source | Weight | Description |
|--------|--------|-------------|
| Price action | 50% | Intraday % change vs. previous close (Polygon grouped daily) |
| Finnhub | 20% | Pre-computed news sentiment + EPS surprise modifier |
| Claude macro | 10% | Claude Haiku reads top-headlines, scores broad market mood |
| MarketAux | 10% | Entity-level news sentiment (100 req/day free tier) |
| Polygon | 5% | Polygon news article sentiment |
| WSB / ApeWisdom | 5% | Reddit WallStreetBets mention counts (no API key needed) |

### Thresholds (configurable via .env)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SENTIMENT_BUY_THRESHOLD` | 0.25 | Minimum score to flag bullish + buy stock |
| `SENTIMENT_OPTIONS_CALL_THRESHOLD` | 0.35 | Minimum score to send call approval link |
| `SENTIMENT_SELL_THRESHOLD` | −0.20 | Maximum score to flag bearish (puts disabled) |

### Earnings Modifier

If earnings are within 3 calendar days, the buy threshold is raised by +0.15
to avoid binary event exposure. This is checked via Alpha Vantage earnings calendar.

---

## Trade Execution Flow

```
Score ≥ 0.35 → Claude Sonnet agent evaluates (live quote + top 5 option contracts)
               Agent says execute=true, confidence=high/medium → stock buy + email call link
               Agent says execute=true, confidence=low → SNS alert only (no order)
               Agent says execute=false → log reason, skip

Score 0.25–0.34 → Stock buy (no options) if risk checks pass

Score < 0.25 → No action
```

---

## VIX Regime Sizing

| VIX Level | Effect |
|-----------|--------|
| < 20 | Full position sizing |
| 20–29 | 20% size reduction, options still enabled |
| ≥ 30 | 40% size reduction, call options blocked |

---

## Risk Tolerance Profiles

Set `RISK_TOLERANCE` in your `.env`:

| Profile | Max Position % | Options | Notes |
|---------|---------------|---------|-------|
| `conservative` | 5% of account | Disabled | Stocks only, extra VIX caution |
| `moderate` | 10–15% | Calls only | Default |
| `aggressive` | 20% | Calls | Higher concentration allowed |

---

## Intraday Rotation

At each scan, the bot checks open positions for:

1. **Signal reversal** — score drops ≤ −0.20 (bearish flip). For options, only exits if also down ≥ 25% (avoids exiting on macro noise alone).
2. **Rotation** — score < 0.15 AND position is at a profit AND there are stronger new signals (≥ 0.35) for unheld tickers.

Sells are sent as HMAC-signed approval links — you click to confirm.

---

## Options Strategy (Calls Only)

- **DTE window**: 14–45 days to expiration
- **Strike selection**: ATM or first OTM call within budget
- **Order type**: LIMIT at mid-price (bid+ask)/2
- **Profit-take**: Auto-close if up ≥ 100% AND gain ≥ $0.20/share
- **Stop-loss**: Auto-close if down ≥ 50%
- **Time exit**: Auto-close if ≤ 14 DTE remaining (avoid theta acceleration)

Puts and put spreads are disabled (configurable via `OPTIONS_CALLS_ENABLED`).

---

## Carpet Bagger (Kalshi Sports Markets)

Trades Kalshi YES contracts on in-game sports favorites.

**Entry rules:**
- Implied probability 55–80% (yes_ask price)
- Game must have started (in-game momentum window varies by sport)
- Market resolves within 36 hours (no futures/season bets)

**Exit rules:**
- Resting SELL limit at take-profit threshold (sport-specific, 0.82–0.90)
- Stop-loss: sell if yes_ask drops below 0.45 (market flipped)

**Sport windows:**

| Sport | Buy Window | Take Profit |
|-------|-----------|-------------|
| NBA | Q2 start → Q3 end | 0.85 |
| NHL | Period 1 end → Period 3 start | 0.85 |
| NCAAB | 10 min into H1 → H2 start | 0.82 |
| NCAAW | 10 min into H1 → H2 start | 0.82 |

Max position: `CARPET_BAGGER_MAX_POSITION` (default $1.00 to avoid overnight lockup).
