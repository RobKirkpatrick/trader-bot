"""
bracket_buster

Tournament tree arbitrage strategy for Kalshi prediction markets.

Detects and executes pure and soft arbitrage opportunities across NCAA March Madness
and other tournament markets by exploiting probability hierarchy violations.

Core modules:
    - strategy: Configuration constants and parameters
    - models: Data models (BracketPosition, ArbitrageOpportunity)
    - analyzer: Core analysis engine (BracketAnalyzer)
    - scout: Discovery and opportunity detection (runs hourly via EventBridge)
    - monitor: Execution and position management (runs every 5 minutes)

Usage:
    # As a module
    from bracket_buster import scout, monitor

    # Configuration
    - Set BRACKET_BUSTER_ENABLED=true in environment
    - Set BRACKET_BUSTER_SNS_TOPIC_ARN for alerts
    - Update carpet_bagger.kalshi_client with NO-side order support

Example event (EventBridge → Lambda):
    {
        "source": "aws.events",
        "detail-type": "Scheduled Event"
    }
"""

__version__ = "1.0.0"
__author__ = "Bracket Buster Team"

# Public API exports
from .models import BracketPosition, ArbitrageOpportunity
from .analyzer import BracketAnalyzer
from .strategy import (
    TOURNAMENT_SERIES,
    TOURNAMENT_TIER,
    PURE_ARB_MIN_SPREAD,
    SOFT_ARB_MIN_MISPRICING,
)

__all__ = [
    "BracketPosition",
    "ArbitrageOpportunity",
    "BracketAnalyzer",
    "TOURNAMENT_SERIES",
    "TOURNAMENT_TIER",
    "PURE_ARB_MIN_SPREAD",
    "SOFT_ARB_MIN_MISPRICING",
    "scout",
    "monitor",
]


def get_version() -> str:
    """Return module version"""
    return __version__
