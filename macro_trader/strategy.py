"""
Macro trader strategy configuration.

Defines thresholds, market mappings, and trading parameters for the macro sentiment
to Kalshi prediction market bridge.

NEW: This configuration centralizes strategy parameters, making it easy to A/B test
different thresholds and risk profiles.
"""

# Signal strength threshold: minimum absolute value required to consider a trade
# Rationale: Weak signals (< 0.50) reflect marginal sentiment shifts; require >±0.50 confidence
MIN_SIGNAL_STRENGTH = 0.50

# Model confidence threshold: the sentiment model's self-assessed confidence (0.0 to 1.0)
# Rationale: Only act on high-confidence macro analysis; news events with weak signal
# and low confidence should not trigger trades
MIN_CONFIDENCE = 0.65

# Kalshi economic series tickers to scan for trading opportunities
# These are the primary economic event series available on Kalshi
ECONOMIC_SERIES = [
    "KXFED",              # Fed funds rate decisions (explicit Fed rate markets)
    "FEDFUNDS",           # Alternative Fed funds series
    "KXCPI",              # Consumer Price Index inflation
    "CPI",                # Alternative CPI series
    "KXJOBS",             # Jobs report / Non-Farm Payroll (NFP)
    "NFP",                # Alternative NFP series
    "KXGDP",              # Gross Domestic Product
    "KXPCE",              # Personal Consumption Expenditures (Fed's preferred inflation gauge)
    "KXUNEMPLOYMENT",     # Unemployment rate
]

# NEW: Map sentiment signal keys to Kalshi series keywords
# Used to match generated signals to relevant markets
SIGNAL_TO_SERIES_KEYWORDS = {
    "fed_signal": ["FED", "FEDFUNDS", "FOMC", "RATES"],
    "inflation_signal": ["CPI", "INFLATION", "PCE", "KXCPI"],
    "employment_signal": ["JOBS", "NFP", "UNEMPLOYMENT", "PAYROLL"],
    "gdp_signal": ["GDP", "GROWTH"],
}

# Position sizing constraints
MAX_POSITION_PER_MARKET = 10.00  # USD amount per trade (higher than carpet_bagger—econ events resolve slowly)
MAX_SIMULTANEOUS_POSITIONS = 5   # Limit exposure across multiple markets
MAX_PCT_BANKROLL = 0.20          # Max 20% of account per position (conservative for macro)

# Exit signal: if the sentiment signal for an open position reverses past this threshold,
# exit early even if the market hasn't resolved yet
# Rationale: If sentiment flips bearish on a Fed position we're holding bullish, the edge may evaporate
SIGNAL_REVERSAL_EXIT_THRESHOLD = -0.30

# Market resolution window: only trade if resolution is within this range
# Rationale: Markets resolving too soon won't give sentiment time to play out;
# markets too far out introduce too much noise
MIN_DAYS_TO_RESOLUTION = 1
MAX_DAYS_TO_RESOLUTION = 30

# Edge calculation: minimum profitable gap between implied probability and market price
# Rationale: If signal implies 70% but market is 65%, that's a 5% edge—might not justify execution costs
MIN_EDGE = 0.08  # 8 cents minimum edge (8% difference in probability)

# Market liquidity filter: only trade if bid-ask spread is tight
# Rationale: Wide spreads indicate illiquid markets; harder to exit profitably
MAX_BID_ASK_SPREAD = 0.05  # Max spread between yes_bid and yes_ask (5 cents)

# Signal freshness: if macro signal is older than this, don't trade
# Rationale: Economic sentiment changes rapidly around data releases; stale signals are unreliable
MAX_SIGNAL_AGE_HOURS = 4

# NEW: Early exit criteria
# If we've been in a position for this long and haven't hit the target, close it
# (only applies if signal remains positive; reversed signal closes immediately)
POSITION_HOLD_MAX_DAYS = 14
