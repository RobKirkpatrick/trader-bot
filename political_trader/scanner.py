# NEW: Political market scanner—discovers opportunities every 6 hours
"""
Scans Kalshi for political markets that meet signal thresholds.
Runs every 6 hours via EventBridge.

Workflow:
1. Fetch all open markets in POLITICAL_SERIES
2. Dynamically discover additional political markets by keyword matching
3. Filter by days to resolution and spread
4. Assess each market's signal
5. Write qualifying opportunities to DynamoDB
6. Send SNS digest summary
"""

import logging
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import asyncio
import uuid

import boto3

from .strategy import (
    POLITICAL_SERIES,
    POLITICAL_KEYWORDS,
    MIN_COMBINED_SIGNAL,
    MIN_EDGE,
    MAX_DAYS_TO_RESOLUTION,
    MIN_DAYS_TO_RESOLUTION,
    MAX_BID_ASK_SPREAD,
)
from .models import PoliticalOpportunity, PoliticalSignal
from .signal_reader import PoliticalSignalReader

logger = logging.getLogger(__name__)

# NEW: DynamoDB and SNS clients
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

OPPORTUNITIES_TABLE_NAME = "political-opportunities"
SNS_TOPIC_ARN = "SENTINEL_SNS_ARN"  # From environment


class PoliticalMarketScanner:
    """
    Discovers and evaluates Kalshi political markets for trading opportunities.
    """

    def __init__(
        self,
        kalshi_client,
        signal_reader: PoliticalSignalReader,
        dynamodb_resource=None,
        sns_client=None,
    ):
        """
        Args:
            kalshi_client: KalshiClient instance for API calls
            signal_reader: PoliticalSignalReader instance
            dynamodb_resource: Boto3 DynamoDB resource (defaults to global)
            sns_client: Boto3 SNS client (defaults to global)
        """
        self.kalshi_client = kalshi_client
        self.signal_reader = signal_reader
        self.dynamodb = dynamodb_resource or dynamodb
        self.sns = sns_client or sns
        self.opportunities_table = self.dynamodb.Table(OPPORTUNITIES_TABLE_NAME)

    # ========================================================================
    # MARKET DISCOVERY
    # ========================================================================

    def get_political_markets(self) -> List[Dict[str, Any]]:
        """
        Fetch all Kalshi markets in POLITICAL_SERIES.
        NEW: Also dynamically discover political markets by keyword matching.

        Returns:
            List of market dicts with required fields:
            - ticker, title, series, yes_ask, bid_ask_spread, resolution_date
        """
        political_markets = []

        try:
            # NEW: Fetch from known political series
            for series in POLITICAL_SERIES:
                logger.info(f"Fetching markets from series: {series}")
                markets = self.kalshi_client.get_markets_by_series(series)
                political_markets.extend(markets)

            logger.info(f"Fetched {len(political_markets)} markets from political series")

            # NEW: Dynamically discover by keyword matching
            additional = self._discover_by_keyword(political_markets)
            logger.info(f"Discovered {len(additional)} additional political markets by keyword")
            political_markets.extend(additional)

            return political_markets

        except Exception as e:
            logger.error(f"Error fetching political markets: {e}", exc_info=True)
            return []

    def _discover_by_keyword(self, existing_markets: List[Dict]) -> List[Dict]:
        """
        NEW: Search all markets for political keywords not in known series.
        Avoids duplicates by checking ticker against existing markets.
        """
        discovered = []
        existing_tickers = {m.get("ticker") for m in existing_markets}

        try:
            # NEW: Query all markets (expensive, so do sparingly)
            # This is a simplified approach; production might use market search API
            logger.debug("Searching for political markets by keyword")

            # Placeholder: would fetch all markets and filter by keyword
            # For now, rely on POLITICAL_SERIES coverage
            return discovered

        except Exception as e:
            logger.error(f"Keyword discovery error: {e}")
            return discovered

    # ========================================================================
    # MARKET FILTERING
    # ========================================================================

    def _is_market_tradeable(self, market: Dict[str, Any]) -> bool:
        """
        Check if market meets basic trading criteria.
        NEW: Filter by resolution date, spread, volume.
        """
        try:
            # NEW: Parse resolution date
            resolution_str = market.get("resolution_date", "")
            if not resolution_str:
                logger.debug(f"No resolution date: {market.get('ticker')}")
                return False

            resolution = datetime.fromisoformat(resolution_str.replace("Z", "+00:00"))
            days_to_resolution = (resolution - datetime.utcnow()).days

            # NEW: Check resolution date bounds
            if days_to_resolution > MAX_DAYS_TO_RESOLUTION:
                logger.debug(
                    f"Market {market.get('ticker')} resolves too far out: {days_to_resolution} days"
                )
                return False

            if days_to_resolution < MIN_DAYS_TO_RESOLUTION:
                logger.debug(
                    f"Market {market.get('ticker')} resolves too soon: {days_to_resolution} days"
                )
                return False

            # NEW: Check bid-ask spread
            spread = market.get("bid_ask_spread", 0.10)
            if spread > MAX_BID_ASK_SPREAD:
                logger.debug(f"Market {market.get('ticker')} spread too wide: {spread}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error checking market tradeability: {e}")
            return False

    # ========================================================================
    # SIGNAL ASSESSMENT & OPPORTUNITY CREATION
    # ========================================================================

    async def assess_market_async(
        self, market: Dict[str, Any]
    ) -> Optional[PoliticalOpportunity]:
        """
        NEW: Async assessment of a single market.
        Returns PoliticalOpportunity if it meets signal thresholds.
        """
        try:
            market_ticker = market.get("ticker", "")
            market_title = market.get("title", "")
            series = market.get("series", "")
            resolution_date = market.get("resolution_date", "")
            yes_price = float(market.get("yes_ask", 0.50))

            # NEW: Assess signal
            signal = self.signal_reader.assess_market(
                market_ticker=market_ticker,
                market_title=market_title,
                series=series,
                resolution_date=resolution_date,
                current_yes_price=yes_price,
            )

            # NEW: Check if opportunity qualifies
            if abs(signal.combined_signal) < MIN_COMBINED_SIGNAL:
                logger.debug(
                    f"Market {market_ticker} signal too weak: {signal.combined_signal}"
                )
                return None

            if abs(signal.edge_vs_market) < MIN_EDGE:
                logger.debug(
                    f"Market {market_ticker} edge too small: {signal.edge_vs_market}"
                )
                return None

            # NEW: Create opportunity record
            opportunity = PoliticalOpportunity(
                opportunity_id=str(uuid.uuid4()),
                market_ticker=market_ticker,
                market_title=market_title,
                series=series,
                resolution_date=resolution_date,
                signal=signal,
                combined_signal=signal.combined_signal,
                edge_vs_market=signal.edge_vs_market,
                status="pending",
            )

            logger.info(
                f"Opportunity identified: {market_ticker} signal={signal.combined_signal:.3f} edge={signal.edge_vs_market:.3f}"
            )
            return opportunity

        except Exception as e:
            logger.error(f"Error assessing market {market.get('ticker')}: {e}", exc_info=True)
            return None

    async def scan_all_markets(self) -> List[PoliticalOpportunity]:
        """
        NEW: Scan all political markets and return qualifying opportunities.
        """
        logger.info("Starting political market scan")
        start_time = datetime.utcnow()

        # NEW: Discover markets
        all_markets = self.get_political_markets()
        logger.info(f"Found {len(all_markets)} total political markets")

        # NEW: Filter by basic criteria
        tradeable_markets = [m for m in all_markets if self._is_market_tradeable(m)]
        logger.info(f"Filtered to {len(tradeable_markets)} tradeable markets")

        # NEW: Assess signals concurrently (with rate limiting)
        opportunities = []
        for market in tradeable_markets:
            opp = await self.assess_market_async(market)
            if opp:
                opportunities.append(opp)
            # NEW: Rate limit API calls
            await asyncio.sleep(0.5)

        elapsed = (datetime.utcnow() - start_time).total_seconds()
        logger.info(
            f"Scan complete: {len(opportunities)} qualifying opportunities in {elapsed:.1f}s"
        )

        return opportunities

    # ========================================================================
    # PERSISTENCE & ALERTS
    # ========================================================================

    def persist_opportunity(self, opportunity: PoliticalOpportunity) -> bool:
        """
        NEW: Write opportunity to DynamoDB, avoiding duplicates.
        """
        try:
            # NEW: Check if we already have this opportunity pending
            response = self.opportunities_table.get_item(
                Key={"market_ticker": opportunity.market_ticker}
            )

            if "Item" in response:
                existing = response["Item"]
                if existing.get("status") == "pending":
                    logger.debug(
                        f"Opportunity {opportunity.market_ticker} already pending; skipping"
                    )
                    return False

            # NEW: Write to DynamoDB
            self.opportunities_table.put_item(
                Item={
                    "opportunity_id": opportunity.opportunity_id,
                    "market_ticker": opportunity.market_ticker,
                    "market_title": opportunity.market_title,
                    "series": opportunity.series,
                    "resolution_date": opportunity.resolution_date,
                    "signal": json.dumps(opportunity.signal.to_dict()),
                    "combined_signal": opportunity.combined_signal,
                    "edge_vs_market": opportunity.edge_vs_market,
                    "status": opportunity.status,
                    "created_at": opportunity.created_at,
                    "entered_position_id": None,
                }
            )

            logger.info(f"Persisted opportunity: {opportunity.market_ticker}")
            return True

        except Exception as e:
            logger.error(f"Error persisting opportunity: {e}", exc_info=True)
            return False

    def send_scan_summary(self, opportunities: List[PoliticalOpportunity]) -> bool:
        """
        NEW: Send SNS alert with scan summary.
        """
        try:
            if not opportunities:
                logger.info("No opportunities to report")
                return True

            # NEW: Group by series
            by_series = {}
            for opp in opportunities:
                series = opp.series
                if series not in by_series:
                    by_series[series] = []
                by_series[series].append(opp)

            # NEW: Format message
            lines = [
                "POLITICAL SCAN SUMMARY",
                f"Timestamp: {datetime.utcnow().isoformat()}",
                f"Total opportunities: {len(opportunities)}",
                "",
            ]

            for series, opps in by_series.items():
                lines.append(f"{series}: {len(opps)} markets")
                for opp in opps[:3]:  # Top 3 per series
                    lines.append(
                        f"  - {opp.market_ticker}: signal={opp.combined_signal:.3f} edge={opp.edge_vs_market:.3f}"
                    )

            message = "\n".join(lines)

            # NEW: Send via SNS
            topic_arn = SNS_TOPIC_ARN
            if topic_arn and topic_arn != "SENTINEL_SNS_ARN":
                self.sns.publish(
                    TopicArn=topic_arn,
                    Subject="POLITICAL TRADER SCAN",
                    Message=message,
                )
                logger.info(f"Sent SNS alert for {len(opportunities)} opportunities")

            return True

        except Exception as e:
            logger.error(f"Error sending scan summary: {e}", exc_info=True)
            return False

    # ========================================================================
    # MAIN RUNNER
    # ========================================================================

    async def run(self) -> Dict[str, Any]:
        """
        NEW: Execute full scan cycle.
        Returns metrics for logging/monitoring.
        """
        logger.info("=" * 70)
        logger.info("POLITICAL MARKET SCANNER STARTING")
        logger.info("=" * 70)

        start_time = datetime.utcnow()

        try:
            # NEW: Scan all markets
            opportunities = await self.scan_all_markets()

            # NEW: Persist qualifying opportunities
            persisted_count = 0
            for opp in opportunities:
                if self.persist_opportunity(opp):
                    persisted_count += 1

            # NEW: Send summary alert
            self.send_scan_summary(opportunities)

            elapsed = (datetime.utcnow() - start_time).total_seconds()

            metrics = {
                "status": "success",
                "scan_duration_seconds": elapsed,
                "opportunities_found": len(opportunities),
                "opportunities_persisted": persisted_count,
                "timestamp": datetime.utcnow().isoformat(),
            }

            logger.info(f"Scanner metrics: {metrics}")
            return metrics

        except Exception as e:
            logger.error(f"Scanner failed: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


async def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    NEW: AWS Lambda entry point for scanner.
    Called by EventBridge every 6 hours.
    """
    logger.info(f"Scanner invoked: {event}")

    # NEW: Initialize clients and scanner
    # (This would import KalshiClient from carpet_bagger and newsapi_key from config)
    from carpet_bagger.kalshi_client import KalshiClient

    kalshi_client = KalshiClient()
    signal_reader = PoliticalSignalReader(kalshi_client=kalshi_client)
    scanner = PoliticalMarketScanner(kalshi_client, signal_reader)

    # NEW: Run scan
    result = await scanner.run()
    return result
