# NEW: Political sentiment-driven trading strategy configuration for Kalshi markets
"""
Strategy parameters for political prediction market trading.

Core edge: Kalshi political markets are dominated by retail opinion traders.
A news-signal-driven approach that systematically scores recent political
headlines via Claude has structural edge over gut-feel retail pricing.

Key differences from Macro Trader:
- Hold times: days to weeks (not hours)
- Multiple simultaneous markets per political theme
- Resolution dates: fixed (election days, vote dates)
- Signal sources: news sentiment + polling momentum + market momentum
"""

from dataclasses import dataclass
from typing import Dict

# ============================================================================
# MARKET CONFIGURATION
# ============================================================================

POLITICAL_SERIES = [
    "KXPRES",          # US Presidential race
    "KXSENATE",        # Senate control / individual seats
    "KXHOUSE",         # House control / individual seats
    "KXGOV",           # Governor races
    "KXLEGIS",         # Legislation passing
    "KXSCOTUS",        # Supreme Court decisions
    "KXINTL",          # International elections
]

POLITICAL_KEYWORDS = [
    "election",
    "senate",
    "house",
    "president",
    "governor",
    "vote",
    "legislation",
    "bill",
    "passes",
    "wins",
    "majority",
    "primary",
    "candidat",
    "party",
    "congress",
]

# ============================================================================
# SIGNAL THRESHOLDS
# ============================================================================

MIN_NEWS_SIGNAL = 0.45  # Claude political news score magnitude
MIN_POLLING_MOMENTUM = 0.03  # 3-point polling swing in 7 days
MIN_COMBINED_SIGNAL = 0.50  # Weighted combo of news + polling + market momentum
MIN_EDGE = 0.07  # 7 cents vs Kalshi price (political mkts are thinner than sports)

# ============================================================================
# POSITION SIZING
# ============================================================================

MAX_POSITION_PER_MARKET = 15.00  # USD per position
MAX_SIMULTANEOUS_POSITIONS = 6  # Total open political positions
MAX_PCT_BANKROLL = 0.15  # Max 15% of account in political positions
MAX_DAYS_TO_RESOLUTION = 90  # Don't enter if resolves >90 days out (uncertainty)
MIN_DAYS_TO_RESOLUTION = 2  # Don't enter if resolves in <2 days (thin liquidity)

# ============================================================================
# EXIT RULES
# ============================================================================

SIGNAL_REVERSAL_THRESHOLD = -0.35  # Exit if combined signal flips hard against us
TRAILING_EDGE_EXIT = 0.03  # Exit early if edge compresses to <3 cents

# ============================================================================
# SIGNAL WEIGHTING
# ============================================================================

SIGNAL_WEIGHTS: Dict[str, float] = {
    "news_sentiment": 0.50,   # Dominant signal: recent political news momentum
    "polling_momentum": 0.35,  # Secondary: polling trend (when available)
    "market_momentum": 0.15,   # Tertiary: Kalshi price movement (smart money signal)
}

# ============================================================================
# LIQUIDITY & EXECUTION
# ============================================================================

MAX_BID_ASK_SPREAD = 0.08  # Political markets can be thin; 8 cents max acceptable spread
MIN_MARKET_VOLUME = 20  # Minimum lifetime contracts traded
EXECUTION_TIMEOUT_SECONDS = 30  # Political mkts slower than sports; allow longer wait

# ============================================================================
# POLLING API CONFIGURATION
# ============================================================================

FIVETHIRTYEIGHT_BASE_URL = "https://projects.fivethirtyeight.com/polls/"
REALCLEARPOLITICS_BASE_URL = "https://api.realclearpolitics.com/json/"
POLLING_LOOKBACK_DAYS = 7  # Calculate momentum over 7-day window

# ============================================================================
# MONITORING & ALERTS
# ============================================================================

MONITOR_FREQUENCY_HOURS = 8  # Check positions every 8 hours (not 4h like sports)
SCANNER_FREQUENCY_HOURS = 6  # Scan for new opportunities every 6 hours
WEEKLY_DIGEST_DAY_HOUR = ("Sunday", 20)  # Weekly P&L digest Sunday 8pm ET

# ============================================================================
# MARKET MOMENTUM THRESHOLDS
# ============================================================================

MARKET_MOMENTUM_TRIGGER = 0.05  # 5-cent move in 24h = noteworthy signal
MARKET_MOMENTUM_WINDOW_HOURS = 24  # Look back 24 hours for price movement


@dataclass
class StrategyParams:
    """Type-safe strategy parameter container."""
    min_news_signal: float = MIN_NEWS_SIGNAL
    min_polling_momentum: float = MIN_POLLING_MOMENTUM
    min_combined_signal: float = MIN_COMBINED_SIGNAL
    min_edge: float = MIN_EDGE
    max_position_per_market: float = MAX_POSITION_PER_MARKET
    max_simultaneous_positions: int = MAX_SIMULTANEOUS_POSITIONS
    max_pct_bankroll: float = MAX_PCT_BANKROLL
    max_days_to_resolution: int = MAX_DAYS_TO_RESOLUTION
    min_days_to_resolution: int = MIN_DAYS_TO_RESOLUTION
    signal_reversal_threshold: float = SIGNAL_REVERSAL_THRESHOLD
    trailing_edge_exit: float = TRAILING_EDGE_EXIT
    signal_weights: Dict[str, float] = None

    def __post_init__(self):
        if self.signal_weights is None:
            self.signal_weights = SIGNAL_WEIGHTS.copy()
