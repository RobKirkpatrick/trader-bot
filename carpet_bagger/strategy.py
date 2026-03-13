"""
Carpet Bagger strategy constants and helpers.

All probability values are in decimal (0.0–1.0).
All dollar amounts are floats.
"""

# ---------------------------------------------------------------------------
# Pre-game scout filter
# ---------------------------------------------------------------------------
PRE_GAME_MIN = 0.55   # minimum yes_ask to add to watchlist
PRE_GAME_MAX = 0.70   # maximum yes_ask to add to watchlist — only moderate pre-game favorites; 80%+ during game = in-game signal
MIN_MINS_TO_GAME  = 30    # skip markets starting within this many minutes
MAX_GAME_AGE_MINS = 90    # skip games that started more than 90 min ago (too late to catch in-game)
MAX_CLOSE_HOURS   = 36    # only track markets that resolve within 36 hours
                          # (filters out season/tournament futures)

# ---------------------------------------------------------------------------
# In-game risk controls
# ---------------------------------------------------------------------------
STOP_LOSS    = 0.35   # sell if yes_ask drops below this
MAX_POSITIONS = 10    # max simultaneous open positions
MAX_POSITION_PCT = 0.50       # max fraction of live balance per position (auto-scales with bankroll)
BUY_CUTOFF_HOUR_ET = 23      # stop watching (no new buys) after 11 PM ET — covers all evening NBA/NHL

# ---------------------------------------------------------------------------
# Tiered position sizing (fraction of available float per tier)
# ---------------------------------------------------------------------------
# Keys are (low_inclusive, high_exclusive) probability ranges.
# Values are the fraction of available_float to deploy.
TIERED_SIZING: dict[tuple[float, float], float] = {
    (0.75, 0.80): 0.25,
    (0.80, 0.85): 0.50,
    (0.85, 0.90): 0.75,
    (0.90, 1.00): 1.00,
}

# ---------------------------------------------------------------------------
# Sport-specific rules
# ---------------------------------------------------------------------------
SPORT_SERIES = [
    "KXNBAGAMES",    # NBA individual game winner (season series)
    "KXNBAGAME",     # NBA individual game winner (alternate/daily series)
    "KXNHLGAME",     # NHL individual game winner
    "KXNCAABGAME",   # NCAAB men's basketball game winner (regular season + conf tournaments)
    "KXNCAABBGAME",  # NCAAB men's basketball game winner (alternate series, March Madness)
    "KXNCAAWBGAME",  # NCAAW women's basketball game winner
    "KXMLBGAME",     # MLB individual game winner
    # Excluded: golf (KXPGA*), racing (KXNASCAR*, KXF1*) — multiple competitors, not head-to-head
]

SPORT_RULES: dict[str, dict] = {
    "KXNBAGAMES":   {"window_open": "Q2_start",  "window_close": "Q3_end",   "take_profit": 0.98},
    "KXNBAGAME":    {"window_open": "Q2_start",  "window_close": "Q3_end",   "take_profit": 0.98},
    "KXNHLGAME":    {"window_open": "P1_end",    "window_close": "P3_start", "take_profit": 0.98},
    "KXNCAABGAME":  {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.98},
    "KXNCAABBGAME": {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.98},
    "KXNCAAWBGAME": {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.98},
    "KXMLBGAME":    {"window_open": "inning_3",  "window_close": "inning_7", "take_profit": 0.92},
}


def get_tier_fraction(prob: float) -> float:
    """
    Return the position-size fraction (0.0–1.0) for a given probability.
    Returns 0.0 if the probability doesn't meet any tier threshold.
    """
    for (low, high), fraction in TIERED_SIZING.items():
        if low <= prob < high:
            return fraction
    return 0.0


def get_take_profit(sport: str) -> float:
    """Return the take-profit threshold for a given sport series ticker."""
    return SPORT_RULES.get(sport, {}).get("take_profit", 0.92)


def dollars_to_cents(dollars: float) -> int:
    """Convert a dollar amount to Kalshi cents (integer)."""
    return int(round(dollars * 100))


def cents_to_dollars(cents: int) -> float:
    """Convert Kalshi cents to dollars."""
    return cents / 100.0
