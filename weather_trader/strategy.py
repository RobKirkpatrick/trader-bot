# NEW: Strategy configuration for NWS-edge weather trading

"""
Weather market trading strategy constants and configuration.

Core edge: National Weather Service publishes calibrated probabilistic forecasts for free.
Kalshi weather markets are priced by retail traders without systematic NWS data consumption.
When NWS says 75% chance of rain and Kalshi prices it at 55%, that's a +20 cent edge.

Key insight: NWS forecasts are scientifically validated. Market prices are retail opinion.
This gap is one of the cleanest signal-to-market-price inefficiencies in the entire bot.
"""

from typing import Dict, Tuple

# Cities covered by NWS API with proven market liquidity on Kalshi
# Coordinates in (latitude, longitude) format
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "NYC": (40.7128, -74.0060),
    "LA": (34.0522, -118.2437),
    "Chicago": (41.8781, -87.6298),
    "Houston": (29.7604, -95.3698),
    "Phoenix": (33.4484, -112.0740),
    "Philadelphia": (39.9526, -75.1652),
    "San Antonio": (29.4241, -98.4936),
    "Dallas": (32.7767, -96.7970),
    "Austin": (30.2672, -97.7431),
    "Miami": (25.7617, -80.1918),
    "Atlanta": (33.7490, -84.3880),
    "Boston": (42.3601, -71.0589),
    "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903),
    "Las Vegas": (36.1699, -115.1398),
}

# Position sizing and edge requirements
MIN_EDGE: float = 0.10  # 10 cents minimum edge; weather markets are thin but clear
MAX_BID_ASK_SPREAD: float = 0.10  # Skip if market spread > 10 cents
MIN_POSITION_SIZE: float = 1.00  # Minimum viable order size
MAX_POSITION_PER_MARKET: float = 20.00  # Weather markets thin but directional
MAX_SIMULTANEOUS_POSITIONS: int = 8  # Diversify across cities/dates
MAX_PCT_BANKROLL: float = 0.15  # Max 15% of bankroll per market

# Timing constraints
MIN_DAYS_TO_RESOLUTION: float = 0.5  # Can enter same-day if early enough
MAX_DAYS_TO_RESOLUTION: float = 7  # NWS accuracy degrades beyond 7 days
OPTIMAL_ENTRY_WINDOW_DAYS: Tuple[float, float] = (1, 5)  # Best edge: 1-5 days out

# Exit logic: NWS reversal threshold
# If NWS probability shifts >15 cents against our position, exit early
NWS_REVERSAL_THRESHOLD: float = 0.15

# NWS API configuration
NWS_BASE_URL: str = "https://api.weather.gov"
NWS_USER_AGENT: str = "trader-bot/1.0 rkirkpard@gmail.com"
NWS_CACHE_TTL_MINUTES: int = 60  # Cache forecasts for 1 hour (NWS updates hourly)
NWS_REQUEST_DELAY_SECONDS: float = 1.0  # Respectful rate limiting between city lookups

# DynamoDB tables
OPPORTUNITIES_TABLE: str = "weather-opportunities"
POSITIONS_TABLE: str = "weather-positions"
NWS_CACHE_TABLE: str = "nws-forecast-cache"

# Logging and monitoring
LOG_GROUP: str = "/aws/lambda/weather-trader"
SNS_TOPIC_ENV_VAR: str = "SENTINEL_SNS_ARN"  # Reuse existing bot SNS topic
