"""
bracket_buster/strategy.py

Configuration constants and strategy parameters for tournament tree arbitrage detection
and execution on Kalshi prediction markets.

The bracket_buster strategy exploits probability hierarchy violations in tournament markets
where multiple rounds of the same tournament are simultaneously trading (e.g., NCAA March Madness).

Mathematical foundation:
    P(championship) ≤ P(final_four) ≤ P(elite_eight) ≤ P(sweet_sixteen) ≤ P(game_win)

Pure arbitrage: when this inequality is violated (e.g., championship YES at 60% but game win YES at 55%)
Soft arbitrage: when one market is statistically mispriced vs historical correlation
"""

from typing import Dict

# ============================================================================
# TOURNAMENT SERIES CONFIGURATION
# ============================================================================

# Kalshi series codes and their human-readable descriptions
# Update dynamically by scanning for series with tournament-related keywords
TOURNAMENT_SERIES: Dict[str, str] = {
    # NCAA Men's Basketball (March Madness)
    "KXNCAAMBCHAMP": "NCAA Men's Basketball Championship",
    "KXNCAAMBF4": "NCAA Men's Final Four",
    "KXNCAAMBE8": "NCAA Men's Elite Eight",
    "KXNCAAMBS16": "NCAA Men's Sweet Sixteen",
    "KXNCAAMBGAME": "NCAA Men's Game",
    # NCAA Women's Basketball
    "KXNCAAWBCHAMP": "NCAA Women's Basketball Championship",
    "KXNCAAWBF4": "NCAA Women's Final Four",
    "KXNCAAWBE8": "NCAA Women's Elite Eight",
    "KXNCAAWBS16": "NCAA Women's Sweet Sixteen",
    "KXNCAAWBGAME": "NCAA Women's Game",
    # Future: Add NFL playoffs, March Madness, other sports
}

# ============================================================================
# TOURNAMENT TIER HIERARCHY
# ============================================================================

# Probability tier ordering: lower index = more restrictive event
# Higher tier markets (game wins) should always price higher than lower tier (championships)
# If violated → arbitrage opportunity
TOURNAMENT_TIER: Dict[str, int] = {
    "championship": 0,      # Must win entire tournament
    "final_four": 1,        # Must reach Final Four
    "elite_eight": 2,       # Must reach Elite Eight
    "sweet_sixteen": 3,     # Must reach Sweet Sixteen
    "game": 4,              # Must win next game
}

# Tier keywords for market title/series classification
TIER_KEYWORDS: Dict[str, list[str]] = {
    "championship": ["champ", "championship", "winner"],
    "final_four": ["final four", "final 4", "f4"],
    "elite_eight": ["elite eight", "e8"],
    "sweet_sixteen": ["sweet sixteen", "sweet 16", "s16"],
    "game": ["game", "winner", "beats", "vs"],
}

# ============================================================================
# PURE ARBITRAGE PARAMETERS
# ============================================================================

# Minimum guaranteed spread to enter a pure arb position
# Pure arb = buy lower tier at price X, buy NO (higher tier) at price Y
#   where guaranteed profit = 1 - X - Y ≥ this threshold
# E.g., PURE_ARB_MIN_SPREAD = 0.02 means need 2 cents guaranteed profit minimum
PURE_ARB_MIN_SPREAD: float = 0.02

# Do not enter pure arbs if implied probability spread between tiers exceeds this
# (spreads > threshold suggest model mismatch or one market is broken/illiquid)
PURE_ARB_MAX_IMPLIED_SPREAD: float = 0.15

# ============================================================================
# SOFT ARBITRAGE / CONVERGENCE PARAMETERS
# ============================================================================

# Minimum mispricing percentage to enter soft arb position
# Soft arb = take underpriced single leg based on historical correlation
# E.g., if championship prices in at 35% but historical teams with 55% game-win odds
# only reach championship 15% of the time, then championship is overpriced at 35%
SOFT_ARB_MIN_MISPRICING: float = 0.08  # 8% probability gap vs expected

# Exit soft arb positions when price moves this many cents in our favor
# (convergence play: price should move toward fair value)
CONVERGENCE_PROFIT_TARGET: float = 0.06  # 6 cents profit

# For soft arbs, sell if price drops below this stop loss level
# (only for single-leg soft arbs; pure arbs have no stop loss)
SOFT_ARB_STOP_LOSS: float = 0.40  # Exit if price goes to 40%

# ============================================================================
# POSITION SIZING & RISK MANAGEMENT
# ============================================================================

# Maximum position size per individual market (USD)
# Pure arbs can be larger since downside is zero; soft arbs have directional risk
MAX_POSITION_PER_MARKET: float = 5.00

# Maximum number of simultaneously open bracket_buster positions
MAX_SIMULTANEOUS_POSITIONS: int = 10

# Maximum percentage of available bankroll to risk on a single position
MAX_PCT_BANKROLL_PER_POSITION: float = 0.25  # 25% per position max

# For multi-leg positions, enforce capital efficiency
# (pure arbs use less capital than single-leg soft arbs for same dollar exposure)
MIN_EXPECTED_RETURN_PCT: float = 0.05  # 5% minimum expected return

# ============================================================================
# EXECUTION PARAMETERS
# ============================================================================

# Order execution mode: "market" for immediate fills, "limit" for price improvement
ORDER_MODE: str = "limit"

# Limit order placement offset from current mid for limit orders (cents above/below)
LIMIT_ORDER_OFFSET: float = 0.01  # 1 cent improvement target

# Time-in-force for limit orders (minutes)
LIMIT_ORDER_TIME_IN_FORCE_MINUTES: int = 5

# For soft arbs, aggressiveness of limit order pricing
# Higher = more likely to fill, less likely to get best price
SOFT_ARB_LIMIT_AGGRESSIVENESS: float = 0.5  # 0.5 cents from mid

# ============================================================================
# MONITORING & RECONCILIATION
# ============================================================================

# Scout runs every N minutes to detect new opportunities
SCOUT_INTERVAL_MINUTES: int = 60  # 1 hour (could go to 8am ET schedule like carpet_bagger)

# Monitor runs every N minutes to check open positions
MONITOR_INTERVAL_MINUTES: int = 5

# Time window to hold soft arb positions before forced exit (hours)
# Soft arbs should close quickly; if not converging, liquidate
MAX_HOLD_TIME_SOFT_ARB_HOURS: int = 24

# Maximum hold time for pure arbs (should close immediately on market settlement)
MAX_HOLD_TIME_PURE_ARB_HOURS: int = 72

# ============================================================================
# MARKET DATA FILTERING
# ============================================================================

# Minimum volume/liquidity thresholds
MIN_ORDER_BOOK_DEPTH: float = 10.00  # Need at least $10 on both sides

# Only consider markets with at least this many contracts traded in last N days
MIN_VOLUME_CONTRACTS_7D: int = 50

# Skip markets with bid-ask spread > this percentage (illiquid)
MAX_BID_ASK_SPREAD_PCT: float = 0.05  # 5%

# ============================================================================
# LOGGING & ALERTS
# ============================================================================

# Alert thresholds
ALERT_MIN_GUARANTEED_PROFIT: float = 0.05  # Alert if pure arb profit > 5 cents

# Include market details in SNS alerts
INCLUDE_MARKET_DETAILS_IN_ALERTS: bool = True

# Log all position actions to DynamoDB
ENABLE_AUDIT_LOGGING: bool = True
