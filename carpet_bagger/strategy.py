"""
Carpet Bagger strategy constants and helpers.

All probability values are in decimal (0.0–1.0).
All dollar amounts are floats.
"""

# ---------------------------------------------------------------------------
# Pre-game scout filter
# ---------------------------------------------------------------------------
PRE_GAME_MIN = 0.55   # minimum yes_ask to add to watchlist (and to buy)
PRE_GAME_MAX = 0.80   # maximum yes_ask to add to watchlist — widen to catch strong in-game favorites
MIN_MINS_TO_GAME  = 30    # skip markets starting within this many minutes
MAX_GAME_AGE_MINS = 90    # skip games that started more than 90 min ago (too late to catch in-game)
MAX_CLOSE_HOURS   = 36    # only track markets that resolve within 36 hours
                          # (filters out season/tournament futures)

# ---------------------------------------------------------------------------
# In-game risk controls
# ---------------------------------------------------------------------------
STOP_LOSS    = 0.45   # sell if yes_ask drops below $0.45 — below 50% means the market flipped, get out
TAKE_PROFIT  = 0.82   # resting sell limit — momentum scalp exit, recycles capital for next game
MAX_POSITIONS = 15    # max simultaneous open positions — raised for March Madness volume (32 games/day)
MAX_POSITION_PCT     = 0.50  # max fraction of live balance per position (auto-scales with bankroll)
MAX_POSITION_DOLLARS = 1.00  # hard dollar cap per position — $1 max to avoid overnight capital lockup
BUY_CUTOFF_HOUR_ET = 23      # stop watching (no new buys) after 11 PM ET — covers all evening NBA/NHL
PRE_GAME_BUY_FRACTION = 0.15 # fraction of available float to stake pre-game (legacy — superseded by MAX_POSITION_DOLLARS)

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
    "KXNCAAMBGAME",  # NCAA Men's Basketball game winner (March Madness, conf tournaments)
    # "KXNCAABGAME" had 0 markets (outdated/dead series) — replaced by KXNCAAMBGAME
    # "KXNCAABBGAME" is NCAA BASEBALL — blocked below
    "KXNCAAWBGAME",  # NCAAW women's basketball game winner
    # "KXMLBGAME",   # MLB — blocked (games too long, capital lockup)
    # "KXMLBSTGAME", # MLB Spring Training — blocked
    # Excluded: golf (KXPGA*), racing (KXNASCAR*, KXF1*) — multiple competitors, not head-to-head
]

# Series blocked from dynamic discovery — will never be added even if Kalshi lists them
BLOCKED_SPORT_SERIES: set[str] = {
    "KXNCAABBGAME",   # NCAA BASEBALL (not basketball!) — blocked permanently
    "KXNCAABASEGAME", # NCAA Baseball alternate series — blocked permanently
    "KXMLBGAME",      # MLB — blocked (3hr games, capital lockup not worth it)
    "KXMLBSTGAME",    # MLB Spring Training — blocked
}

SPORT_RULES: dict[str, dict] = {
    # NBA/NHL: longer games with more comeback risk — exit at 0.85 to capture momentum without overstaying
    "KXNBAGAMES":   {"window_open": "Q2_start",  "window_close": "Q3_end",   "take_profit": 0.85},
    "KXNBAGAME":    {"window_open": "Q2_start",  "window_close": "Q3_end",   "take_profit": 0.85},
    "KXNHLGAME":    {"window_open": "P1_end",    "window_close": "P3_start", "take_profit": 0.85},
    # NCAAB: 40-min game, momentum moves fast — scalp at 0.82 and recycle capital for next MM game
    "KXNCAAMBGAME": {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.82},
    "KXNCAABGAME":  {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.82},  # legacy/dead series
    "KXNCAAWBGAME": {"window_open": "H1_10min",  "window_close": "H2_start", "take_profit": 0.82},
    "KXMLBGAME":    {"window_open": "inning_3",  "window_close": "inning_7", "take_profit": 0.75},
    "KXMLBSTGAME":  {"window_open": "inning_3",  "window_close": "inning_7", "take_profit": 0.75},
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
    return SPORT_RULES.get(sport, {}).get("take_profit", 0.85)


def dollars_to_cents(dollars: float) -> int:
    """Convert a dollar amount to Kalshi cents (integer)."""
    return int(round(dollars * 100))


def cents_to_dollars(cents: int) -> float:
    """Convert Kalshi cents to dollars."""
    return cents / 100.0
