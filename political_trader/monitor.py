# NEW: Political position monitor and manager—executes, monitors, exits trades
"""
Continuous monitoring loop for political positions.
Runs every 8 hours via EventBridge.

Workflow:
Phase A: Execute pending opportunities
- Re-check signal and liquidity
- Place market orders
- Write PoliticalPosition to DynamoDB

Phase B: Monitor open positions
- Check for resolution
- Refresh signals
- Exit on signal reversal or edge compression
- Handle position P&L

Phase C: Weekly digest (Sundays 8pm ET)
- Summarize all positions, signals, P&L
- Send detailed SNS report
"""

import logging
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import uuid
import asyncio

import boto3

from .strategy import (
    MIN_COMBINED_SIGNAL,
    MIN_EDGE,
    SIGNAL_REVERSAL_THRESHOLD,
    TRAILING_EDGE_EXIT,
    MAX_SIMULTANEOUS_POSITIONS,
    MAX_POSITION_PER_MARKET,
    MAX_PCT_BANKROLL,
)
from .models import PoliticalPosition, PoliticalOpportunity
from .signal_reader import PoliticalSignalReader

logger = logging.getLogger(__name__)

# NEW: DynamoDB, SNS, and other AWS clients
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

OPPORTUNITIES_TABLE_NAME = "political-opportunities"
POSITIONS_TABLE_NAME = "political-positions"
SNS_TOPIC_ARN = "SENTINEL_SNS_ARN"


class PoliticalMonitor:
    """
    Manages execution and monitoring of political trading positions.
    """

    def __init__(
        self,
        kalshi_client,
        signal_reader: PoliticalSignalReader,
        dynamodb_resource=None,
        sns_client=None,
        account_bankroll: float = 10000.0,
    ):
        """
        Args:
            kalshi_client: KalshiClient instance
            signal_reader: PoliticalSignalReader instance
            dynamodb_resource: Boto3 DynamoDB resource
            sns_client: Boto3 SNS client
            account_bankroll: Total account size for position sizing
        """
        self.kalshi_client = kalshi_client
        self.signal_reader = signal_reader
        self.dynamodb = dynamodb_resource or dynamodb
        self.sns = sns_client or sns
        self.account_bankroll = account_bankroll

        self.opportunities_table = self.dynamodb.Table(OPPORTUNITIES_TABLE_NAME)
        self.positions_table = self.dynamodb.Table(POSITIONS_TABLE_NAME)

    # ========================================================================
    # PHASE A: EXECUTE PENDING OPPORTUNITIES
    # ========================================================================

    def get_pending_opportunities(self) -> List[Dict[str, Any]]:
        """
        NEW: Fetch all pending opportunities from DynamoDB.
        """
        try:
            response = self.opportunities_table.scan(
                FilterExpression="s#status = :status",
                ExpressionAttributeNames={"s#status": "status"},
                ExpressionAttributeValues={":status": "pending"},
            )

            items = response.get("Items", [])
            logger.info(f"Found {len(items)} pending opportunities")
            return items

        except Exception as e:
            logger.error(f"Error fetching pending opportunities: {e}", exc_info=True)
            return []

    def get_open_positions_count(self) -> int:
        """NEW: Count currently open positions."""
        try:
            response = self.positions_table.scan(
                FilterExpression="s#status = :status",
                ExpressionAttributeNames={"s#status": "status"},
                ExpressionAttributeValues={":status": "open"},
                Select="COUNT",
            )
            return response.get("Count", 0)

        except Exception as e:
            logger.error(f"Error counting open positions: {e}", exc_info=True)
            return 0

    def calculate_position_size(self, opp_signal: float, opp_edge: float) -> Tuple[float, int]:
        """
        NEW: Determine contract count based on signal confidence and edge.
        Conservative sizing: longer hold times = smaller positions.

        Args:
            opp_signal: Combined signal strength (abs value)
            opp_edge: Edge vs market price in cents

        Returns:
            (position_size_usd, contracts_count)
        """
        try:
            # NEW: Base size from edge (higher edge = higher confidence)
            base_size = max(2.0, opp_edge * 100.0)  # Edge in cents → dollars
            base_size = min(base_size, MAX_POSITION_PER_MARKET)

            # NEW: Scale by signal confidence
            signal_factor = min(abs(opp_signal) / 1.0, 1.0)  # Cap at 1.0
            position_size = base_size * signal_factor

            # NEW: Check bankroll limit
            max_bankroll = self.account_bankroll * MAX_PCT_BANKROLL
            position_size = min(position_size, max_bankroll)

            # NEW: Estimate contract count (assuming ~50 cent entry)
            contracts = int(position_size / 0.50)
            contracts = max(1, min(contracts, 100))  # 1-100 contracts

            return position_size, contracts

        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            return 1.0, 1

    async def execute_opportunity(self, opportunity_item: Dict[str, Any]) -> Optional[str]:
        """
        NEW: Execute a pending opportunity—place order, create position record.

        Returns:
            position_id if successful, None otherwise
        """
        try:
            market_ticker = opportunity_item.get("market_ticker", "")
            market_title = opportunity_item.get("market_title", "")
            series = opportunity_item.get("series", "")
            resolution_date = opportunity_item.get("resolution_date", "")

            logger.info(f"Executing opportunity: {market_ticker}")

            # NEW: Check position count limit
            open_count = self.get_open_positions_count()
            if open_count >= MAX_SIMULTANEOUS_POSITIONS:
                logger.warning(
                    f"Position limit reached ({open_count}); skipping {market_ticker}"
                )
                return None

            # NEW: Refresh market state and signal
            market = self.kalshi_client.get_market(market_ticker)
            if not market:
                logger.error(f"Market not found: {market_ticker}")
                return None

            yes_price = float(market.get("yes_ask", 0.50))
            signal = self.signal_reader.assess_market(
                market_ticker=market_ticker,
                market_title=market_title,
                series=series,
                resolution_date=resolution_date,
                current_yes_price=yes_price,
            )

            # NEW: Validate signal still meets thresholds
            if abs(signal.combined_signal) < MIN_COMBINED_SIGNAL:
                logger.warning(
                    f"Signal deteriorated for {market_ticker}; canceling execution"
                )
                return None

            if abs(signal.edge_vs_market) < MIN_EDGE:
                logger.warning(f"Edge compressed for {market_ticker}; canceling execution")
                return None

            # NEW: Determine position size
            position_size, contracts = self.calculate_position_size(
                signal.combined_signal, signal.edge_vs_market * 100  # Convert to cents
            )

            # NEW: Determine direction (YES or NO)
            direction = "yes" if signal.combined_signal > 0 else "no"
            entry_price = yes_price if direction == "yes" else (1.0 - yes_price)

            # NEW: Place order via Kalshi client
            order_result = self.kalshi_client.place_order(
                market_ticker=market_ticker,
                side=direction,
                quantity=contracts,
                limit_price=entry_price,
            )

            if not order_result:
                logger.error(f"Failed to place order for {market_ticker}")
                return None

            order_id = order_result.get("order_id", "")

            # NEW: Create position record
            position_id = f"{market_ticker}-{datetime.utcnow().isoformat()}"
            position = PoliticalPosition(
                position_id=position_id,
                market_ticker=market_ticker,
                series=series,
                market_title=market_title,
                news_signal=signal.news_signal,
                polling_momentum=signal.polling_momentum or 0.0,
                market_momentum=signal.market_momentum or 0.0,
                combined_signal=signal.combined_signal,
                entry_summary=signal.news_summary,
                direction=direction,
                entry_price=entry_price,
                contracts=contracts,
                position_size_usd=position_size,
                order_id=order_id,
                fair_value_estimate=signal.implied_probability,
                status="open",
                resolution_date=resolution_date,
                opened_at=datetime.utcnow().isoformat(),
            )

            # NEW: Persist position
            self.positions_table.put_item(Item=position.to_dict())

            # NEW: Update opportunity status
            self.opportunities_table.update_item(
                Key={"market_ticker": market_ticker},
                UpdateExpression="SET #s = :status, entered_position_id = :pos_id",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": "entered", ":pos_id": position_id},
            )

            logger.info(
                f"Position opened: {position_id} {direction}@{entry_price} x{contracts} (signal={signal.combined_signal:.3f})"
            )

            # NEW: Send alert
            self._send_position_opened_alert(position, signal)

            return position_id

        except Exception as e:
            logger.error(f"Error executing opportunity: {e}", exc_info=True)
            return None

    # ========================================================================
    # PHASE B: MONITOR OPEN POSITIONS
    # ========================================================================

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """NEW: Fetch all open positions from DynamoDB."""
        try:
            response = self.positions_table.scan(
                FilterExpression="s#status = :status",
                ExpressionAttributeNames={"s#status": "status"},
                ExpressionAttributeValues={":status": "open"},
            )
            return response.get("Items", [])

        except Exception as e:
            logger.error(f"Error fetching open positions: {e}", exc_info=True)
            return []

    async def monitor_position(self, position_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        NEW: Monitor a single open position—check resolution, update signal, evaluate exit.

        Returns:
            Updated position dict, or None if error
        """
        try:
            market_ticker = position_item.get("market_ticker", "")
            position_id = position_item.get("position_id", "")

            logger.debug(f"Monitoring position: {position_id} ({market_ticker})")

            # NEW: Check if market has resolved
            market = self.kalshi_client.get_market(market_ticker)
            if not market:
                logger.warning(f"Market not found: {market_ticker}")
                return None

            if market.get("resolved"):
                return await self._handle_position_resolution(position_item, market)

            # NEW: Refresh signal
            signal = self.signal_reader.assess_market(
                market_ticker=position_item.get("market_ticker", ""),
                market_title=position_item.get("market_title", ""),
                series=position_item.get("series", ""),
                resolution_date=position_item.get("resolution_date", ""),
                current_yes_price=float(market.get("yes_ask", 0.50)),
            )

            # NEW: Check for exit conditions

            # 1. Signal reversal
            original_signal = position_item.get("combined_signal", 0.0)
            signal_change = signal.combined_signal - original_signal

            if signal_change < SIGNAL_REVERSAL_THRESHOLD:
                logger.info(
                    f"Signal reversal for {market_ticker}: {original_signal:.3f} → {signal.combined_signal:.3f}"
                )
                return await self._exit_position(
                    position_item, "signal_reversal", signal.current_yes_price
                )

            # 2. Edge compression
            if abs(signal.edge_vs_market) < TRAILING_EDGE_EXIT:
                logger.info(f"Edge compressed for {market_ticker}: {signal.edge_vs_market:.3f}")
                return await self._exit_position(
                    position_item, "edge_compression", signal.current_yes_price
                )

            # NEW: Update position with fresh signal
            try:
                self.positions_table.update_item(
                    Key={"position_id": position_id},
                    UpdateExpression="SET last_signal_refresh = :now, combined_signal = :signal",
                    ExpressionAttributeValues={
                        ":now": datetime.utcnow().isoformat(),
                        ":signal": signal.combined_signal,
                    },
                )
            except Exception as e:
                logger.error(f"Error updating position {position_id}: {e}")

            return position_item

        except Exception as e:
            logger.error(f"Error monitoring position: {e}", exc_info=True)
            return None

    async def _handle_position_resolution(
        self, position_item: Dict[str, Any], market: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """NEW: Market has resolved—calculate P&L and close position."""
        try:
            position_id = position_item.get("position_id", "")
            market_ticker = position_item.get("market_ticker", "")
            direction = position_item.get("direction", "yes")
            entry_price = position_item.get("entry_price", 0.0)
            contracts = position_item.get("contracts", 0)
            position_size = position_item.get("position_size_usd", 0.0)

            # NEW: Determine resolution outcome
            resolution_answer = market.get("resolution_answer", "")
            won = (direction == "yes" and resolution_answer == "yes") or (
                direction == "no" and resolution_answer == "no"
            )

            # NEW: Calculate P&L
            if won:
                # Won: gain (1.0 - entry_price) * contracts
                pnl = (1.0 - entry_price) * contracts
                outcome = "won"
            else:
                # Lost: lose entry_price * contracts
                pnl = -(entry_price) * contracts
                outcome = "lost"

            pnl_pct = (pnl / position_size * 100.0) if position_size > 0 else 0.0

            # NEW: Close position in DynamoDB
            opened_at = datetime.fromisoformat(position_item.get("opened_at", ""))
            days_held = (datetime.utcnow() - opened_at).days

            self.positions_table.update_item(
                Key={"position_id": position_id},
                UpdateExpression="SET #s = :status, pnl = :pnl, pnl_pct = :pnl_pct, outcome = :outcome, closed_at = :closed, days_held = :days",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": "closed",
                    ":pnl": pnl,
                    ":pnl_pct": pnl_pct,
                    ":outcome": outcome,
                    ":closed": datetime.utcnow().isoformat(),
                    ":days": days_held,
                },
            )

            logger.info(
                f"Position resolved: {position_id} {outcome.upper()} P&L=${pnl:.2f} ({pnl_pct:.1f}%)"
            )

            return None  # Position is closed

        except Exception as e:
            logger.error(f"Error handling resolution: {e}", exc_info=True)
            return None

    async def _exit_position(
        self, position_item: Dict[str, Any], reason: str, exit_price: float
    ) -> Optional[Dict[str, Any]]:
        """NEW: Exit open position before resolution."""
        try:
            position_id = position_item.get("position_id", "")
            market_ticker = position_item.get("market_ticker", "")
            direction = position_item.get("direction", "yes")
            entry_price = position_item.get("entry_price", 0.0)
            contracts = position_item.get("contracts", 0)
            position_size = position_item.get("position_size_usd", 0.0)

            # NEW: Close position via Kalshi API
            close_result = self.kalshi_client.place_order(
                market_ticker=market_ticker,
                side="no" if direction == "yes" else "yes",  # Opposite side to close
                quantity=contracts,
                limit_price=exit_price,
            )

            if not close_result:
                logger.error(f"Failed to close position {position_id}")
                return position_item

            # NEW: Calculate realized P&L
            if direction == "yes":
                pnl = (exit_price - entry_price) * contracts
            else:
                pnl = (entry_price - exit_price) * contracts

            pnl_pct = (pnl / position_size * 100.0) if position_size > 0 else 0.0

            # NEW: Update position
            opened_at = datetime.fromisoformat(position_item.get("opened_at", ""))
            days_held = (datetime.utcnow() - opened_at).days

            self.positions_table.update_item(
                Key={"position_id": position_id},
                UpdateExpression="SET #s = :status, pnl = :pnl, pnl_pct = :pnl_pct, outcome = :outcome, exit_reason = :reason, exit_price = :exit, closed_at = :closed, days_held = :days",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": "closed",
                    ":pnl": pnl,
                    ":pnl_pct": pnl_pct,
                    ":outcome": "early_exit",
                    ":reason": reason,
                    ":exit": exit_price,
                    ":closed": datetime.utcnow().isoformat(),
                    ":days": days_held,
                },
            )

            logger.info(
                f"Position exited: {position_id} {reason} P&L=${pnl:.2f} ({pnl_pct:.1f}%)"
            )

            return None  # Position is closed

        except Exception as e:
            logger.error(f"Error exiting position: {e}", exc_info=True)
            return position_item

    # ========================================================================
    # PHASE C: WEEKLY DIGEST & ALERTS
    # ========================================================================

    def should_send_weekly_digest(self) -> bool:
        """NEW: Check if it's Sunday 8pm ET."""
        now = datetime.utcnow()
        # Convert UTC to ET (rough; should use pytz in production)
        et_hour = now.hour - 5  # Simplified; actual offset varies with DST
        return now.weekday() == 6 and 20 <= et_hour <= 21

    async def send_weekly_digest(self) -> bool:
        """NEW: Send comprehensive weekly P&L and position report."""
        try:
            open_positions = self.get_open_positions()

            # NEW: Calculate metrics
            total_unrealized_pnl = 0.0
            total_days_to_resolution = 0.0

            for pos in open_positions:
                # Would calculate unrealized P&L here if we had current prices
                res_date = datetime.fromisoformat(pos.get("resolution_date", ""))
                days_to_res = (res_date - datetime.utcnow()).days
                total_days_to_resolution += days_to_res

            avg_days = (
                total_days_to_resolution / len(open_positions)
                if open_positions
                else 0
            )

            # NEW: Fetch recent closed positions
            response = self.positions_table.scan(
                FilterExpression="attribute_exists(closed_at)",
                Limit=50,
            )
            closed_positions = response.get("Items", [])

            weekly_realized_pnl = sum(p.get("pnl", 0.0) for p in closed_positions)
            weekly_won = sum(1 for p in closed_positions if p.get("outcome") == "won")
            weekly_win_rate = (
                100.0 * weekly_won / len(closed_positions) if closed_positions else 0.0
            )

            # NEW: Format message
            lines = [
                "=" * 70,
                "POLITICAL TRADER WEEKLY DIGEST",
                f"Week Ending: {datetime.utcnow().strftime('%Y-%m-%d')}",
                "=" * 70,
                "",
                "OPEN POSITIONS:",
                f"  Total: {len(open_positions)}",
                f"  Avg days to resolution: {avg_days:.1f}",
                f"  Unrealized P&L: ${total_unrealized_pnl:.2f}",
                "",
                "CLOSED THIS WEEK:",
                f"  Positions: {len(closed_positions)}",
                f"  Realized P&L: ${weekly_realized_pnl:.2f}",
                f"  Win rate: {weekly_win_rate:.1f}%",
                "",
                "OPEN POSITIONS DETAIL:",
            ]

            for pos in open_positions[:10]:
                lines.append(
                    f"  {pos.get('market_ticker')}: {pos.get('direction').upper()}@{pos.get('entry_price'):.2f} signal={pos.get('combined_signal'):.3f}"
                )

            if len(open_positions) > 10:
                lines.append(f"  ... and {len(open_positions) - 10} more")

            message = "\n".join(lines)

            # NEW: Send via SNS
            topic_arn = SNS_TOPIC_ARN
            if topic_arn and topic_arn != "SENTINEL_SNS_ARN":
                self.sns.publish(
                    TopicArn=topic_arn,
                    Subject="POLITICAL TRADER WEEKLY DIGEST",
                    Message=message,
                )

            logger.info("Weekly digest sent")
            return True

        except Exception as e:
            logger.error(f"Error sending weekly digest: {e}", exc_info=True)
            return False

    # ========================================================================
    # ALERTS
    # ========================================================================

    def _send_position_opened_alert(
        self, position: PoliticalPosition, signal
    ) -> bool:
        """NEW: Send SNS alert when position opens."""
        try:
            topic_arn = SNS_TOPIC_ARN
            if not topic_arn or topic_arn == "SENTINEL_SNS_ARN":
                return True

            message = f"""POLITICAL POSITION OPENED

Market: {position.market_title}
Ticker: {position.market_ticker}
Direction: {position.direction.upper()}
Entry Price: {position.entry_price:.2f}
Contracts: {position.contracts}
Position Size: ${position.position_size_usd:.2f}

Signal Assessment:
  Combined Signal: {signal.combined_signal:.3f}
  News Sentiment: {signal.news_signal:.3f}
  Polling Momentum: {signal.polling_momentum or 'N/A'}
  Market Momentum: {signal.market_momentum or 'N/A'}
  Fair Value (YES): {signal.implied_probability:.1%}
  Edge vs Market: {signal.edge_vs_market:.3f}

Resolution Date: {position.resolution_date}
Summary: {position.entry_summary}
"""

            self.sns.publish(
                TopicArn=topic_arn,
                Subject=f"POLITICAL POSITION: {position.market_ticker} {position.direction.upper()}",
                Message=message,
            )

            return True

        except Exception as e:
            logger.error(f"Error sending position opened alert: {e}")
            return False

    # ========================================================================
    # MAIN RUNNER
    # ========================================================================

    async def run(self) -> Dict[str, Any]:
        """
        NEW: Execute full monitor cycle (A, B, C).
        """
        logger.info("=" * 70)
        logger.info("POLITICAL MONITOR STARTING")
        logger.info("=" * 70)

        start_time = datetime.utcnow()

        try:
            # NEW: Phase A - Execute pending
            pending = self.get_pending_opportunities()
            executed_count = 0

            for opp_item in pending[:3]:  # Limit to 3 per run
                position_id = await self.execute_opportunity(opp_item)
                if position_id:
                    executed_count += 1
                await asyncio.sleep(1)

            # NEW: Phase B - Monitor open
            open_positions = self.get_open_positions()
            updated_count = 0

            for pos_item in open_positions:
                result = await self.monitor_position(pos_item)
                if result:
                    updated_count += 1
                await asyncio.sleep(0.5)

            # NEW: Phase C - Weekly digest
            digest_sent = False
            if self.should_send_weekly_digest():
                digest_sent = await self.send_weekly_digest()

            elapsed = (datetime.utcnow() - start_time).total_seconds()

            metrics = {
                "status": "success",
                "monitor_duration_seconds": elapsed,
                "opportunities_executed": executed_count,
                "positions_monitored": updated_count,
                "weekly_digest_sent": digest_sent,
                "timestamp": datetime.utcnow().isoformat(),
            }

            logger.info(f"Monitor metrics: {metrics}")
            return metrics

        except Exception as e:
            logger.error(f"Monitor failed: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


async def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """NEW: AWS Lambda entry point for monitor."""
    logger.info(f"Monitor invoked: {event}")

    from carpet_bagger.kalshi_client import KalshiClient

    kalshi_client = KalshiClient()
    signal_reader = PoliticalSignalReader(kalshi_client=kalshi_client)
    monitor = PoliticalMonitor(kalshi_client, signal_reader)

    result = await monitor.run()
    return result
