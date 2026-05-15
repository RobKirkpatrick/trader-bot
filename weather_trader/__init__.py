# NEW: Weather trader module initialization

"""
weather_trader — NWS-edge trading for Kalshi weather prediction markets

Core strategy:
- National Weather Service publishes calibrated probabilistic forecasts (free, no API key)
- Kalshi weather markets are priced by retail traders (no systematic NWS integration)
- When NWS says 75% and Kalshi prices it at 55%, that's a +20 cent edge
- NWS forecasts are scientifically validated; market prices are retail opinion

This module provides:
- NWSClient: Fetch hourly/grid forecasts from NWS API
- WeatherMarketParser: Parse Kalshi market titles to extract parameters
- WeatherMarketScanner: Search for weather markets, compute NWS edge, store opportunities
- WeatherPositionMonitor: Execute trades, monitor positions, exit on NWS reversal

Integration:
- Runs on EventBridge schedule (4h scanner, 6h monitor)
- Reuses existing carpet_bagger.kalshi_client for order placement
- Stores state in DynamoDB, sends alerts via SNS
- Compatible with Lambda (no external dependencies beyond aiohttp)

Start with WEATHER_TRADER_ENABLED=false and monitor SNS output to validate edge
calculations and NWS parsing before enabling live trading.
"""

from .strategy import (
    CITY_COORDS,
    MIN_EDGE,
    MAX_BID_ASK_SPREAD,
    MIN_POSITION_SIZE,
    MAX_POSITION_PER_MARKET,
    MAX_SIMULTANEOUS_POSITIONS,
    MAX_PCT_BANKROLL,
    MIN_DAYS_TO_RESOLUTION,
    MAX_DAYS_TO_RESOLUTION,
    OPTIMAL_ENTRY_WINDOW_DAYS,
    NWS_REVERSAL_THRESHOLD,
    NWS_USER_AGENT,
    NWS_CACHE_TTL_MINUTES,
)
from .models import WeatherPosition, MarketOpportunity, NWSForecast
from .nws_client import NWSClient, fetch_nws_forecast
from .market_parser import WeatherMarketParser
from .scanner import WeatherMarketScanner, run_scanner
from .monitor import WeatherPositionMonitor, run_monitor

__version__ = "1.0.0"
__all__ = [
    # Strategy
    "CITY_COORDS",
    "MIN_EDGE",
    "MAX_BID_ASK_SPREAD",
    "MIN_POSITION_SIZE",
    "MAX_POSITION_PER_MARKET",
    "MAX_SIMULTANEOUS_POSITIONS",
    "MAX_PCT_BANKROLL",
    "MIN_DAYS_TO_RESOLUTION",
    "MAX_DAYS_TO_RESOLUTION",
    "OPTIMAL_ENTRY_WINDOW_DAYS",
    "NWS_REVERSAL_THRESHOLD",
    "NWS_USER_AGENT",
    "NWS_CACHE_TTL_MINUTES",
    # Models
    "WeatherPosition",
    "MarketOpportunity",
    "NWSForecast",
    # NWS Client
    "NWSClient",
    "fetch_nws_forecast",
    # Parser
    "WeatherMarketParser",
    # Scanner
    "WeatherMarketScanner",
    "run_scanner",
    # Monitor
    "WeatherPositionMonitor",
    "run_monitor",
]
