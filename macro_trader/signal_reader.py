"""
Macro signal reader: interface to the existing sentiment pipeline.

Reads macro sentiment signals from sentiment/news_macro.py via DynamoDB cache
or direct invocation. Handles signal freshness validation and provides utilities
for determining trade direction and actionability.

NEW: Decouples the scanner from how sentiment signals are obtained, allowing
flexibility in whether signals are cached or computed on-demand.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import os

import boto3

from . import strategy

logger = logging.getLogger(__name__)


class MacroSignalReader:
    """
    Reads and validates macro sentiment signals.

    Strategy:
    1. Primary: Read from DynamoDB cache table "macro-signal-cache"
       (news_macro should write here for every run)
    2. Fallback: Import and call sentiment/news_macro directly (if available)
    3. Return None and log if signal is stale (> MAX_SIGNAL_AGE_HOURS)
    """

    def __init__(self):
        """Initialize DynamoDB client and cache table name."""
        self.dynamodb = boto3.resource("dynamodb")
        self.cache_table_name = os.getenv("MACRO_SIGNAL_CACHE_TABLE", "macro-signal-cache")
        self.cache_table = self.dynamodb.Table(self.cache_table_name)
        self.max_signal_age_hours = strategy.MAX_SIGNAL_AGE_HOURS

    def get_latest_signal(self) -> Optional[Dict[str, Any]]:
        """
        Fetch the most recent macro sentiment signal.

        Returns shape:
        {
            "overall_score": float,        # -1.0 (bearish) to +1.0 (bullish)
            "fed_signal": float,
            "inflation_signal": float,
            "employment_signal": float,
            "gdp_signal": float,
            "confidence": float,           # 0.0 to 1.0
            "headline_count": int,
            "summary": str,
            "generated_at": str            # ISO-8601 timestamp
        }

        Returns None if:
        - Cache table doesn't exist or is empty
        - Signal is stale (> MAX_SIGNAL_AGE_HOURS)
        - DynamoDB query fails
        """
        try:
            # Query latest signal by date (hash key: "signal_date")
            # Most recent run is today's date string: YYYY-MM-DD
            today = datetime.utcnow().date().isoformat()
            response = self.cache_table.get_item(Key={"signal_date": today})

            if "Item" not in response:
                logger.warning(
                    f"No macro signal found in cache for date {today}. "
                    "Check that sentiment/news_macro is writing to macro-signal-cache."
                )
                return None

            signal = response["Item"]

            # Validate freshness
            if not self._is_signal_fresh(signal):
                logger.warning(
                    f"Macro signal is stale (generated {signal.get('generated_at')}). "
                    f"Max age: {self.max_signal_age_hours} hours."
                )
                return None

            logger.info(
                f"Loaded macro signal from cache: "
                f"overall_score={signal.get('overall_score'):.2f}, "
                f"confidence={signal.get('confidence'):.2f}, "
                f"generated_at={signal.get('generated_at')}"
            )
            return signal

        except Exception as e:
            logger.error(f"Failed to read macro signal from cache: {e}")
            return None

    def _is_signal_fresh(self, signal: Dict[str, Any]) -> bool:
        """Check if signal timestamp is within acceptable age window."""
        generated_at_str = signal.get("generated_at")
        if not generated_at_str:
            logger.warning("Signal missing 'generated_at' field")
            return False

        try:
            # Parse ISO-8601 timestamp (with or without 'Z' suffix)
            if generated_at_str.endswith("Z"):
                generated_at_str = generated_at_str[:-1]
            generated_at = datetime.fromisoformat(generated_at_str)
        except (ValueError, AttributeError) as e:
            logger.error(f"Failed to parse signal timestamp: {generated_at_str}, error: {e}")
            return False

        age_hours = (datetime.utcnow() - generated_at).total_seconds() / 3600
        is_fresh = age_hours <= self.max_signal_age_hours

        if not is_fresh:
            logger.warning(
                f"Signal age {age_hours:.1f} hours exceeds max {self.max_signal_age_hours} hours"
            )

        return is_fresh

    def is_signal_actionable(self, signal: Dict[str, Any], signal_key: str) -> bool:
        """
        Determine if a specific signal is strong enough to trade.

        Checks:
        1. Signal[signal_key] exists and is numeric
        2. abs(signal[signal_key]) >= MIN_SIGNAL_STRENGTH
        3. signal['confidence'] >= MIN_CONFIDENCE

        Returns True only if all conditions pass.
        """
        if not signal:
            return False

        signal_value = signal.get(signal_key)
        confidence = signal.get("confidence")

        if signal_value is None or confidence is None:
            logger.debug(
                f"Signal key '{signal_key}' or 'confidence' missing in signal dict"
            )
            return False

        try:
            signal_value = float(signal_value)
            confidence = float(confidence)
        except (TypeError, ValueError):
            logger.warning(
                f"Cannot convert signal_value={signal_value} or confidence={confidence} to float"
            )
            return False

        strength_check = abs(signal_value) >= strategy.MIN_SIGNAL_STRENGTH
        confidence_check = confidence >= strategy.MIN_CONFIDENCE

        if not strength_check:
            logger.debug(
                f"Signal '{signal_key}' strength {signal_value:.2f} "
                f"below threshold {strategy.MIN_SIGNAL_STRENGTH}"
            )
        if not confidence_check:
            logger.debug(
                f"Signal confidence {confidence:.2f} "
                f"below threshold {strategy.MIN_CONFIDENCE}"
            )

        return strength_check and confidence_check

    def get_direction(self, signal_value: float) -> str:
        """
        Convert signal score to trade direction.

        Logic:
        - signal_value > 0: bullish → buy YES on bullish outcome
        - signal_value < 0: bearish → buy NO on bullish outcome (equivalent to buy YES on bearish)

        Args:
            signal_value: Signal score (-1.0 to +1.0)

        Returns:
            "yes" if signal is positive (bullish)
            "no" if signal is negative (bearish)
        """
        return "yes" if signal_value > 0 else "no"

    def estimate_implied_probability(self, signal_value: float) -> float:
        """
        Convert signal score to an estimated market probability.

        Mapping:
        - signal_value = 0.0 → 50% (coin flip)
        - signal_value = +1.0 → 90% (near-certain YES)
        - signal_value = -1.0 → 10% (near-certain NO)

        Linear formula:
        implied_prob = 0.50 + (signal_value * 0.40)

        Examples:
        - signal=+0.75 → 0.50 + (0.75 * 0.40) = 0.80 (80% YES)
        - signal=-0.50 → 0.50 + (-0.50 * 0.40) = 0.30 (30% YES, i.e., 70% NO)

        Args:
            signal_value: Signal score (-1.0 to +1.0)

        Returns:
            Estimated probability for YES outcome (0.0 to 1.0)
        """
        return max(0.0, min(1.0, 0.50 + (signal_value * 0.40)))

    def calculate_edge(
        self, implied_probability: float, market_yes_price: float
    ) -> float:
        """
        Calculate the edge: gap between implied probability and market price.

        Edge represents the expected value of taking a position:
        - edge = implied_probability - market_yes_price
        - Positive edge: market is underpricing YES
        - Negative edge: market is overpricing YES

        Args:
            implied_probability: Signal-derived probability (0.0 to 1.0)
            market_yes_price: Current Kalshi market YES price (0.0 to 1.0)

        Returns:
            Edge as probability point difference (can be negative)
        """
        return implied_probability - market_yes_price
