# NEW: Weather market scanner — runs every 4 hours

"""
Scanner phase of weather trading bot.
Searches for weather-related Kalshi markets, parses them, computes NWS edge,
and logs opportunities to DynamoDB for execution in monitor phase.
"""

import logging
import asyncio
from typing import List, Optional
from datetime import datetime, timedelta
import uuid

import aiohttp

from .strategy import (
    CITY_COORDS,
    MIN_EDGE,
    MAX_BID_ASK_SPREAD,
    MIN_DAYS_TO_RESOLUTION,
    MAX_DAYS_TO_RESOLUTION,
    OPTIMAL_ENTRY_WINDOW_DAYS,
)
from .models import MarketOpportunity
from .market_parser import WeatherMarketParser
from .nws_client import NWSClient, fetch_nws_forecast

logger = logging.getLogger(__name__)


class WeatherMarketScanner:
    """
    Scans Kalshi for weather markets and identifies trading opportunities.
    """

    def __init__(self, kalshi_client, dynamo_client=None, sns_client=None):
        """
        Args:
            kalshi_client: carpet_bagger.kalshi_client.KalshiClient instance
            dynamo_client: boto3 DynamoDB resource (optional, for storage)
            sns_client: boto3 SNS client (optional, for alerts)
        """
        self.kalshi_client = kalshi_client
        self.dynamo_client = dynamo_client
        self.sns_client = sns_client

        self.parser = WeatherMarketParser(CITY_COORDS)
        self.nws_client = NWSClient()

    async def run(self) -> dict:
        """
        Execute full scanner loop:
        1. Fetch all open Kalshi markets (search for weather keywords)
        2. Parse each market
        3. Calculate NWS edge
        4. Store opportunities in DynamoDB
        5. Send SNS alert with summary

        Returns:
            Summary dict with results
        """
        logger.info("Weather market scanner starting...")

        opportunities_found = []
        markets_scanned = 0
        errors = []

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Search for weather-related markets
                markets = await self._search_weather_markets()
                markets_scanned = len(markets)
                logger.info(f"Found {markets_scanned} potential weather markets")

                # Step 2-4: Parse, compute edge, and store
                for market in markets:
                    try:
                        opp = await self._process_market(market, session)
                        if opp:
                            opportunities_found.append(opp)
                    except Exception as e:
                        logger.error(f"Error processing market {market.get('ticker')}: {e}")
                        errors.append(str(e))

            # Step 5: Store results and alert
            if opportunities_found:
                await self._store_opportunities(opportunities_found)
                await self._send_alert(opportunities_found, markets_scanned)

            logger.info(
                f"Scanner complete: {len(opportunities_found)} opportunities found, {markets_scanned} markets scanned"
            )

            return {
                "status": "success",
                "markets_scanned": markets_scanned,
                "opportunities_found": len(opportunities_found),
                "errors": errors,
            }

        except Exception as e:
            logger.error(f"Scanner failed: {e}")
            return {"status": "error", "error": str(e)}

    async def _search_weather_markets(self) -> List[dict]:
        """
        Search Kalshi for weather-related markets.
        Tries both keyword search and series listing.
        """
        markets = []

        # Keywords to search for
        keywords = ["weather", "rain", "precipitation", "temperature", "temp", "snow", "wind"]

        for keyword in keywords:
            try:
                # Use Kalshi client to search
                # Assuming kalshi_client has a search method
                found = await asyncio.to_thread(
                    self.kalshi_client.search_markets, {"query": keyword, "status": "open"}
                )
                if found:
                    markets.extend(found)
            except Exception as e:
                logger.warning(f"Error searching keyword '{keyword}': {e}")

        # Deduplicate by ticker
        seen_tickers = set()
        unique_markets = []
        for market in markets:
            ticker = market.get("ticker")
            if ticker and ticker not in seen_tickers:
                unique_markets.append(market)
                seen_tickers.add(ticker)

        return unique_markets

    async def _process_market(self, market: dict, session: aiohttp.ClientSession) -> Optional[MarketOpportunity]:
        """
        Process a single market:
        1. Parse title
        2. Validate city and date constraints
        3. Fetch NWS forecast
        4. Calculate edge
        5. Return opportunity if qualifies

        Args:
            market: Kalshi market dict
            session: aiohttp session for NWS API calls

        Returns:
            MarketOpportunity if edge > MIN_EDGE, else None
        """
        # Parse market title
        parsed = self.parser.parse_market(market)
        if not parsed:
            return None

        city = parsed.get("city")
        target_date = parsed.get("target_date")

        # Validate city
        if city not in CITY_COORDS:
            logger.debug(f"City '{city}' not in coverage list, skipping")
            return None

        # Validate resolution date
        days_to_resolution = self._days_until(target_date)
        if not (MIN_DAYS_TO_RESOLUTION <= days_to_resolution <= MAX_DAYS_TO_RESOLUTION):
            logger.debug(
                f"{city}: {target_date} is {days_to_resolution:.1f} days out, outside [{MIN_DAYS_TO_RESOLUTION}, {MAX_DAYS_TO_RESOLUTION}] window"
            )
            return None

        # Extract market prices
        yes_bid = market.get("yes_bid", 0.0)
        yes_ask = market.get("yes_ask", 1.0)
        no_bid = market.get("no_bid", 0.0)
        no_ask = market.get("no_ask", 1.0)
        yes_last = market.get("last_traded_price", (yes_bid + yes_ask) / 2)

        # Check bid-ask spread
        bid_ask_spread = yes_ask - yes_bid
        if bid_ask_spread > MAX_BID_ASK_SPREAD:
            logger.debug(f"{market.get('ticker')}: spread {bid_ask_spread:.3f} exceeds max {MAX_BID_ASK_SPREAD}")
            return None

        # Fetch NWS forecast
        lat, lon = CITY_COORDS[city]
        try:
            nws_forecast = await fetch_nws_forecast(city, lat, lon, target_date, self.nws_client, session)
        except Exception as e:
            logger.error(f"Failed to fetch NWS for {city} {target_date}: {e}")
            return None

        # Get NWS probability for YES outcome
        nws_probability = self.parser.get_nws_probability(parsed, nws_forecast)

        # Calculate edges
        edge_yes = nws_probability - yes_ask  # Edge if we buy YES
        edge_no = (1.0 - nws_probability) - no_ask  # Edge if we buy NO

        # Determine best side
        if edge_yes > edge_no:
            recommended_side = "yes"
            recommended_edge = edge_yes
        else:
            recommended_side = "no"
            recommended_edge = edge_no

        # Skip if edge too small
        if recommended_edge < MIN_EDGE:
            logger.debug(f"{market.get('ticker')}: edge {recommended_edge:.3f} < MIN_EDGE {MIN_EDGE}")
            return None

        # Construct opportunity
        opp = MarketOpportunity(
            opportunity_id=str(uuid.uuid4()),
            market_ticker=market.get("ticker"),
            market_title=market.get("title", ""),
            city=city,
            weather_type=parsed.get("weather_type"),
            threshold=parsed.get("threshold"),
            direction=parsed.get("direction"),
            target_date=target_date,
            kalshi_yes_bid=yes_bid,
            kalshi_yes_ask=yes_ask,
            kalshi_yes_last=yes_last,
            nws_probability=nws_probability,
            nws_fetched_at=nws_forecast.fetched_at,
            edge_yes=edge_yes,
            edge_no=edge_no,
            recommended_side=recommended_side,
            recommended_edge=recommended_edge,
            days_to_resolution=days_to_resolution,
            bid_ask_spread=bid_ask_spread,
            created_at=datetime.utcnow().isoformat() + "Z",
            expires_at=(datetime.utcnow() + timedelta(hours=4)).isoformat() + "Z",
        )

        logger.info(
            f"Opportunity found: {opp.market_ticker} — {city} {opp.weather_type} @ {opp.recommended_side} "
            f"(NWS: {nws_probability:.1%}, edge: +{recommended_edge:.2f})"
        )

        return opp

    async def _store_opportunities(self, opportunities: List[MarketOpportunity]) -> None:
        """
        Store opportunities in DynamoDB for the monitor phase to consider.
        """
        if not self.dynamo_client:
            logger.warning("No DynamoDB client, skipping storage")
            return

        table = self.dynamo_client.Table("weather-opportunities")

        for opp in opportunities:
            try:
                table.put_item(Item=opp.to_dict())
            except Exception as e:
                logger.error(f"Failed to store opportunity {opp.opportunity_id}: {e}")

    async def _send_alert(self, opportunities: List[MarketOpportunity], markets_scanned: int) -> None:
        """
        Send SNS alert with scanner results.
        """
        if not self.sns_client:
            logger.warning("No SNS client, skipping alert")
            return

        # Format opportunity list
        opp_lines = []
        for opp in opportunities[:10]:  # Top 10
            edge_pct = opp.recommended_edge * 100
            opp_lines.append(
                f"  • {opp.city} {opp.weather_type} @ {opp.recommended_side.upper()} "
                f"(NWS: {opp.nws_probability:.0%}, edge: +{edge_pct:.0f}¢)"
            )

        message = (
            f"WEATHER SCAN COMPLETE\n"
            f"Markets scanned: {markets_scanned}\n"
            f"Opportunities found: {len(opportunities)}\n"
            f"\n"
            f"Top opportunities:\n"
            f"{chr(10).join(opp_lines)}\n"
        )

        try:
            self.sns_client.publish(Topic=os.getenv("SENTINEL_SNS_ARN"), Message=message)
        except Exception as e:
            logger.error(f"Failed to send SNS alert: {e}")

    @staticmethod
    def _days_until(date_str: str) -> float:
        """
        Calculate days from now until target date string (YYYY-MM-DD).
        """
        target = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.now()
        delta = target - now
        return delta.total_seconds() / 86400.0


async def run_scanner(kalshi_client, dynamo_client=None, sns_client=None) -> dict:
    """
    Standalone function to run scanner once.
    Useful for EventBridge Lambda invocation.
    """
    scanner = WeatherMarketScanner(kalshi_client, dynamo_client, sns_client)
    return await scanner.run()
