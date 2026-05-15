# NEW: Political Trader Package
"""
Claude-powered political sentiment trading for Kalshi prediction markets.

Core Components:
- strategy.py: Configuration and signal thresholds
- models.py: Data classes for positions, signals, opportunities
- signal_reader.py: News sentiment, polling momentum, market momentum
- scanner.py: Market discovery and opportunity identification (6h cycle)
- monitor.py: Position execution, monitoring, and exits (8h cycle)

Integration:
This module is designed to integrate with an existing AWS Lambda bot
that shares infrastructure (KalshiClient, DynamoDB, SNS, Secrets Manager).

Usage:
  from political_trader.scanner import handler as scanner_handler
  from political_trader.monitor import handler as monitor_handler

  # Schedule via EventBridge:
  # - scanner_handler: every 6 hours
  # - monitor_handler: every 8 hours
"""

from .strategy import (
    StrategyParams,
    POLITICAL_SERIES,
    POLITICAL_KEYWORDS,
    MIN_COMBINED_SIGNAL,
    MIN_EDGE,
    MAX_DAYS_TO_RESOLUTION,
    MIN_DAYS_TO_RESOLUTION,
    SIGNAL_WEIGHTS,
)

from .models import PoliticalPosition, PoliticalSignal, PoliticalOpportunity, WeeklyPoliticalDigest

from .signal_reader import PoliticalSignalReader

from .scanner import PoliticalMarketScanner, handler as scanner_handler

from .monitor import PoliticalMonitor, handler as monitor_handler

__version__ = "1.0.0"
__author__ = "Sentinel Trading"

__all__ = [
    # Strategy
    "StrategyParams",
    "POLITICAL_SERIES",
    "POLITICAL_KEYWORDS",
    "MIN_COMBINED_SIGNAL",
    "MIN_EDGE",
    "MAX_DAYS_TO_RESOLUTION",
    "MIN_DAYS_TO_RESOLUTION",
    "SIGNAL_WEIGHTS",
    # Models
    "PoliticalPosition",
    "PoliticalSignal",
    "PoliticalOpportunity",
    "WeeklyPoliticalDigest",
    # Readers
    "PoliticalSignalReader",
    # Runners
    "PoliticalMarketScanner",
    "PoliticalMonitor",
    # Handlers
    "scanner_handler",
    "monitor_handler",
]
