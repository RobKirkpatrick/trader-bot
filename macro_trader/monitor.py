"""
Macro position monitor: executes pending opportunities and manages open positions.

Runs on a 4-hour schedule. Phases:
- Phase A: Execute pending opportunities (place orders, open positions)
- Phase B: Monitor open positions (detect resolution, check signal reversals, exit if needed)
- Phase C: Daily P&L summary (once per day at 8pm ET)

NEW: Decouples position management from discovery, allowing real-time monitoring
independent of scanner schedule.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Literal
import os
import asyncio
from zoneinfo import ZoneInfo

import boto3

from . import strategy
from .models import MacroPosition, MacroOpportunity
from .signal_reader import MacroSignalReader

logger = logging.getLogger(__name__)


class MacroMonitor:
    """Executes and manages macro positions."""

    def __init__(self):
        """Initialize DynamoDB, Kalshi client, SNS."""
        self.dynamodb = boto3.resource("dynamodb")
        self.opportunities_table = self.dynamodb.Table(
            os.getenv("MACRO_OPPORTUNITIES_TABLE", "macro-opportunities")
        )
        self.positions_table = self.dynamodb.Table(
            os.getenv("MACRO_POSITIONS_TABLE", "macro-positions")
        )

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

        self.signal_reader = MacroSignalReader()
        self.sns_client = boto3.client("sns")
        self.sns_topic_arn = os.getenv("SENTINEL_SNS_ARN")

    async def run(self) -> Dict[str, Any]:
        """
        Execute full monitoring cycle: execute pending opportunities, manage open positions.

        Returns summary of actions taken.
        """
        monitor_id = f"monitor_{datetime.utcnow().isoformat()}"
        logger.info(f"[{monitor_id}] Starting macro monitor run...")

        results = {
            "monitor_id": monitor_id,
            "phase_a_executed": 0,
            "phase_b_closed": 0,
            "phase_b_exited_early": 0,
            "phase_c_summary": "",
            "errors": [],
        }

        # Phase A: Execute pending opportunities
        try:
            executed_count = await self._phase_a_execute_pending(monitor_id)
            results["phase_a_executed"] = executed_count
        except Exception as e:
            logger.error(f"[{monitor_id}] Phase A failed: {e}")
            results["errors"].append(f"Phase A: {e}")

        # Phase B: Monitor open positions
        try:
            closed_count, exited_count = await self._phase_b_monitor_open(monitor_id)
            results["phase_b_closed"] = closed_count
            results["phase_b_exited_early"] = exited_count
        except Exception as e:
            logger.error(f"[{monitor_id}] Phase B failed: {e}")
            results["errors"].append(f"Phase B: {e}")

        # Phase C: Daily P&L summary (once per day at 20:00 ET)
        if self._is_pnl_summary_time():
            try:
                summary = await self._phase_c_pnl_summary(monitor_id)
                results["phase_c_summary"] = summary
                await self._send_sns_summary(summary)
            except Exception as e:
                logger.error(f"[{monitor_id}] Phase C failed: {e}")
                results["errors"].append(f"Phase C: {e}")

        logger.info(
            f"[{monitor_id}] Monitor complete: executed={results['phase_a_executed']}, "
            f"closed={results['phase_b_closed']}, exited={results['phase_b_exited_early']}"
        )

        return results

    async def _phase_a_execute_pending(self, monitor_id: str) -> int:
        """
        Execute pending opportunities: scan opportunities table, validate, place orders.

        For each pending opportunity:
        1. Re-fetch Kalshi market (confirm still open, still has edge)
        2. Re-read latest signal (confirm signal still actionable)
        3. Calculate position size (based on bankroll, max position)
        4. Place order via Kalshi API
        5. Write MacroPosition to positions table
        6. Update opportunity status to "executed"
        7. SNS alert: "MACRO POSITION OPENED: ..."

        Returns count of successfully executed positions.
        """
        logger.info(f"[{monitor_id}] Phase A: Executing pending opportunities...")

        if not self.kalshi_client:
            logger.error("[Phase A] Kalshi client not available")
            return 0

        # Scan for pending opportunities
        try:
            response = self.opportunities_table.scan(
                FilterExpression="[#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pending": "pending"},
            )
            pending_opps = response.get("Items", [])
        except Exception as e:
            logger.error(f"[Phase A] Failed to scan pending opportunities: {e}")
            return 0

        logger.info(f"[{monitor_id}] Found {len(pending_opps)} pending opportunities")

        executed_count = 0

        for opp_item in pending_opps:
            opp = MacroOpportunity(**opp_item)

            # Check if opportunity has expired
            if datetime.fromisoformat(opp.expires_at.rstrip("Z")) < datetime.utcnow():
                logger.info(
                    f"[Phase A] Opportunity {opp.opportunity_id} expired; skipping"
                )
                await self._update_opportunity_status(
                    opp.opportunity_id, "expired", "Opportunity expired"
                )
                continue

            # Re-validate opportunity
            try:
                market = await asyncio.to_thread(
                    self.kalshi_client.get_market_details, opp.market_ticker
                )

                if not market or market.get("status") != "open":
                    logger.info(
                        f"[Phase A] Market {opp.market_ticker} no longer open; skipping"
                    )
                    await self._update_opportunity_status(
                        opp.opportunity_id, "skipped", "Market closed"
                    )
                    continue

                # Re-check signal
                signal = self.signal_reader.get_latest_signal()
                if (
                    not signal
                    or not self.signal_reader.is_signal_actionable(signal, opp.signal_key)
                ):
                    logger.info(
                        f"[Phase A] Signal {opp.signal_key} no longer actionable; skipping"
                    )
                    await self._update_opportunity_status(
                        opp.opportunity_id, "skipped", "Signal no longer actionable"
                    )
                    continue

                # NEW: Calculate position size (conservative sizing)
                position_size_usd = await self._calculate_position_size(
                    opp.market_ticker
                )
                contracts = max(
                    1,
                    int(position_size_usd / max(0.01, opp.market_yes_price)),
                )

                # Place order
                price = market.get("yes_ask") if opp.direction == "yes" else market.get("yes_bid")
                order_result = await asyncio.to_thread(
                    self.kalshi_client.place_order,
                    market_ticker=opp.market_ticker,
                    direction=opp.direction,
                    contracts=contracts,
                    price=price,
                )

                if not order_result or not order_result.get("order_id"):
                    logger.error(
                        f"[Phase A] Failed to place order for {opp.market_ticker}: {order_result}"
                    )
                    continue

                order_id = order_result["order_id"]

                # Create MacroPosition
                position = MacroPosition.create(
                    market_ticker=opp.market_ticker,
                    series=opp.series,
                    event_description=opp.event_description,
                    signal_key=opp.signal_key,
                    entry_signal_value=opp.signal_value,
                    entry_confidence=opp.signal_confidence,
                    entry_summary=opp.signal_summary,
                    direction=opp.direction,
                    entry_price=price,
                    contracts=contracts,
                    position_size_usd=position_size_usd,
                    order_id=order_id,
                    resolution_date=opp.resolution_date,
                )

                # Write position to DynamoDB
                self.positions_table.put_item(Item=position.to_dynamodb_item())

                # Update opportunity as executed
                await self._update_opportunity_status(
                    opp.opportunity_id,
                    "executed",
                    None,
                    executed_position_id=position.position_id,
                )

                # SNS alert
                alert = (
                    f"MACRO POSITION OPENED\n"
                    f"  Market: {opp.event_description}\n"
                    f"  Direction: Buy {opp.direction.upper()}\n"
                    f"  Entry price: {price:.3f}\n"
                    f"  Contracts: {contracts}\n"
                    f"  Position size: ${position_size_usd:.2f}\n"
                    f"  Signal: {opp.signal_key}={opp.signal_value:+.2f} (confidence: {opp.signal_confidence:.0%})\n"
                    f"  Edge: {opp.edge:+.3f}\n"
                    f"  Resolves: {opp.resolution_date}"
                )
                await self._send_sns_alert(alert)

                executed_count += 1
                logger.info(
                    f"[Phase A] Executed: {opp.market_ticker} (order={order_id})"
                )

            except Exception as e:
                logger.error(
                    f"[Phase A] Error executing opportunity {opp.opportunity_id}: {e}"
                )

        return executed_count

    async def _phase_b_monitor_open(self, monitor_id: str) -> tuple[int, int]:
        """
        Monitor open positions: check for resolution, signal reversal, early exit.

        For each open position:
        1. Check if market has resolved → record outcome, close position
        2. If not resolved:
           a. Re-read latest signal for this signal_key
           b. If signal has reversed past SIGNAL_REVERSAL_EXIT_THRESHOLD → exit early
           c. Check if position has been held > POSITION_HOLD_MAX_DAYS → exit
           d. Otherwise hold to resolution
        3. Update position in DynamoDB with status/outcome/P&L
        4. SNS alert on close

        Returns (closed_count, exited_early_count)
        """
        logger.info(f"[{monitor_id}] Phase B: Monitoring open positions...")

        try:
            response = self.positions_table.scan(
                FilterExpression="#status = :open",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":open": "open"},
            )
            open_positions = response.get("Items", [])
        except Exception as e:
            logger.error(f"[Phase B] Failed to scan open positions: {e}")
            return 0, 0

        logger.info(f"[{monitor_id}] Found {len(open_positions)} open positions")

        closed_count = 0
        exited_early = 0

        for pos_item in open_positions:
            position = MacroPosition(**pos_item)

            try:
                # Fetch current market state
                market = await asyncio.to_thread(
                    self.kalshi_client.get_market_details, position.market_ticker
                )

                if not market:
                    logger.warning(f"[Phase B] Could not fetch market {position.market_ticker}")
                    continue

                # Check if market has resolved
                if market.get("status") == "resolved":
                    outcome_value = market.get("outcome", None)
                    if outcome_value == position.direction:
                        # We won
                        pnl = (1.0 - position.entry_price) * position.contracts
                        position.close(pnl, "won", "resolved_win")
                        await self._send_sns_alert(
                            f"MACRO POSITION WON\n"
                            f"  {position.event_description}\n"
                            f"  Direction: {position.direction}\n"
                            f"  P&L: +${pnl:.2f}"
                        )
                    else:
                        # We lost
                        pnl = -position.entry_price * position.contracts
                        position.close(pnl, "lost", "resolved_loss")
                        await self._send_sns_alert(
                            f"MACRO POSITION LOST\n"
                            f"  {position.event_description}\n"
                            f"  Direction: {position.direction}\n"
                            f"  P&L: ${pnl:.2f}"
                        )

                    # Write closed position
                    self.positions_table.put_item(Item=position.to_dynamodb_item())
                    closed_count += 1
                    logger.info(f"[Phase B] Closed: {position.market_ticker} (outcome={outcome_value})")
                    continue

                # Market not resolved; check for early exit conditions

                # 1. Check signal reversal
                signal = self.signal_reader.get_latest_signal()
                if signal and position.signal_key in signal:
                    current_signal = signal[position.signal_key]

                    # Check if signal has reversed beyond threshold
                    signal_moved = current_signal - position.entry_signal_value
                    if signal_moved < strategy.SIGNAL_REVERSAL_EXIT_THRESHOLD:
                        logger.info(
                            f"[Phase B] Signal reversal detected for {position.market_ticker}: "
                            f"{position.entry_signal_value:+.2f} → {current_signal:+.2f} "
                            f"(moved {signal_moved:+.2f})"
                        )

                        # Exit position
                        exit_price = market.get("yes_bid") if position.direction == "yes" else market.get("yes_ask")
                        pnl = (exit_price - position.entry_price) * position.contracts
                        position.close(pnl, "early_exit", "signal_reversal")
                        self.positions_table.put_item(Item=position.to_dynamodb_item())
                        exited_early += 1

                        await self._send_sns_alert(
                            f"MACRO POSITION EXITED (SIGNAL REVERSAL)\n"
                            f"  {position.event_description}\n"
                            f"  Signal moved: {signal_moved:+.2f}\n"
                            f"  P&L: ${pnl:+.2f}"
                        )
                        logger.info(
                            f"[Phase B] Early exit: {position.market_ticker} (signal_reversal)"
                        )
                        continue

                # 2. Check max hold duration
                hold_duration = datetime.utcnow() - datetime.fromisoformat(
                    position.opened_at.rstrip("Z")
                )
                if hold_duration.days > strategy.POSITION_HOLD_MAX_DAYS:
                    logger.info(
                        f"[Phase B] Position {position.market_ticker} held for "
                        f"{hold_duration.days} days; exiting"
                    )

                    exit_price = market.get("yes_bid") if position.direction == "yes" else market.get("yes_ask")
                    pnl = (exit_price - position.entry_price) * position.contracts
                    position.close(pnl, "early_exit", "max_hold_days")
                    self.positions_table.put_item(Item=position.to_dynamodb_item())
                    exited_early += 1

                    await self._send_sns_alert(
                        f"MACRO POSITION EXITED (MAX HOLD DURATION)\n"
                        f"  {position.event_description}\n"
                        f"  Held for: {hold_duration.days} days\n"
                        f"  P&L: ${pnl:+.2f}"
                    )
                    logger.info(
                        f"[Phase B] Early exit: {position.market_ticker} (max_hold_days)"
                    )

            except Exception as e:
                logger.error(f"[Phase B] Error monitoring position {position.position_id}: {e}")

        return closed_count, exited_early

    async def _phase_c_pnl_summary(self, monitor_id: str) -> str:
        """
        Generate daily P&L and position summary.

        Returns formatted string with:
        - All open positions (market, direction, days held, current signal, days to resolution)
        - P&L from closed positions today
        - Net macro portfolio performance
        """
        lines = [
            "MACRO PORTFOLIO SUMMARY",
            f"  Generated: {datetime.utcnow().isoformat()}",
        ]

        # Get all open positions
        try:
            response = self.positions_table.scan(
                FilterExpression="#status = :open",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":open": "open"},
            )
            open_positions = [MacroPosition(**item) for item in response.get("Items", [])]
        except Exception as e:
            logger.error(f"[Phase C] Failed to scan open positions: {e}")
            open_positions = []

        lines.append(f"\n  Open positions: {len(open_positions)}")
        if open_positions:
            signal = self.signal_reader.get_latest_signal()
            for pos in open_positions:
                days_held = (datetime.utcnow() - datetime.fromisoformat(
                    pos.opened_at.rstrip("Z")
                )).days
                days_to_resolution = (
                    datetime.fromisoformat(pos.resolution_date.rstrip("Z")).date()
                    - datetime.utcnow().date()
                ).days
                current_signal = "N/A"
                if signal and pos.signal_key in signal:
                    current_signal = f"{signal[pos.signal_key]:+.2f}"

                lines.append(
                    f"    {pos.market_ticker}: {pos.direction.upper()} @ {pos.entry_price:.3f}, "
                    f"signal={current_signal}, held {days_held}d, resolve in {days_to_resolution}d"
                )

        # Get closed positions from today
        today = datetime.utcnow().date().isoformat()
        try:
            response = self.positions_table.scan(
                FilterExpression="#status = :closed AND #closed_at >= :today",
                ExpressionAttributeNames={"#status": "status", "#closed_at": "closed_at"},
                ExpressionAttributeValues={":closed": "closed", ":today": today},
            )
            closed_today = [MacroPosition(**item) for item in response.get("Items", [])]
        except Exception as e:
            logger.error(f"[Phase C] Failed to scan closed positions: {e}")
            closed_today = []

        daily_pnl = sum(pos.pnl for pos in closed_today)
        lines.append(f"\n  Closed today: {len(closed_today)}")
        if closed_today:
            lines.append(f"    Daily P&L: ${daily_pnl:+.2f}")
            for pos in closed_today:
                lines.append(
                    f"      {pos.market_ticker}: {pos.outcome} (${pos.pnl:+.2f})"
                )

        return "\n".join(lines)

    def _is_pnl_summary_time(self) -> bool:
        """Check if current time is 20:00 ET (8pm). Returns True if within 1 hour window."""
        et = ZoneInfo("America/New_York")
        now_et = datetime.now(et)
        # Summary window: 20:00-21:00 ET
        return now_et.hour == 20

    async def _calculate_position_size(self, market_ticker: str) -> float:
        """
        Calculate position size in USD based on bankroll and constraints.

        Conservative: 25% of max position per market.
        """
        return strategy.MAX_POSITION_PER_MARKET * 0.25

    async def _update_opportunity_status(
        self,
        opportunity_id: str,
        status: str,
        skip_reason: Optional[str],
        executed_position_id: Optional[str] = None,
    ) -> None:
        """Update opportunity status in DynamoDB."""
        try:
            update_expr = "SET #status = :status"
            expr_values = {":status": status}
            expr_names = {"#status": "status"}

            if skip_reason:
                update_expr += ", skip_reason = :skip_reason"
                expr_values[":skip_reason"] = skip_reason

            if executed_position_id:
                update_expr += ", executed_position_id = :pos_id"
                expr_values[":pos_id"] = executed_position_id

            # Note: This requires knowing the partition key and sort key
            # The current schema uses opportunity_id as hash; adjust query if needed
            self.opportunities_table.update_item(
                Key={"opportunity_id": opportunity_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
        except Exception as e:
            logger.error(f"Failed to update opportunity {opportunity_id}: {e}")

    async def _send_sns_alert(self, message: str) -> None:
        """Send alert via SNS."""
        if not self.sns_topic_arn:
            return

        try:
            self.sns_client.publish(
                TopicArn=self.sns_topic_arn,
                Subject="MACRO POSITION UPDATE",
                Message=message,
            )
        except Exception as e:
            logger.error(f"Failed to send SNS alert: {e}")

    async def _send_sns_summary(self, summary: str) -> None:
        """Send summary via SNS."""
        if not self.sns_topic_arn:
            return

        try:
            self.sns_client.publish(
                TopicArn=self.sns_topic_arn,
                Subject="MACRO PORTFOLIO SUMMARY",
                Message=summary,
            )
        except Exception as e:
            logger.error(f"Failed to send SNS summary: {e}")


async def run() -> Dict[str, Any]:
    """
    Main entry point for the macro monitor.

    Called by EventBridge on a 4-hour schedule.
    """
    monitor = MacroMonitor()
    return await monitor.run()
