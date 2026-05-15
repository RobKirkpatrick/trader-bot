"""
Basis / cash-and-carry arbitrage module for Coinbase dated quarterly futures.

Strategy Overview:
  - Long spot (BTC-USD) + short dated futures (BTC-27JUN25-CDE)
  - Delta-neutral: price moves cancel out
  - Profit = basis (futures premium over spot) locked in at entry, realized at expiry
  - Entry: annualized basis APR > 8%
  - Exit: basis compresses below 3%, OR close spot leg near expiry (futures auto-settle)

Components:
  - strategy.py: Configuration, basis APR helpers, expiry parsing
  - models.py: BasisOpportunity and BasisPosition data models
  - coinbase_client.py: Async HTTP client with JWT auth + futures discovery
  - scanner.py: Runs every 4h to scan for basis opportunities
  - monitor.py: Runs every 1h to execute + manage positions
"""

from . import strategy
from .coinbase_client import CoinbaseClient, CoinbaseAPIError, CoinbaseAuthError
from .models import BasisOpportunity, BasisPosition, FundingOpportunity, FundingPosition
from .monitor import BasisArbMonitor, FundingRateMonitor
from .scanner import BasisArbScanner, FundingRateScanner

__all__ = [
    "strategy",
    "CoinbaseClient",
    "CoinbaseAPIError",
    "CoinbaseAuthError",
    "BasisOpportunity",
    "BasisPosition",
    "FundingOpportunity",
    "FundingPosition",
    "BasisArbMonitor",
    "BasisArbScanner",
    "FundingRateMonitor",
    "FundingRateScanner",
]
