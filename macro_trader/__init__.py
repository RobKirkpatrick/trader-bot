"""
Macro trader module: bridges macro sentiment pipeline to Kalshi economic prediction markets.

This module implements a complete trading system that:
1. Reads macro sentiment signals from the existing news_macro pipeline
2. Maps signals to Kalshi economic event markets (Fed, CPI, jobs, GDP)
3. Discovers trading opportunities based on signal strength and edge
4. Executes trades and manages positions to market resolution

Core components:
- strategy: Configuration and thresholds
- models: Data structures (MacroPosition, MacroOpportunity)
- signal_reader: Interface to macro sentiment signals
- scanner: Discovers opportunities by matching signals to markets
- monitor: Executes opportunities and manages positions

Key insight: The sentiment pipeline was already scoring macro events for equities.
This module reuses those signals for prediction markets where the edge is larger
due to lower market efficiency.

Usage:
    from macro_trader import scanner, monitor

    # Discover opportunities (6-hour schedule)
    await scanner.run()

    # Execute and manage positions (4-hour schedule)
    await monitor.run()
"""

from . import strategy, models, signal_reader, scanner, monitor

__all__ = [
    "strategy",
    "models",
    "signal_reader",
    "scanner",
    "monitor",
]

__version__ = "1.0.0"
