# Weather Trader Integration Guide

## Overview

`weather_trader` is a production-ready Python module that trades Kalshi weather prediction markets using National Weather Service (NWS) probabilistic forecasts.

**Core edge**: NWS forecasts are scientifically calibrated. Kalshi weather markets are priced by retail traders with no systematic NWS integration. When NWS says 75% and Kalshi prices it 55%, that's a +20 cent arbitrage.

---

## Module Structure

```
weather_trader/
├── __init__.py              # Module exports
├── strategy.py              # Configuration constants
├── models.py                # Data classes (WeatherPosition, MarketOpportunity, NWSForecast)
├── nws_client.py            # National Weather Service API client
├── market_parser.py         # Kalshi market title parser
├── scanner.py               # Market scanner (runs every 4h)
├── monitor.py               # Position monitor & executor (runs every 6h)
└── CLAUDE_CODE_HANDOFF.md   # This file
```

---

## Integration Steps

### 1. Copy Module to Repository

```bash
cp -r weather_trader/ /path/to/repo/
```

### 2. Install Dependencies

The module requires:
- `aiohttp` (async HTTP client) — already in requirements
- `boto3` (DynamoDB/SNS) — already available in Lambda

For local development, no additional dependencies beyond what the bot already uses.

**Note**: The module avoids `scipy` for Lambda compatibility. Temperature exceedance probabilities are computed using a standard error function approximation (Abramowitz-Stegun), not scipy's `norm.cdf()`.

### 3. Create DynamoDB Tables

The module requires two DynamoDB tables:

```bash
# Create weather-opportunities table
aws dynamodb create-table \
  --table-name weather-opportunities \
  --attribute-definitions AttributeName=opportunity_id,AttributeType=S \
  --key-schema AttributeName=opportunity_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

# Create weather-positions table
aws dynamodb create-table \
  --table-name weather-positions \
  --attribute-definitions AttributeName=position_id,AttributeType=S \
  --key-schema AttributeName=position_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

Optional DynamoDB table for NWS forecast caching (reduces API calls):

```bash
aws dynamodb create-table \
  --table-name nws-forecast-cache \
  --attribute-definitions \
    AttributeName=cache_key,AttributeType=S \
  --key-schema AttributeName=cache_key,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --ttl-specification AttributeName=expires_at,Enabled=true
```

### 4. Update `config/settings.py`

Add these environment variables to your settings:

```python
# Weather trader configuration
WEATHER_TRADER_ENABLED = os.getenv("WEATHER_TRADER_ENABLED", "false").lower() == "true"

# NWS API settings (free, no key required)
NWS_USER_AGENT = "trader-bot/1.0 your-email@example.com"
NWS_CACHE_TTL_MINUTES = int(os.getenv("NWS_CACHE_TTL_MINUTES", "60"))

# DynamoDB tables
WEATHER_OPPORTUNITIES_TABLE = os.getenv("WEATHER_OPPORTUNITIES_TABLE", "weather-opportunities")
WEATHER_POSITIONS_TABLE = os.getenv("WEATHER_POSITIONS_TABLE", "weather-positions")

# Position sizing
MIN_WEATHER_EDGE = float(os.getenv("MIN_WEATHER_EDGE", "0.10"))
MAX_WEATHER_POS = float(os.getenv("MAX_WEATHER_POS", "20.00"))
MAX_WEATHER_SIMULTANEOUS = int(os.getenv("MAX_WEATHER_SIMULTANEOUS", "8"))

# NWS reversal threshold (exit if forecast shifts this much against position)
NWS_REVERSAL_THRESHOLD = float(os.getenv("NWS_REVERSAL_THRESHOLD", "0.15"))
```

### 5. Update `.env.example`

```bash
# Weather Trader
WEATHER_TRADER_ENABLED=false
NWS_USER_AGENT=trader-bot/1.0 your-email@example.com
NWS_CACHE_TTL_MINUTES=60
MIN_WEATHER_EDGE=0.10
MAX_WEATHER_POS=20.00
MAX_WEATHER_SIMULTANEOUS=8
NWS_REVERSAL_THRESHOLD=0.15
```

### 6. Wire to Lambda

In your `lambda_function.py`, add handlers for scanner and monitor:

```python
from weather_trader import run_scanner, run_monitor
from config.settings import WEATHER_TRADER_ENABLED

async def weather_scanner_handler(event, context):
    """Runs every 4 hours via EventBridge."""
    if not WEATHER_TRADER_ENABLED:
        return {"status": "disabled"}

    import boto3
    dynamo = boto3.resource("dynamodb")
    sns = boto3.client("sns")

    return await run_scanner(
        kalshi_client=get_kalshi_client(),
        dynamo_client=dynamo,
        sns_client=sns
    )

async def weather_monitor_handler(event, context):
    """Runs every 6 hours via EventBridge."""
    if not WEATHER_TRADER_ENABLED:
        return {"status": "disabled"}

    import boto3
    dynamo = boto3.resource("dynamodb")
    sns = boto3.client("sns")

    return await run_monitor(
        kalshi_client=get_kalshi_client(),
        dynamo_client=dynamo,
        sns_client=sns
    )
```

### 7. Create EventBridge Schedules

Scanner (every 4 hours):
```bash
aws events put-rule \
  --name weather-scanner \
  --schedule-expression "rate(4 hours)" \
  --state ENABLED

aws events put-targets \
  --rule weather-scanner \
  --targets "Id"="1","Arn"="arn:aws:lambda:REGION:ACCOUNT:function:weather-scanner"
```

Monitor (every 6 hours):
```bash
aws events put-rule \
  --name weather-monitor \
  --schedule-expression "rate(6 hours)" \
  --state ENABLED

aws events put-targets \
  --rule weather-monitor \
  --targets "Id"="1","Arn"="arn:aws:lambda:REGION:ACCOUNT:function:weather-monitor"
```

### 8. Deployment Checklist

- [ ] Copy `weather_trader/` to repo
- [ ] Create DynamoDB tables
- [ ] Update `config/settings.py` with weather trader env vars
- [ ] Update `.env.example`
- [ ] Add scanner/monitor Lambda handlers
- [ ] Create EventBridge schedule rules
- [ ] Test NWS API access manually:
  ```python
  import asyncio
  import aiohttp
  from weather_trader import fetch_nws_forecast, NWSClient

  async def test():
      nws = NWSClient()
      async with aiohttp.ClientSession() as session:
          forecast = await fetch_nws_forecast(
              "NYC", 40.7128, -74.0060, "2026-04-10", nws, session
          )
          print(f"Precip prob: {forecast.precip_probability:.0%}")
          print(f"Temp high: {forecast.forecast_high}°F")

  asyncio.run(test())
  ```
- [ ] Monitor SNS output for 3-5 days with `WEATHER_TRADER_ENABLED=false`
- [ ] Validate edge calculations and NWS parsing
- [ ] Enable with `WEATHER_TRADER_ENABLED=true`

---

## Strategy Configuration

All strategy parameters are in `strategy.py`:

```python
MIN_EDGE = 0.10                                 # 10 cents minimum
MAX_BID_ASK_SPREAD = 0.10                       # Skip markets > 10 cent spread
MIN_POSITION_SIZE = 1.00                        # Minimum order
MAX_POSITION_PER_MARKET = 20.00                 # Max per market
MAX_SIMULTANEOUS_POSITIONS = 8                  # Max open positions
MAX_PCT_BANKROLL = 0.15                         # Max 15% per market

MIN_DAYS_TO_RESOLUTION = 0.5                    # Can enter same-day
MAX_DAYS_TO_RESOLUTION = 7                      # NWS accuracy limit
OPTIMAL_ENTRY_WINDOW_DAYS = (1, 5)              # Best edge 1-5 days out

NWS_REVERSAL_THRESHOLD = 0.15                   # Exit if forecast shifts 15+ cents against position

NWS_CACHE_TTL_MINUTES = 60                      # Cache forecasts for 1 hour
```

Adjust these based on:
- Market liquidity and bid-ask spreads
- Risk tolerance and bankroll size
- Desired position concentration

---

## How It Works

### Phase 1: Scanner (every 4 hours)

1. Search Kalshi for weather-related markets
2. Parse each market title to extract: city, weather type, threshold, direction, resolution date
3. Validate city (must be in CITY_COORDS) and resolution date (1-7 days out)
4. Fetch NWS forecast for that city and date
5. Calculate NWS probability for YES outcome
6. Compute edge: `nws_prob - kalshi_ask_price`
7. Store opportunities with edge > MIN_EDGE in DynamoDB
8. Send SNS summary: `"WEATHER SCAN: 4 markets found — NYC rain (edge: +0.18), ..."`

### Phase 2: Monitor (every 6 hours)

**Part A — Execute:**
1. Fetch open opportunities from DynamoDB
2. Check position count and bankroll usage
3. Re-fetch NWS to confirm edge hasn't collapsed
4. Calculate position size based on edge strength
5. Place Kalshi limit order
6. Write WeatherPosition to DynamoDB
7. Send SNS: `"WEATHER BET PLACED: NYC rain Apr 10 — YES @ 0.55 (NWS: 73%, edge: +0.18)"`

**Part B — Monitor:**
1. Fetch all open positions
2. Re-fetch NWS forecasts
3. If NWS probability shifted >15 cents against our position → exit early
4. Otherwise hold to market resolution
5. When market resolves → record outcome

---

## Market Parsing Examples

The parser handles a variety of Kalshi market title formats:

```
"Will NYC high temperature exceed 80°F on April 10?"
→ city=NYC, weather_type=temperature, threshold=80, direction=above, date=2026-04-10

"Will it rain more than 0.5 inches in Chicago on April 12?"
→ city=Chicago, weather_type=precipitation, threshold=0.5, direction=above, date=2026-04-12

"Will Boston receive more than 3 inches of snow on April 15?"
→ city=Boston, weather_type=snow, threshold=3, direction=above, date=2026-04-15

"Will Miami high temp be below 70°F on April 8?"
→ city=Miami, weather_type=temperature, threshold=70, direction=below, date=2026-04-08
```

If parsing fails with regex, the parser falls back to substring matching for robustness.

---

## NWS API Details

The National Weather Service API is:
- **Free** — no API key required
- **Accurate** — scientifically calibrated probabilistic forecasts
- **Public** — updated hourly
- **Rate-limited respectfully** — 1-second delay between requests, User-Agent header required

### NWS Endpoints Used

1. **GET /points/{lat},{lon}** — Resolve location to NWS grid coordinates
2. **GET /gridpoints/{gridId}/{x},{y}/forecast/hourly** — Hourly temperature, precip probability, conditions
3. **GET /gridpoints/{gridId}/{x},{y}** — Raw probabilistic grid data (temperature ranges, snow amounts, etc.)

### Forecast Fields

- `probabilityOfPrecipitation`: 0-100%, probability of any measurable precipitation
- `temperature`: Hourly forecast temperature (°F)
- `snowfallAmount`: Probabilistic snow amount (inches)
- `windSpeed`: Hourly wind speed (mph)

Caching is aggressive (1-hour TTL) to minimize API calls. NWS updates every hour, so this TTL is safe.

---

## Trade Execution Logic

### Entry

A trade enters when:
1. NWS probability - Kalshi ask price > MIN_EDGE
2. Market resolves 0.5-7 days from now
3. Bid-ask spread < MAX_BID_ASK_SPREAD
4. Current open positions < MAX_SIMULTANEOUS_POSITIONS
5. Fresh NWS re-fetch still shows edge > MIN_EDGE

Position size scales with edge strength:
```python
edge_factor = min(1.0, (edge - MIN_EDGE) / 0.20)  # Scale up to 0.30 edge
position_size = MIN + (MAX - MIN) * edge_factor
```

### Exit

A position exits when:
1. **Resolution**: Market resolves → record outcome
2. **NWS Reversal**: NWS probability shifts >15 cents against position → early exit
3. **Manual**: Operator intervention (would extend monitor to support this)

### Why Hold to Resolution?

Weather markets are thin but extremely high-conviction once the NWS signal is priced in. Unlike equities or crypto, there's no reason to risk exit slippage — just hold to the clear outcome.

---

## Monitoring and Alerts

### SNS Alerts

Scanner sends:
```
WEATHER SCAN COMPLETE
Markets scanned: 127
Opportunities found: 4

Top opportunities:
  • NYC rain @ YES (NWS: 73%, edge: +18¢)
  • Chicago snow @ NO (NWS: 25%, edge: +15¢)
  • ...
```

Monitor sends:
```
WEATHER BETS PLACED:
  • NYC rain @ YES — Kalshi: 55¢, NWS: 73%, edge: +18¢
  • Chicago snow @ NO — Kalshi: 80¢, NWS: 25%, edge: +55¢
```

Check SNS in CloudWatch to validate:
- Edge calculations (does NWS seem reasonable?)
- Market parsing (are city names correct?)
- Position sizing (are amounts reasonable?)

---

## Testing and Validation

### 1. Unit Tests (local)

```python
import asyncio
from weather_trader import WeatherMarketParser, CITY_COORDS

parser = WeatherMarketParser(CITY_COORDS)

# Test parsing
market = {
    "title": "Will NYC high temperature exceed 80°F on April 10?",
    "ticker": "WEATHER_NYC_TEMP_80_APR10"
}
result = parser.parse_market(market)
assert result["city"] == "NYC"
assert result["weather_type"] == "temperature"
assert result["threshold"] == 80.0
```

### 2. NWS API Test (manual)

```python
import asyncio
import aiohttp
from weather_trader import fetch_nws_forecast, NWSClient

async def test_nws():
    nws = NWSClient()
    async with aiohttp.ClientSession() as session:
        forecast = await fetch_nws_forecast(
            "NYC", 40.7128, -74.0060, "2026-04-15", nws, session
        )
        print(f"Precip: {forecast.precip_probability:.0%}")
        print(f"High: {forecast.forecast_high}°F")
        print(f"Std Dev: {forecast.temp_std_dev}°F")

asyncio.run(test_nws())
```

### 3. Live Validation (3-5 days with disabled trading)

1. Set `WEATHER_TRADER_ENABLED=false`
2. Let scanner and monitor run on schedule
3. Monitor SNS output:
   - Are edges reasonable?
   - Do city names match Kalshi markets?
   - Are dates parsing correctly?
4. Manually cross-check 2-3 opportunities:
   - Go to weather.gov, search city
   - Compare NWS precip probability in SNS to what you see in web UI
   - Verify Kalshi market prices (search Kalshi order book)
5. Once confident, enable with `WEATHER_TRADER_ENABLED=true`

---

## Troubleshooting

### NWS 404 (grid point not found)

```
ERROR: NWS 404: https://api.weather.gov/points/40.7128,-74.0060
```

This means the location is outside NWS coverage or there's a lat/lon error. Check:
- Coordinates are correct (swap if needed)
- City is in continental US (NWS doesn't cover Hawaii, Alaska, US territories)
- No typos in CITY_COORDS

### Market parsing fails

```
WARNING: Could not parse weather market: "Will the weather in Boston be weird on April 10?"
```

Add a regex pattern in `market_parser.py` for the new format, or improve substring matching.

### No opportunities found

Possible causes:
1. Kalshi has no active weather markets (check web UI)
2. All markets are outside the entry window (too early or > 7 days)
3. All edges < MIN_EDGE (adjust MIN_EDGE down if confident)
4. Bid-ask spreads > MAX_BID_ASK_SPREAD

Check SNS output to see how many markets were scanned.

### Position not entering

Check in Phase A logs:
1. Is NWS re-fetch confirming edge?
2. Is Kalshi order placement working?
3. Is DynamoDB write succeeding?

Enable `DEBUG` logging in Lambda to see detailed flow.

---

## Performance and Cost

### API Costs

- **NWS**: Free
- **Kalshi**: Taker fees on execution (part of normal trading)
- **DynamoDB**: On-demand pricing (~$1-5/month for this use case)
- **Lambda**: Minimal compute cost (scanner/monitor are fast)

### Rate Limiting

- NWS: 1-second delay between requests (respects ToS)
- Kalshi: Standard REST rate limits (handled by kalshi_client)

### Caching

NWS forecasts are cached for 1 hour (NWS updates hourly):
```python
NWS_CACHE_TTL_MINUTES = 60
```

This dramatically reduces API calls if scanner finds many markets for the same city/date.

---

## Future Enhancements

1. **Wind markets**: Extend NWS client to fetch wind speed forecasts
2. **Hurricane tracking**: Kalshi has hurricane probability markets — could integrate NHC data
3. **Multi-day forecasts**: Currently uses 1-day forecasts; could extend to weekly
4. **Confidence intervals**: Use NWS confidence intervals as position size input (wider intervals = smaller positions)
5. **Outcome tracking**: Automatically record actual weather vs. forecasts post-resolution for strategy evaluation
6. **Manual exit**: Add Lambda endpoint to close positions on demand
7. **DynamoDB caching**: Move grid point cache to DynamoDB for persistence across invocations

---

## Support

For issues or questions:
1. Check DynamoDB tables for position/opportunity records
2. Review CloudWatch logs for scanner/monitor runs
3. Monitor SNS output for edge/parsing issues
4. Manually test NWS API access and Kalshi market data

---

## References

- **NWS API**: https://www.weather.gov/documentation/services-web-api
- **Kalshi**: https://www.kalshi.com/
- **Error Function Approximation**: Abramowitz and Stegun (1964)

---

**Version**: 1.0.0
**Last Updated**: April 2026
**Status**: Production-ready, start with WEATHER_TRADER_ENABLED=false
