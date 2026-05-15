# NEW: Data models for political trading positions and signals
"""
Pydantic/dataclass models for political trading state and signals.
Compatible with DynamoDB serialization via the shared infrastructure.
"""

from dataclasses import dataclass, asdict, field
from typing import Optional
from datetime import datetime


@dataclass
class PoliticalPosition:
    """
    Represents a single open or closed position in a Kalshi political market.
    Serializable to/from DynamoDB.
    """

    # Identifiers
    position_id: str  # UUID or market_ticker-timestamp
    market_ticker: str  # e.g., "USCASEN23-D"
    series: str  # e.g., "KXSENATE"
    market_title: str  # e.g., "Democrats win Senate majority — 2026"

    # Signal at entry time
    news_signal: float  # [-1.0, +1.0] Claude news sentiment
    polling_momentum: float  # [-1.0, +1.0] 7-day polling trend
    market_momentum: float  # [-1.0, +1.0] 24h Kalshi price movement
    combined_signal: float  # [-1.0, +1.0] weighted combination
    entry_summary: str  # 1-2 sentence Claude summary of the political situation

    # Trade execution
    direction: str  # "yes" or "no"
    entry_price: float  # Kalshi ask price at entry (0.00-0.99)
    contracts: int  # Number of contracts
    position_size_usd: float  # contracts * entry_price
    order_id: str  # Kalshi order ID
    fair_value_estimate: float  # implied_probability based on combined_signal

    # Lifecycle
    status: str  # "open" | "closed" | "error"
    resolution_date: str  # ISO date string, e.g., "2026-11-03"
    opened_at: str  # ISO datetime string
    closed_at: Optional[str] = None

    # P&L (populated when closed)
    pnl: float = 0.0  # Realized profit/loss in USD
    pnl_pct: float = 0.0  # P&L as % of position size
    outcome: Optional[str] = None  # "won" | "lost" | "early_exit"
    exit_reason: Optional[str] = None  # e.g., "signal_reversal", "edge_compression"
    exit_price: Optional[float] = None  # Price at exit (not available until closed)
    days_held: Optional[int] = None

    # Monitoring
    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_signal_refresh: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> dict:
        """Convert to flat dict for DynamoDB."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PoliticalPosition":
        """Reconstruct from flat dict (DynamoDB)."""
        return cls(**data)

    def is_expired(self, days_threshold: int = 90) -> bool:
        """Check if position is approaching/past resolution date."""
        from datetime import datetime as dt, timedelta

        resolution = dt.fromisoformat(self.resolution_date)
        return dt.utcnow() >= resolution - timedelta(days=1)


@dataclass
class PoliticalSignal:
    """Represents a complete signal assessment for a market."""

    market_ticker: str
    market_title: str
    series: str
    resolution_date: str

    # Component signals
    news_signal: float  # [-1.0, +1.0]
    news_confidence: float  # [0.0, 1.0] how confident in news assessment
    news_summary: str  # 1-sentence summary of news momentum

    polling_momentum: Optional[float] = None  # [-1.0, +1.0], None if no polling
    polling_summary: Optional[str] = None

    market_momentum: Optional[float] = None  # [-1.0, +1.0], None if no history
    market_momentum_direction: Optional[str] = None  # "up" | "down" | None

    # Combined assessment
    combined_signal: float = 0.0  # Weighted combo
    implied_probability: float = 0.50  # P(YES outcome)
    edge_vs_market: float = 0.0  # Fair value - market price (in cents)

    # Kalshi market state
    current_yes_price: float = 0.50
    current_no_price: float = 0.50
    bid_ask_spread: float = 0.02
    last_24h_volume: int = 0

    # Metadata
    assessed_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    recommendation: str = "HOLD"  # "BUY_YES" | "BUY_NO" | "HOLD"

    def to_dict(self) -> dict:
        """Convert to dict."""
        return asdict(self)


@dataclass
class PoliticalOpportunity:
    """
    A market that passed the scanner's signal threshold.
    Stored in DynamoDB pending manual or automated entry.
    """

    opportunity_id: str  # UUID
    market_ticker: str
    market_title: str
    series: str
    resolution_date: str

    signal: PoliticalSignal
    combined_signal: float
    edge_vs_market: float

    status: str  # "pending" | "entered" | "skipped" | "expired"
    created_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    entered_position_id: Optional[str] = None
    skip_reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict (recursive for signal)."""
        d = asdict(self)
        if isinstance(self.signal, PoliticalSignal):
            d["signal"] = self.signal.to_dict()
        return d


@dataclass
class WeeklyPoliticalDigest:
    """Summary for weekly P&L and position report."""

    week_ending: str  # ISO date
    generated_at: str

    total_open_positions: int
    total_unrealized_pnl: float
    avg_days_to_resolution: float

    positions: list = field(default_factory=list)  # List of PoliticalPosition dicts

    weekly_closed_positions: int = 0
    weekly_realized_pnl: float = 0.0
    weekly_win_rate: float = 0.0

    scan_results_6h: str = ""  # Last 6h scanner summary

    def to_dict(self) -> dict:
        """Convert to dict."""
        return asdict(self)
