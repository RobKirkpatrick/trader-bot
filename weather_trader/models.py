# NEW: Data models for weather positions and trading signals

"""
Data structures for weather market positions, NWS signals, and trade execution.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class WeatherPosition:
    """
    Represents an open or closed weather market position.
    Tracks NWS signals, execution, and outcomes.
    """

    # Identifiers
    position_id: str  # Unique ID for position tracking
    market_ticker: str  # Kalshi market ticker (e.g., "WEATHER_NYC_RAIN_20260410")
    market_title: str  # Human-readable Kalshi market title

    # Market and weather details
    city: str  # City name (NYC, Chicago, etc.)
    weather_type: str  # "precipitation", "temperature", "snow", "wind", "hurricane"
    threshold: float  # Numeric threshold (e.g., 80.0 for "above 80°F")
    direction: str  # "above" | "below" | "more_than" | "less_than"
    resolution_date: str  # ISO format: YYYY-MM-DD

    # NWS signal at entry time
    nws_probability: float  # NWS probability for YES outcome (0.0-1.0)
    nws_forecast_temp: Optional[float]  # Forecast temperature for temp markets
    nws_precip_prob: Optional[float]  # Precip probability for rain/snow markets
    nws_fetched_at: str  # ISO timestamp when NWS data was fetched

    # Trade execution
    direction_bet: str  # "yes" | "no" — which side we took
    entry_price: float  # Kalshi price at execution (0.0-1.0)
    edge_at_entry: float  # nws_probability - entry_price
    contracts: int  # Number of contracts
    position_size_usd: float  # Dollar amount at risk
    order_id: str  # Kalshi order ID for tracking

    # Position state
    status: str  # "open" | "closed"
    opened_at: str  # ISO timestamp
    closed_at: Optional[str]  # ISO timestamp when position resolved

    # P&L and outcome
    pnl: float = 0.0  # Profit/loss in dollars
    outcome: Optional[str] = None  # "won" | "lost" | "early_exit"
    actual_weather: Optional[str] = None  # What actually happened post-resolution
    exit_reason: Optional[str] = None  # Why we exited (resolution, nws_reversal, manual)
    last_updated: Optional[str] = None  # Last time position was updated

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WeatherPosition":
        """Construct from DynamoDB item."""
        return cls(**data)

    def is_expired_entry(self) -> bool:
        """Check if entry window has passed (optimization check)."""
        # This would use current time vs resolution_date
        # Implementation depends on DynamoDB/cache context
        return False

    def nws_signal_strength(self) -> float:
        """Return absolute confidence (0.0-0.5) in NWS direction."""
        # Confidence is distance from 50/50
        return abs(self.nws_probability - 0.5)


@dataclass
class MarketOpportunity:
    """
    Represents a potential trade found during scanning phase.
    Stored in DynamoDB before execution decision in monitor phase.
    """

    opportunity_id: str
    market_ticker: str
    market_title: str
    city: str
    weather_type: str
    threshold: float
    direction: str
    target_date: str

    # Kalshi market state
    kalshi_yes_bid: float
    kalshi_yes_ask: float
    kalshi_yes_last: float

    # NWS signal
    nws_probability: float
    nws_fetched_at: str

    # Edge calculation (larger is better)
    edge_yes: float  # nws_prob - ask price (profit if we buy YES)
    edge_no: float  # (1 - nws_prob) - no_ask (profit if we buy NO)
    recommended_side: str  # "yes" | "no"
    recommended_edge: float  # The better edge

    # Metadata
    days_to_resolution: float
    bid_ask_spread: float
    created_at: str
    expires_at: str  # When this opportunity is stale

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketOpportunity":
        """Construct from DynamoDB item."""
        return cls(**data)


@dataclass
class NWSForecast:
    """
    Represents parsed NWS forecast for a city and date.
    Cached in DynamoDB with TTL to minimize API calls.
    """

    city: str
    target_date: str  # ISO YYYY-MM-DD
    grid_id: str
    grid_x: int
    grid_y: int

    # Precipitation
    precip_probability: float  # 0.0-1.0
    hourly_precip_probs: list[float]  # Hourly breakdown for averaging

    # Temperature
    forecast_high: float  # Fahrenheit
    forecast_low: float  # Fahrenheit
    temp_std_dev: float  # Estimated from NWS confidence interval

    # Snow
    snow_probability: float  # 0.0-1.0
    snow_expected_amount: Optional[float]  # Inches

    # Wind
    wind_speed_mph: float
    wind_gust_mph: Optional[float]

    # Metadata
    fetched_at: str  # ISO timestamp
    expires_at: str  # When cache entry expires

    def to_dict(self) -> dict:
        """Convert to dictionary for DynamoDB storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NWSForecast":
        """Construct from DynamoDB item."""
        return cls(**data)
