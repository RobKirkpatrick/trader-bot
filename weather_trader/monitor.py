# NEW: Weather position monitor — runs every 6 hours

"""
Monitor phase of weather trading bot.
Executes trades from scanner opportunities, monitors open positions,
and exits early if NWS forecast reverses against us.
"""

import logging
import asyncio
import uuid
from typing import List, Optional
from datetime import datetime, timedelta

import aiohttp

from .strategy import (
    CITY_COORDS,
    NWS_REVERSAL_THRESHOLD,
    MIN_POSITION_SIZE,
    MAX_POSITION_PER_MARKET,
    MAX_SIMULTANEOUS_POSITIONS,
    MAX_PCT_BANKROLL,
    MIN_EDGE,
)
from .models import WeatherPosition, MarketOpportunity
from .nws_client import NWSClient, fetch_nws_forecast
from .market_parser import WeatherMarketParser

logger = logging.getLogger(__name__)


class WeatherPositionMonitor:
    """
    Monitors and executes weather positions.
    Phase A: Execute trades from scanner opportunities
    Phase B: Monitor open positions for early exit
    """

    def __init__(self, kalshi_client, dynamo_client=None, sns_client=None):
        """
        Args:
            kalshi_client: carpet_bagger.kalshi_client.KalshiClient instance
            dynamo_client: boto3 DynamoDB resource (optional)
            sns_client: boto3 SNS client (optional)
        """
        self.kalshi_client = kalshi_client
        self.dynamo_client = dynamo_client
        self.sns_client = sns_client

        self.parser = WeatherMarketParser(CITY_COORDS)
        self.nws_client = NWSClient()

    async def run(self) -> dict:
        """
        Execute both phases:
        Phase A: Execute new trades from opportunities
        Phase B: Monitor existing positions
        """
        logger.info("Weather position monitor starting...")

        results = {"phase_a": {}, "phase_b": {}}

        try:
            async with aiohttp.ClientSession() as session:
                # Phase A: Execute
                results["phase_a"] = await self._phase_a_execute(session)

                # Phase B: Monitor
                results["phase_b"] = await self._phase_b_monitor(session)

            logger.info(f"Monitor complete: {results}")
            return {"status": "success", "details": results}

        except Exception as e:
            logger.error(f"Monitor failed: {e}")
            return {"status": "error", "error": str(e)}

    async def _phase_a_execute(self, session: aiohttp.ClientSession) -> dict:
        """
        Phase A: Execute new trades from scanner opportunities.

        1. Fetch open opportunities from DynamoDB
        2. Check position count and bankroll usage
        3. Re-fetch NWS to confirm edge
        4. Place Kalshi order
        5. Write WeatherPosition to DynamoDB
        6. Send SNS alert
        """
        logger.info("Phase A: Executing new opportunities...")

        opportunities = await self._get_open_opportunities()
        logger.info(f"Found {len(opportunities)} open opportunities")

        executed_positions = []
        errors = []

        # Check current position count
        open_positions = await self._get_open_positions()
        if len(open_positions) >= MAX_SIMULTANEOUS_POSITIONS:
            logger.warning(f"Already at max simultaneous positions ({MAX_SIMULTANEOUS_POSITIONS}), skipping execution")
            return {"executed": 0, "skipped": len(opportunities), "reason": "max_positions"}

        for opp in opportunities:
            try:
                position = await self._execute_opportunity(opp, open_positions, session)
                if position:
                    executed_positions.append(position)
                    open_positions.append(position)  # Update local count
            except Exception as e:
                logger.error(f"Failed to execute opportunity {opp.opportunity_id}: {e}")
                errors.append(str(e))

        # Send summary alert
        if executed_positions:
            await self._send_execution_alert(executed_positions)

        return {
            "executed": len(executed_positions),
            "errors": len(errors),
            "error_details": errors,
        }

    async def _execute_opportunity(
        self, opp: MarketOpportunity, existing_positions: List[WeatherPosition], session: aiohttp.ClientSession
    ) -> Optional[WeatherPosition]:
        """
        Execute a single opportunity:
        1. Re-fetch NWS to confirm edge hasn't collapsed
        2. Check if position already exists
        3. Calculate position size
        4. Place Kalshi order
        5. Write position to DynamoDB
        6. Return position object
        """
        # Re-fetch NWS to ensure edge is still valid
        city = opp.city
        lat, lon = CITY_COORDS[city]

        try:
            nws_forecast = await fetch_nws_forecast(city, lat, lon, opp.target_date, self.nws_client, session)
        except Exception as e:
            logger.error(f"Re-fetch NWS failed for {city}: {e}")
            return None

        nws_probability = self.parser.get_nws_probability(
            {
                "city": city,
                "weather_type": opp.weather_type,
                "threshold": opp.threshold,
                "direction": opp.direction,
            },
            nws_forecast,
        )

        # Recalculate edge with fresh NWS
        if opp.recommended_side == "yes":
            fresh_edge = nws_probability - opp.kalshi_yes_ask
        else:
            fresh_edge = (1.0 - nws_probability) - (1.0 - opp.kalshi_yes_bid)

        # Check if edge still qualifies
        if fresh_edge < MIN_EDGE:
            logger.warning(
                f"{opp.market_ticker}: Fresh edge {fresh_edge:.3f} < MIN_EDGE, skipping execution"
            )
            return None

        # Check if we already have a position in this market
        for pos in existing_positions:
            if pos.market_ticker == opp.market_ticker:
                logger.debug(f"{opp.market_ticker}: Position already exists, skipping")
                return None

        # Calculate position size
        # Start with min, scale based on edge (stronger edge = bigger position)
        edge_factor = min(1.0, (fresh_edge - MIN_EDGE) / 0.20)  # Scale up to 0.30 edge
        position_size = MIN_POSITION_SIZE + (MAX_POSITION_PER_MARKET - MIN_POSITION_SIZE) * edge_factor
        position_size = min(position_size, MAX_POSITION_PER_MARKET)

        logger.info(
            f"Executing {opp.market_ticker}: {opp.recommended_side.upper()} @ {opp.kalshi_yes_ask if opp.recommended_side == 'yes' else 1-opp.kalshi_yes_bid:.3f}, "
            f"size={position_size}, edge=+{fresh_edge:.2f}"
        )

        # Place Kalshi order
        try:
            order_response = await asyncio.to_thread(
                self._place_kalshi_order,
                opp.market_ticker,
                opp.recommended_side,
                position_size,
                opp.kalshi_yes_ask if opp.recommended_side == "yes" else (1.0 - opp.kalshi_yes_bid),
            )

            if not order_response or "id" not in order_response:
                logger.error(f"Order failed for {opp.market_ticker}: {order_response}")
                return None

            order_id = order_response["id"]
        except Exception as e:
            logger.error(f"Failed to place Kalshi order for {opp.market_ticker}: {e}")
            return None

        # Create position object
        now = datetime.utcnow().isoformat() + "Z"
        position = WeatherPosition(
            position_id=str(uuid.uuid4()),
            market_ticker=opp.market_ticker,
            market_title=opp.market_title,
            city=opp.city,
            weather_type=opp.weather_type,
            threshold=opp.threshold,
            direction=opp.direction,
            resolution_date=opp.target_date,
            nws_probability=nws_probability,
            nws_forecast_temp=nws_forecast.forecast_high,
            nws_precip_prob=nws_forecast.precip_probability,
            nws_fetched_at=nws_forecast.fetched_at,
            direction_bet=opp.recommended_side,
            entry_price=opp.kalshi_yes_ask if opp.recommended_side == "yes" else (1.0 - opp.kalshi_yes_bid),
            edge_at_entry=fresh_edge,
            contracts=int(position_size),
            position_size_usd=position_size,
            order_id=order_id,
            status="open",
            opened_at=now,
            closed_at=None,
            last_updated=now,
        )

        # Store in DynamoDB
        if self.dynamo_client:
            try:
                table = self.dynamo_client.Table("weather-positions")
                table.put_item(Item=position.to_dict())
            except Exception as e:
                logger.error(f"Failed to store position {position.position_id}: {e}")

        return position

    async def _phase_b_monitor(self, session: aiohttp.ClientSession) -> dict:
        """
        Phase B: Monitor open positions.

        For each open position:
        1. Check if resolved (market settled) → record outcome
        2. Re-fetch NWS forecast
        3. If NWS has shifted >NWS_REVERSAL_THRESHOLD against us → exit early
        4. Otherwise hold to resolution
        """
        logger.info("Phase B: Monitoring open positions...")

        open_positions = await self._get_open_positions()
        logger.info(f"Found {len(open_positions)} open positions to monitor")

        monitored = 0
        exited = 0
        errors = []

        for position in open_positions:
            try:
                # Check if market has resolved
                market_state = await asyncio.to_thread(
                    self.kalshi_client.get_market, position.market_ticker
                )

                if market_state.get("status") == "closed":
                    # Market resolved
                    await self._close_position(position, "resolution", market_state)
                    exited += 1
                    continue

                # Re-fetch NWS for this city/date
                city = position.city
                lat, lon = CITY_COORDS[city]
                nws_forecast = await fetch_nws_forecast(city, lat, lon, position.resolution_date, self.nws_client, session)

                # Get updated probability
                nws_probability = self.parser.get_nws_probability(
                    {
                        "city": city,
                        "weather_type": position.weather_type,
                        "threshold": position.threshold,
                        "direction": position.direction,
                    },
                    nws_forecast,
                )

                # Check if NWS has reversed against our position
                prob_shift = abs(nws_probability - position.nws_probability)

                if prob_shift > NWS_REVERSAL_THRESHOLD:
                    # Check direction of shift
                    if position.direction_bet == "yes":
                        is_against_us = nws_probability < position.nws_probability
                    else:
                        is_against_us = nws_probability > position.nws_probability

                    if is_against_us:
                        logger.warning(
                            f"{position.market_ticker}: NWS reversed by {prob_shift:.1%} (from {position.nws_probability:.0%} to {nws_probability:.0%}), exiting"
                        )
                        await self._close_position(position, "nws_reversal", {"new_nws_prob": nws_probability})
                        exited += 1
                        continue

                monitored += 1

            except Exception as e:
                logger.error(f"Error monitoring position {position.position_id}: {e}")
                errors.append(str(e))

        return {
            "monitored": monitored,
            "exited": exited,
            "errors": len(errors),
        }

    def _place_kalshi_order(self, market_ticker: str, side: str, amount: float, price: float) -> dict:
        """
        Place a Kalshi order (synchronous wrapper for async context).
        """
        # Use kalshi_client's order placement method
        # This is a placeholder — adjust to actual Kalshi API signature
        try:
            order = self.kalshi_client.place_order(
                ticker=market_ticker,
                side=side.upper(),
                type="limit",
                amount=amount,
                price=price,
            )
            return order
        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
            raise

    async def _close_position(self, position: WeatherPosition, exit_reason: str, extra_data: dict = None) -> None:
        """
        Close a position and update DynamoDB.
        """
        position.status = "closed"
        position.exit_reason = exit_reason
        position.closed_at = datetime.utcnow().isoformat() + "Z"
        position.last_updated = position.closed_at

        # Store updated position
        if self.dynamo_client:
            try:
                table = self.dynamo_client.Table("weather-positions")
                table.update_item(
                    Key={"position_id": position.position_id},
                    UpdateExpression="SET #status=:status, exit_reason=:reason, closed_at=:closed, last_updated=:updated",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":status": "closed",
                        ":reason": exit_reason,
                        ":closed": position.closed_at,
                        ":updated": position.last_updated,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to update position {position.position_id}: {e}")

    async def _get_open_opportunities(self) -> List[MarketOpportunity]:
        """Fetch open opportunities from DynamoDB."""
        if not self.dynamo_client:
            return []

        try:
            table = self.dynamo_client.Table("weather-opportunities")
            response = table.scan(FilterExpression="attribute_not_exists(executed_at)")
            items = response.get("Items", [])
            return [MarketOpportunity.from_dict(item) for item in items]
        except Exception as e:
            logger.error(f"Failed to fetch opportunities: {e}")
            return []

    async def _get_open_positions(self) -> List[WeatherPosition]:
        """Fetch open positions from DynamoDB."""
        if not self.dynamo_client:
            return []

        try:
            table = self.dynamo_client.Table("weather-positions")
            response = table.scan(FilterExpression="#status=:status", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":status": "open"})
            items = response.get("Items", [])
            return [WeatherPosition.from_dict(item) for item in items]
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}")
            return []

    async def _send_execution_alert(self, positions: List[WeatherPosition]) -> None:
        """Send SNS alert for executed trades."""
        if not self.sns_client:
            return

        message = "WEATHER BETS PLACED:\n"
        for pos in positions:
            nws_pct = pos.nws_probability * 100
            price_pct = pos.entry_price * 100
            edge_pct = pos.edge_at_entry * 100
            message += (
                f"  • {pos.city} {pos.weather_type} @ {pos.direction_bet.upper()} "
                f"— Kalshi: {price_pct:.0f}¢, NWS: {nws_pct:.0f}%, edge: +{edge_pct:.0f}¢\n"
            )

        try:
            import os

            self.sns_client.publish(Topic=os.getenv("SENTINEL_SNS_ARN"), Message=message)
        except Exception as e:
            logger.error(f"Failed to send execution alert: {e}")


async def run_monitor(kalshi_client, dynamo_client=None, sns_client=None) -> dict:
    """
    Standalone function to run monitor once.
    Useful for EventBridge Lambda invocation.
    """
    monitor = WeatherPositionMonitor(kalshi_client, dynamo_client, sns_client)
    return await monitor.run()
