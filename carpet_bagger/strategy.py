"""
Carpet Bagger strategy constants and helpers.

All probability values are in decimal (0.0–1.0).
All dollar amounts are floats.
"""

import os
from typing import Dict

# ---------------------------------------------------------------------------
# Pre-game scout filter
# ---------------------------------------------------------------------------
PRE_GAME_MIN = 0.62   # minimum yes_ask — raised to 0.62 for MLB focus (60% is coin-flip territory in baseball)
PRE_GAME_MAX = 0.72   # maximum yes_ask — 0.72–0.80 is a dead zone, 0% win rate historically
MIN_MINS_TO_GAME  = 30    # skip markets starting within this many minutes
MAX_GAME_AGE_MINS = 90    # skip games that started more than 90 min ago (too late to catch in-game)
MAX_CLOSE_HOURS   = 36    # only track markets that resolve within 36 hours
                          # (filters out season/tournament futures)

# Entry window aliases (used in Phase B helpers and new monitor logic)
ENTRY_MIN_PROB = PRE_GAME_MIN
ENTRY_MAX_PROB = PRE_GAME_MAX

# ---------------------------------------------------------------------------
# In-game risk controls
# ---------------------------------------------------------------------------
STOP_LOSS    = 0.45   # sell if yes_ask drops below $0.45 — below 50% means the market flipped, get out
TAKE_PROFIT  = 0.82   # resting sell limit — momentum scalp exit, recycles capital for next game
MAX_POSITIONS = 15    # max simultaneous open positions — raised for March Madness volume (32 games/day)
MAX_POSITION_PCT     = 0.50  # max fraction of live balance per position (auto-scales with bankroll)
MAX_POSITION_DOLLARS = float(os.getenv("CARPET_BAGGER_MAX_POSITION", "1.00"))  # hard dollar cap per position
BUY_CUTOFF_HOUR_ET = 23      # stop watching (no new buys) after 11 PM ET — covers all evening NBA/NHL
PRE_GAME_BUY_FRACTION = 0.15 # fraction of available float to stake pre-game (legacy — superseded by MAX_POSITION_DOLLARS)

# Aliases matching new strategy naming convention
MAX_POSITION_PER_GAME = MAX_POSITION_DOLLARS
MAX_SIMULTANEOUS_POSITIONS = MAX_POSITIONS
MAX_BANKROLL_PERCENT_PER_POSITION = MAX_POSITION_PCT

# ---------------------------------------------------------------------------
# NEW: Trailing stop logic
# ---------------------------------------------------------------------------
# Trailing stop activates once peak_prob reaches this threshold
TRAILING_STOP_ACTIVATION = 0.80   # kick in at 80% probability

# If peak_prob drops by this amount from peak, trigger exit
TRAILING_STOP_DROP = 0.06         # exit if drops 6 cents from peak

# ---------------------------------------------------------------------------
# NEW: Game-time aware exits
# ---------------------------------------------------------------------------
# When price is very high, assume game is decided; exit early instead of waiting
LATE_GAME_HIGH_PROB_THRESHOLD = 0.90   # price this high = game essentially over

# Accept this lower exit threshold instead of full take-profit target
LATE_GAME_EXIT_PROB = 0.87             # exit at 87% instead of waiting for full target

# ---------------------------------------------------------------------------
# High-confidence entry threshold (kept for sport rules reference only)
# Multiplier removed — diagnostic shows large positions have 0% win rate
# ---------------------------------------------------------------------------
HIGH_CONFIDENCE_ENTRY = 0.70

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
# Alias for new naming convention
TIERED_POSITION_SIZING = TIERED_SIZING

# ---------------------------------------------------------------------------
# Sport-specific rules
# ---------------------------------------------------------------------------
SPORT_SERIES = [
    "KXMLBGAME",     # MLB individual game winner — primary active strategy
    # "KXNBAGAMES",  # NBA — blocked (playoffs too contested, comebacks too frequent)
    # "KXNBAGAME",   # NBA — blocked
    # "KXNHLGAME",   # NHL — blocked (playoffs too contested)
    # "KXNCAAMBGAME" — NCAAB season over
    # "KXNCAAWBGAME" — NCAAW season over
    # Excluded: golf (KXPGA*), racing (KXNASCAR*, KXF1*) — multiple competitors, not head-to-head
]

# Series blocked from dynamic discovery — will never be added even if Kalshi lists them
BLOCKED_SPORT_SERIES: set[str] = {
    "KXNBAGAMES",     # NBA — blocked (playoff momentum strategy unprofitable)
    "KXNBAGAME",      # NBA — blocked
    "KXNHLGAME",      # NHL — blocked (playoff momentum strategy unprofitable)
    "KXNCAABBGAME",   # NCAA BASEBALL (not basketball!) — blocked permanently
    "KXNCAABASEGAME", # NCAA Baseball alternate series — blocked permanently
    "KXMLBSTGAME",    # MLB Spring Training — blocked (not real season stakes)
}

# Extended with trailing-stop and late-game fields for Phase B exit logic.
# Keys: window_open/window_close (Phase A timing), take_profit (resting sell),
#       trailing_stop_activation, trailing_stop_drop, late_game_threshold (Phase B exits)
SPORT_RULES: dict[str, dict] = {
    # NBA/NHL: longer games with more comeback risk — wider trailing stop
    "KXNBAGAMES": {
        "window_open": "Q2_start", "window_close": "Q3_end",
        "take_profit": 0.85, "stop_loss": 0.45,
        "trailing_stop_activation": 0.82, "trailing_stop_drop": 0.07,
        "high_confidence_entry": 0.72, "late_game_threshold": 0.92,
    },
    "KXNBAGAME": {
        "window_open": "Q2_start", "window_close": "Q3_end",
        "take_profit": 0.85, "stop_loss": 0.45,
        "trailing_stop_activation": 0.82, "trailing_stop_drop": 0.07,
        "high_confidence_entry": 0.72, "late_game_threshold": 0.92,
    },
    "KXNHLGAME": {
        "window_open": "P1_end", "window_close": "P3_start",
        "take_profit": 0.85, "stop_loss": 0.45,
        "trailing_stop_activation": 0.82, "trailing_stop_drop": 0.07,
        "high_confidence_entry": 0.72, "late_game_threshold": 0.92,
        "min_entry_prob": 0.72,  # NHL tighter entry: 50% win rate vs NBA 70% — raised floor from global 0.60
    },
    # NCAAB: 40-min game, momentum moves fast — tighter trailing stop
    "KXNCAAMBGAME": {
        "window_open": "H1_10min", "window_close": "H2_start",
        "take_profit": 0.82, "stop_loss": 0.45,
        "trailing_stop_activation": 0.78, "trailing_stop_drop": 0.05,
        "high_confidence_entry": 0.70, "late_game_threshold": 0.90,
    },
    "KXNCAABGAME": {  # legacy/dead series
        "window_open": "H1_10min", "window_close": "H2_start",
        "take_profit": 0.82, "stop_loss": 0.45,
        "trailing_stop_activation": 0.78, "trailing_stop_drop": 0.05,
        "high_confidence_entry": 0.70, "late_game_threshold": 0.90,
    },
    "KXNCAAWBGAME": {
        "window_open": "H1_10min", "window_close": "H2_start",
        "take_profit": 0.82, "stop_loss": 0.45,
        "trailing_stop_activation": 0.78, "trailing_stop_drop": 0.05,
        "high_confidence_entry": 0.70, "late_game_threshold": 0.90,
    },
    # MLB: leads can flip fast (home run, bullpen implosion) — conservative targets,
    # tighter floor (62% = team has a meaningful lead), early trailing stop.
    # Don't enter after 9pm ET to avoid west coast late games locking capital overnight.
    "KXMLBGAME": {
        "window_open": "inning_2", "window_close": "inning_7",
        "take_profit": 0.78, "stop_loss": 0.47,
        "trailing_stop_activation": 0.76, "trailing_stop_drop": 0.05,
        "high_confidence_entry": 0.70, "late_game_threshold": 0.92,
        "min_entry_prob": 0.62,  # 60% MLB favorite is essentially a coin flip — require meaningful edge
        "max_entry_hour_et": 21, # no new entries after 9pm ET (late west coast games lock capital overnight)
    },
    "KXMLBSTGAME": {
        "window_open": "inning_2", "window_close": "inning_7",
        "take_profit": 0.75, "stop_loss": 0.47,
        "trailing_stop_activation": 0.74, "trailing_stop_drop": 0.05,
        "high_confidence_entry": 0.70, "late_game_threshold": 0.90,
        "min_entry_prob": 0.62,
        "max_entry_hour_et": 21,
    },
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def get_sport_rule(sport: str, rule_name: str, default: float) -> float:
    """
    Retrieve a sport-specific rule, with fallback to global default.

    Args:
        sport: Market ticker sport code (e.g., 'KXNBAGAME')
        rule_name: Rule key (e.g., 'take_profit', 'trailing_stop_drop')
        default: Value to return if rule not found
    """
    if sport in SPORT_RULES and rule_name in SPORT_RULES[sport]:
        return SPORT_RULES[sport][rule_name]
    return default


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
    default = 0.82 if ("NCAAB" in sport or "NCAAW" in sport) else 0.85
    return get_sport_rule(sport, "take_profit", default)


def get_trailing_stop_activation(sport: str) -> float:
    """Get trailing stop activation threshold for a sport."""
    return get_sport_rule(sport, "trailing_stop_activation", TRAILING_STOP_ACTIVATION)


def get_trailing_stop_drop(sport: str) -> float:
    """Get trailing stop drop amount for a sport."""
    return get_sport_rule(sport, "trailing_stop_drop", TRAILING_STOP_DROP)


def get_high_confidence_entry(sport: str) -> float:
    """Get high-confidence entry threshold for a sport."""
    return get_sport_rule(sport, "high_confidence_entry", HIGH_CONFIDENCE_ENTRY)


def get_late_game_threshold(sport: str) -> float:
    """Get late-game high-probability threshold for a sport."""
    return get_sport_rule(sport, "late_game_threshold", LATE_GAME_HIGH_PROB_THRESHOLD)


def get_late_game_exit_prob(sport: str) -> float:
    """Get late-game exit probability for a sport."""
    return LATE_GAME_EXIT_PROB


def calculate_tiered_position_size(current_prob: float) -> float:
    """
    Calculate position size as a fraction of available float based on current probability.
    """
    for (min_p, max_p), fraction in TIERED_SIZING.items():
        if min_p < current_prob <= max_p:
            return fraction
    return 0.25 if current_prob <= 0.75 else 1.00


def get_min_entry_prob(sport: str) -> float:
    """Return the minimum entry probability for a sport (defaults to PRE_GAME_MIN)."""
    return get_sport_rule(sport, "min_entry_prob", PRE_GAME_MIN)


def get_max_entry_hour_et(sport: str) -> int:
    """
    Return the latest ET hour at which new entries are allowed for a sport.
    MLB defaults to 21 (9pm ET) to avoid west coast late games locking capital overnight.
    All other sports default to BUY_CUTOFF_HOUR_ET (23).
    """
    return int(get_sport_rule(sport, "max_entry_hour_et", BUY_CUTOFF_HOUR_ET))


def dollars_to_cents(dollars: float) -> int:
    """Convert a dollar amount to Kalshi cents (integer)."""
    return int(round(dollars * 100))


def cents_to_dollars(cents: int) -> float:
    """Convert Kalshi cents to dollars."""
    return cents / 100.0
