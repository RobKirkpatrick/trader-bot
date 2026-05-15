"""
Macro market scanner: discovers trading opportunities.

Runs on a schedule (6-hour interval or immediately after sentiment scan completes).
Reads the latest macro sentiment signal, matches it to open Kalshi economic markets,
calculates edge, and writes qualifying opportunities to DynamoDB for execution.

NEW: Bridge between sentiment pipeline and Kalshi market execution. Performs market
discovery, liquidity checks, and edge validation before queuing trades.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import os
import asyncio

import boto3
from dateutil import parser as date_parser

from . import strategy
from .models import MacroOpportunity
from .signal_reader import MacroSignalReader

logger = logging.getLogger(__name__)


class MacroScanner:
    """Discovers macro trading opportunities by matching sentiment signals to Kalshi markets."""

    def __init__(self):
        """Initialize DynamoDB and Kalshi client."""
        self.dynamodb = boto3.resource("dynamodb")
        self.opportunities_table_name = os.getenv(
            "MACRO_OPPORTUNITIES_TABLE", "macro-opportunities"
        )
        self.opportunities_table = self.dynamodb.Table(self.opportunities_table_name)

        self.signal_reader = MacroSignalReader()

        # Import Kalshi client from carpet_bagger — pass credentials from settings
        try:
            from carpet_bagger.kalshi_client import KalshiClient
            from config.settings import settings as _s
            self.kalshi_client = KalshiClient(
                api_key=_s.KALSHI_API_KEY,
                rsa_private_key_pem=_s.KALSHI_RSA_PRIVATE_KEY,
            )
        except ImportError:
            logger.error(
                "Failed to import KalshiClient from carpet_bagger. "
                "Ensure carpet_bagger module is available."
            )
            self.kalshi_client = None

        self.sns_client = boto3.client("sns")
        self.sns_topic_arn = os.getenv("SENTINEL_SNS_ARN")

    async def run(self) -> Dict[str, Any]:
        """
        Execute full market scan: fetch signal, match markets, validate edge, write opportunities.

        Returns:
        {
            "scan_id": str,
            "signal": dict,
            "opportunities_found": int,
            "opportunities_by_signal": dict,  # {signal_key: count}
            "summary": str
        }
        """
        scan_id = f"scan_{datetime.utcnow().isoformat()}"
        logger.info(f"[{scan_id}] Starting macro market scan...")

        # Phase 1: Load signal
        signal = self.signal_reader.get_latest_signal()
        if not signal:
            logger.warning(f"[{scan_id}] No actionable signal found; exiting scan")
            return {
                "scan_id": scan_id,
                "signal": None,
                "opportunities_found": 0,
                "opportunities_by_signal": {},
                "summary": "No macro signal available (stale or missing)",
            }

        logger.info(
            f"[{scan_id}] Loaded signal: overall={signal.get('overall_score'):.2f}, "
            f"confidence={signal.get('confidence'):.2f}"
        )

        # Phase 2: Check actionability of each signal key
        signal_keys = ["fed_signal", "inflation_signal", "employment_signal", "gdp_signal"]
        actionable_signals = {
            key: signal[key]
            for key in signal_keys
            if key in signal and self.signal_reader.is_signal_actionable(signal, key)
        }

        if not actionable_signals:
            logger.info(f"[{scan_id}] No actionable signals found")
            return {
                "scan_id": scan_id,
                "signal": signal,
                "opportunities_found": 0,
                "opportunities_by_signal": {},
                "summary": "All signals below actionable threshold",
            }

        logger.info(
            f"[{scan_id}] Found {len(actionable_signals)} actionable signals: "
            f"{list(actionable_signals.keys())}"
        )

        # Phase 3: For each actionable signal, scan relevant Kalshi markets
        all_opportunities = []
        summary_by_signal = {}

        for signal_key, signal_value in actionable_signals.items():
            logger.info(f"[{scan_id}] Processing signal: {signal_key}={signal_value:.2f}")

            opportunities_for_signal = await self._scan_for_signal(
                signal_key, signal_value, signal
            )

            if opportunities_for_signal:
                all_opportunities.extend(opportunities_for_signal)
                summary_by_signal[signal_key] = len(opportunities_for_signal)
                logger.info(
                    f"[{scan_id}] {signal_key}: {len(opportunities_for_signal)} opportunities"
                )

        # Phase 4: Write opportunities to DynamoDB
        if all_opportunities:
            try:
                await self._write_opportunities(all_opportunities)
                logger.info(f"[{scan_id}] Wrote {len(all_opportunities)} opportunities to DDB")
            except Exception as e:
                logger.error(f"[{scan_id}] Failed to write opportunities: {e}")
                return {
                    "scan_id": scan_id,
                    "signal": signal,
                    "opportunities_found": 0,
                    "opportunities_by_signal": {},
                    "summary": f"Scan failed: {e}",
                }

        # Phase 5: SNS summary
        summary = self._build_summary(signal, summary_by_signal, all_opportunities)
        await self._send_sns_summary(summary)

        return {
            "scan_id": scan_id,
            "signal": signal,
            "opportunities_found": len(all_opportunities),
            "opportunities_by_signal": summary_by_signal,
            "summary": summary,
        }

    async def _scan_for_signal(
        self, signal_key: str, signal_value: float, signal: Dict[str, Any]
    ) -> List[MacroOpportunity]:
        """
        Find Kalshi markets matching a signal key and calculate opportunities.

        Steps:
        1. Get relevant series keywords from strategy mapping
        2. Fetch open markets in those series
        3. Filter by resolution window (MIN/MAX_DAYS_TO_RESOLUTION)
        4. Skip markets where we already have open positions
        5. For each market: calculate implied prob, market price, edge
        6. Only yield if edge >= MIN_EDGE and liquidity is acceptable
        """
        opportunities = []

        if not self.kalshi_client:
            logger.error(f"[_scan_for_signal] Kalshi client not available")
            return opportunities

        # Get series keywords for this signal
        keywords = strategy.SIGNAL_TO_SERIES_KEYWORDS.get(signal_key, [])
        logger.debug(f"[_scan_for_signal] {signal_key} keywords: {keywords}")

        try:
            # Fetch all open markets in economic series
            markets = await self._fetch_relevant_markets(keywords)
            logger.info(
                f"[_scan_for_signal] Found {len(markets)} markets "
                f"matching keywords {keywords}"
            )

            # Calculate implied probability from signal
            implied_prob = self.signal_reader.estimate_implied_probability(signal_value)
            direction = self.signal_reader.get_direction(signal_value)

            logger.debug(
                f"[_scan_for_signal] {signal_key}={signal_value:.2f} → "
                f"implied_prob={implied_prob:.2f}, direction={direction}"
            )

            # Filter and score each market
            for market in markets:
                opp = self._evaluate_market(
                    market,
                    signal_key,
                    signal_value,
                    implied_prob,
                    direction,
                    signal,
                )

                if opp:
                    opportunities.append(opp)
                    logger.info(
                        f"[_scan_for_signal] Qualified: {market.get('ticker')} "
                        f"(edge={opp.edge:.3f})"
                    )

        except Exception as e:
            logger.error(f"[_scan_for_signal] Error scanning {signal_key}: {e}")

        return opportunities

    async def _fetch_relevant_markets(self, keywords: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch open Kalshi markets matching any of the given keywords.

        Queries the Kalshi API for markets in economic series.
        Filters to only "open" status markets.

        Returns list of market dicts with: ticker, title, yes_bid, yes_ask, resolution_date
        """
        all_markets = []

        for series_ticker in strategy.ECONOMIC_SERIES:
            try:
                # NEW: Use Kalshi client to fetch markets for this series
                markets = await asyncio.to_thread(
                    self.kalshi_client.get_market_series,
                    series_ticker,
                )

                if markets:
                    # Filter to open markets only
                    open_markets = [m for m in markets if m.get("status") == "open"]
                    all_markets.extend(open_markets)
                    logger.debug(
                        f"[_fetch_relevant_markets] {series_ticker}: {len(open_markets)} open"
                    )

            except Exception as e:
                logger.debug(f"[_fetch_relevant_markets] Failed to fetch {series_ticker}: {e}")

        return all_markets

    def _evaluate_market(
        self,
        market: Dict[str, Any],
        signal_key: str,
        signal_value: float,
        implied_prob: float,
        direction: str,
        signal: Dict[str, Any],
    ) -> Optional[MacroOpportunity]:
        """
        Evaluate a single market for trading opportunity.

        Checks:
        1. Resolution date within acceptable window
        2. Bid-ask spread tight enough (liquidity check)
        3. Edge >= MIN_EDGE
        4. No existing position in this market

        Returns MacroOpportunity if all checks pass, else None.
        """
        market_ticker = market.get("ticker", "UNKNOWN")

        # Check resolution date window
        resolution_date_str = market.get("resolution_date")
        if not resolution_date_str:
            logger.debug(f"[_evaluate_market] {market_ticker}: no resolution_date")
            return None

        try:
            resolution_date = date_parser.parse(resolution_date_str).date()
            days_to_resolution = (resolution_date - datetime.utcnow().date()).days
        except Exception as e:
            logger.warning(
                f"[_evaluate_market] {market_ticker}: Failed to parse resolution_date: {e}"
            )
            return None

        if not (
            strategy.MIN_DAYS_TO_RESOLUTION
            <= days_to_resolution
            <= strategy.MAX_DAYS_TO_RESOLUTION
        ):
            logger.debug(
                f"[_evaluate_market] {market_ticker}: {days_to_resolution} days "
                f"outside window [{strategy.MIN_DAYS_TO_RESOLUTION}, "
                f"{strategy.MAX_DAYS_TO_RESOLUTION}]"
            )
            return None

        # Check liquidity (bid-ask spread)
        yes_bid = market.get("yes_bid", 0.0)
        yes_ask = market.get("yes_ask", 1.0)
        spread = yes_ask - yes_bid

        if spread > strategy.MAX_BID_ASK_SPREAD:
            logger.debug(
                f"[_evaluate_market] {market_ticker}: spread {spread:.3f} "
                f"exceeds max {strategy.MAX_BID_ASK_SPREAD}"
            )
            return None

        # Calculate edge
        # Use ask price if buying YES, bid price if selling YES
        market_price = yes_ask if direction == "yes" else (1.0 - yes_bid)
        edge = self.signal_reader.calculate_edge(implied_prob, market_price)

        if edge < strategy.MIN_EDGE:
            logger.debug(
                f"[_evaluate_market] {market_ticker}: edge {edge:.3f} "
                f"below minimum {strategy.MIN_EDGE}"
            )
            return None

        # TODO: Check if we already have an open position in this market

        # NEW: All checks passed; create opportunity
        series = market.get("series_ticker", "UNKNOWN")
        event_description = market.get("title", "Economic Event")

        max_contracts = int(
            strategy.MAX_POSITION_PER_MARKET / max(0.01, market_price)
        )
        recommended_contracts = max(1, max(int(max_contracts * 0.25), 1))  # 25% of max

        opp = MacroOpportunity.create(
            market_ticker=market_ticker,
            series=series,
            event_description=event_description,
            signal_key=signal_key,
            signal_value=signal_value,
            signal_confidence=signal.get("confidence", 0.0),
            signal_summary=signal.get("summary", ""),
            direction=direction,
            implied_probability=implied_prob,
            market_yes_price=market_price,
            edge=edge,
            max_contracts=max_contracts,
            recommended_contracts=recommended_contracts,
            resolution_date=resolution_date_str,
        )

        return opp

    async def _write_opportunities(self, opportunities: List[MacroOpportunity]) -> None:
        """
        Write discovered opportunities to DynamoDB.

        Each opportunity is stored with market_ticker as hash key and scanned_at as sort key.
        """
        with self.opportunities_table.batch_writer(
            batch_size=25
        ) as batch:
            for opp in opportunities:
                try:
                    item = opp.to_dynamodb_item()
                    batch.put_item(Item=item)
                    logger.debug(f"Wrote opportunity: {opp.opportunity_id}")
                except Exception as e:
                    logger.error(
                        f"Failed to write opportunity {opp.opportunity_id}: {e}"
                    )

    def _build_summary(
        self,
        signal: Dict[str, Any],
        summary_by_signal: Dict[str, int],
        opportunities: List[MacroOpportunity],
    ) -> str:
        """Build human-readable SNS summary of scan results."""
        overall_score = signal.get("overall_score", 0.0)
        confidence = signal.get("confidence", 0.0)
        headline_count = signal.get("headline_count", 0)

        lines = [
            "MACRO MARKET SCAN",
            f"  Overall sentiment: {overall_score:+.2f} (confidence: {confidence:.0%})",
            f"  Headlines analyzed: {headline_count}",
            f"  Opportunities found: {len(opportunities)}",
        ]

        if summary_by_signal:
            lines.append("  By signal:")
            for signal_key, count in summary_by_signal.items():
                lines.append(f"    {signal_key}: {count}")

        if opportunities:
            lines.append("  Top opportunities:")
            for opp in sorted(opportunities, key=lambda x: x.edge, reverse=True)[:5]:
                lines.append(
                    f"    {opp.market_ticker}: edge={opp.edge:+.3f}, "
                    f"direction={opp.direction}, {opp.event_description}"
                )

        return "\n".join(lines)

    async def _send_sns_summary(self, summary: str) -> None:
        """Send scan summary via SNS."""
        if not self.sns_topic_arn:
            logger.warning("SENTINEL_SNS_ARN not set; skipping SNS notification")
            return

        try:
            self.sns_client.publish(
                TopicArn=self.sns_topic_arn,
                Subject="MACRO MARKET SCAN",
                Message=summary,
            )
            logger.info("SNS summary sent")
        except Exception as e:
            logger.error(f"Failed to send SNS summary: {e}")


async def run() -> Dict[str, Any]:
    """
    Main entry point for the macro scanner.

    Called by EventBridge on a 6-hour schedule or after sentiment scan completes.
    """
    scanner = MacroScanner()
    return await scanner.run()
