# Weather Trader: NWS-Edge Trading for Kalshi Weather Markets

Production-ready Python module for trading Kalshi weather prediction markets using National Weather Service (NWS) probabilistic forecasts.

## The Edge

**NWS forecasts are calibrated.** When the National Weather Service says there's a 75% chance of rain on a given day, it rains 75% of the time. These are scientifically validated probabilistic forecasts published freely via a public API.

**Kalshi weather markets are priced by retail traders** with no systematic integration of NWS data. When NWS says 75% and Kalshi prices the YES at 55 cents, that's a **+20 cent arbitrage**.

## Quick Start

### 1. Install

```bash
# Copy module to your bot
cp -r weather_trader/ /path/to/repo/

# No additional dependencies needed (uses aiohttp, already installed)
```

### 2. Configure

Add to `config/settings.py`:

```python
WEATHER_TRADER_ENABLED = os.getenv("WEATHER_TRADER_ENABLED", "false").lower() == "true"
```

Add to `.env`:

```bash
WEATHER_TRADER_ENABLED=false  # Start disabled for validation
```

### 3. Create DynamoDB Tables

```bash
aws dynamodb create-table \
  --table-name weather-opportunities \
  --attribute-definitions AttributeName=opportunity_id,AttributeType=S \
  --key-schema AttributeName=opportunity_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

aws dynamodb create-table \
  --table-name weather-positions \
  --attribute-definitions AttributeName=position_id,AttributeType=S \
  --key-schema AttributeName=position_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

### 4. Wire Lambda Handlers

```python
from weather_trader import run_scanner, run_monitor
from config.settings import WEATHER_TRADER_ENABLED

async def weather_scanner_handler(event, context):
    if not WEATHER_TRADER_ENABLED:
        return {"status": "disabled"}
    return await run_scanner(kalshi_client, dynamo, sns)

async def weather_monitor_handler(event, context):
    if not WEATHER_TRADER_ENABLED:
        return {"status": "disabled"}
    return await run_monitor(kalshi_client, dynamo, sns)
```

### 5. Create EventBridge Schedules

```bash
# Scanner (every 4 hours)
aws events put-rule --name weather-scanner --schedule-expression "rate(4 hours)"

# Monitor (every 6 hours)
aws events put-rule --name weather-monitor --schedule-expression "rate(6 hours)"
```

### 6. Validate (3-5 days)

1. Keep `WEATHER_TRADER_ENABLED=false`
2. Monitor CloudWatch logs and SNS alerts
3. Verify edge calculations and NWS data
4. Once confident, enable with `WEATHER_TRADER_ENABLED=true`

## How It Works

### Scanner (every 4 hours)

1. Search Kalshi for weather markets
2. Parse market title → extract city, weather type, threshold, resolution date
3. Fetch NWS forecast for that city/date
4. Calculate NWS probability → Kalshi market price gap (edge)
5. Store opportunities with edge > $0.10 in DynamoDB

### Monitor (every 6 hours)

**Execute Phase:**
- Fetch opportunities from DynamoDB
- Re-fetch NWS to confirm edge
- Calculate position size
- Place Kalshi limit order
- Write position to DynamoDB

**Monitor Phase:**
- Check if markets resolved
- Re-fetch NWS for open positions
- Exit early if NWS forecast shifts >15 cents against us
- Hold otherwise to resolution

## Module Structure

```
weather_trader/
├── strategy.py          # Configuration (MIN_EDGE, MAX_POS, timing, etc.)
├── models.py            # Data classes (WeatherPosition, MarketOpportunity, NWSForecast)
├── nws_client.py        # NWS API client (grid points, hourly forecasts, grid data)
├── market_parser.py     # Parse Kalshi titles → weather parameters
├── scanner.py           # Market scanner (search, parse, compute edge)
├── monitor.py           # Position executor & monitor
├── __init__.py          # Exports
├── CLAUDE_CODE_HANDOFF.md  # Integration guide
├── example_usage.py     # Usage examples
└── README.md            # This file
```

## Configuration

All strategy parameters are in `strategy.py`:

```python
MIN_EDGE = 0.10                 # 10 cents minimum edge
MAX_BID_ASK_SPREAD = 0.10       # Skip wide markets
MAX_POSITION_PER_MARKET = 20.00 # Max per market
MAX_SIMULTANEOUS_POSITIONS = 8  # Max open positions

MIN_DAYS_TO_RESOLUTION = 0.5    # Can enter same-day
MAX_DAYS_TO_RESOLUTION = 7      # NWS accuracy limit

NWS_REVERSAL_THRESHOLD = 0.15   # Exit if forecast shifts 15+ cents against position
```

Adjust based on your market liquidity, risk tolerance, and bankroll size.

## Market Parsing

The parser handles various Kalshi title formats:

```
"Will NYC high temperature exceed 80°F on April 10?"
→ city=NYC, weather_type=temperature, threshold=80, direction=above

"Will it rain more than 0.5 inches in Chicago on April 12?"
→ city=Chicago, weather_type=precipitation, threshold=0.5, direction=above

"Will Boston receive more than 3 inches of snow on April 15?"
→ city=Boston, weather_type=snow, threshold=3, direction=above
```

Falls back to substring matching if regex fails, for robustness.

## Supported Cities

```python
NYC, LA, Chicago, Houston, Phoenix, Philadelphia, San Antonio, Dallas,
Austin, Miami, Atlanta, Boston, Seattle, Denver, Las Vegas
```

Coverage limited to continental US (NWS API constraint).

## Example Usage

### Fetch NWS Forecast

```python
import asyncio
import aiohttp
from weather_trader import fetch_nws_forecast, NWSClient, CITY_COORDS
from datetime import datetime, timedelta

async def main():
    nws = NWSClient()
    async with aiohttp.ClientSession() as session:
        target_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        forecast = await fetch_nws_forecast(
            "NYC", 40.7128, -74.0060, target_date, nws, session
        )
        print(f"Precip: {forecast.precip_probability:.0%}")
        print(f"High: {forecast.forecast_high}°F")

asyncio.run(main())
```

### Parse Market Title

```python
from weather_trader import WeatherMarketParser, CITY_COORDS

parser = WeatherMarketParser(CITY_COORDS)
market = {"title": "Will NYC high temperature exceed 80°F on April 10?"}
parsed = parser.parse_market(market)
# → {"city": "NYC", "weather_type": "temperature", "threshold": 80, ...}
```

### Calculate Temperature Probability

```python
from weather_trader import NWSClient

# P(temp > 80°F) given forecast of 75°F with std dev 8°F
prob = NWSClient.compute_temp_exceedance_prob(
    forecast_temp=75.0,
    std_dev=8.0,
    threshold=80.0,
    direction="above"
)
print(f"P(temp > 80°F) = {prob:.0%}")
```

See `example_usage.py` for complete examples.

## NWS API

- **Free** — no API key required
- **Public** — `https://api.weather.gov`
- **Accurate** — calibrated probabilistic forecasts
- **Respectful rate limiting** — 1-second delay between requests

Endpoints:
- `GET /points/{lat},{lon}` — resolve grid coordinates
- `GET /gridpoints/{gridId}/{x},{y}/forecast/hourly` — hourly forecast
- `GET /gridpoints/{gridId}/{x},{y}` — probabilistic grid data

Caching: 1-hour TTL (NWS updates hourly).

## Trade Execution

### Entry Rules

- NWS probability - Kalshi ask > MIN_EDGE (10 cents)
- Market resolves 0.5-7 days from now
- Bid-ask spread < 10 cents
- Position count < 8 simultaneous
- Fresh NWS re-fetch still shows edge

Position size scales with edge:

```python
edge_factor = min(1.0, (edge - MIN_EDGE) / 0.20)
size = MIN + (MAX - MIN) * edge_factor
```

### Exit Rules

- **Resolution** — market settles → record outcome
- **NWS Reversal** — NWS prob shifts >15 cents against position → exit early
- **Manual** — operator intervention

## Monitoring

SNS alerts every 4-6 hours:

```
WEATHER SCAN COMPLETE
Markets scanned: 127
Opportunities found: 4

Top opportunities:
  • NYC rain @ YES (NWS: 73%, edge: +18¢)
  • Chicago snow @ NO (NWS: 25%, edge: +15¢)
```

Check CloudWatch logs to validate:
- Edge calculations
- Market parsing
- Position sizing

## Performance

### API Costs

- NWS: Free
- Kalshi: Standard taker fees
- DynamoDB: On-demand, ~$1-5/month
- Lambda: Minimal

### Rate Limiting

- NWS: 1-second delay between requests
- Kalshi: Standard REST limits (handled by kalshi_client)

### Efficiency

- Aggressive NWS caching (1-hour TTL)
- Parallel market scanning
- Batch position monitoring

## Limitations & Future Work

### Current

- US only (NWS coverage constraint)
- No wind markets (can extend to NWS wind API)
- No hurricane markets (could integrate NHC data)
- 1-day forecasts (can extend to weekly)

### Future Enhancements

- Wind speed forecasting
- Hurricane probability markets
- Extended forecasts (7+ days)
- Confidence-based position sizing
- Automatic outcome tracking
- Manual exit Lambda endpoint
- DynamoDB grid point caching

## Troubleshooting

### No opportunities found

1. Check if Kalshi has active weather markets (web UI)
2. Check if markets are outside entry window (should be 1-5 days out)
3. Lower MIN_EDGE in strategy.py if confident
4. Check CloudWatch logs for parsing/NWS errors

### Market parsing fails

Add regex pattern for new format in `market_parser.py`, or improve substring matching.

### NWS 404

City may be outside NWS coverage or coordinates are wrong. Check CITY_COORDS and weather.gov.

## Dependencies

- `aiohttp` — async HTTP (already installed)
- `boto3` — DynamoDB, SNS (already available)
- Python 3.9+
- No scipy (uses error function approximation instead)

## Support

See `CLAUDE_CODE_HANDOFF.md` for full integration guide.

For issues:
1. Check CloudWatch logs
2. Monitor SNS alerts
3. Validate NWS API access manually
4. Review DynamoDB tables for position records

## References

- NWS API: https://www.weather.gov/documentation/services-web-api
- Kalshi: https://www.kalshi.com/
- Error Function Approximation: Abramowitz & Stegun (1964)

---

**Status**: Production-ready
**Version**: 1.0.0
**Start with**: `WEATHER_TRADER_ENABLED=false` and validate 3-5 days before enabling
