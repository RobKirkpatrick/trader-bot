"""
Basis arb monitor — executes opportunities and manages dated futures positions.

Runs every 1 hour (EventBridge trigger).

Phase A: Execute pending opportunities from DynamoDB
  - Re-verify basis APR still qualifies
  - Place spot BUY + futures SHORT
  - Record BasisPosition in DynamoDB

Phase B: Monitor open positions
  - Check for early exit (basis compressed below EXIT_BASIS_APR)
  - Check for near-expiry exit (DAYS_BEFORE_EXPIRY_EXIT)
  - On exit: close spot leg only (futures leg auto-settles at expiry)
  - P&L = (entry_basis_usd × contracts) - fees
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

import boto3

from . import strategy
from .coinbase_client import CoinbaseClient, CoinbaseAPIError, CoinbaseAuthError
from .models import BasisPosition

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")


class BasisArbMonitor:
    """
    Monitors basis arb positions and executes pending opportunities.

    Profit is realized at expiry when futures converge to spot. There are no
    intra-period funding payments. The monitor only closes early if the basis
    compresses significantly below the entry level.
    """

    def __init__(
        self,
        coinbase_client: CoinbaseClient,
        opportunities_table_name: str = "funding-rate-opportunities",
        positions_table_name: str = "funding-rate-positions",
        max_position_usd: Optional[float] = None,
        max_pct_balance: Optional[float] = None,
    ):
        self.client = coinbase_client
        self.opportunities_table = dynamodb.Table(opportunities_table_name)
        self.positions_table = dynamodb.Table(positions_table_name)
        self.max_position_usd = max_position_usd or strategy.MAX_POSITION_USD
        self.max_pct_balance = max_pct_balance or strategy.MAX_PCT_BALANCE

    async def run(self, sns_topic_arn: Optional[str] = None) -> dict[str, Any]:
        """Execute full monitor cycle (Phase A + Phase B)."""
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "phase_a_executed": 0,
            "phase_a_errors": [],
            "phase_b_monitored": 0,
            "phase_b_closed": 0,
            "phase_b_errors": [],
            "summary": "",
        }

        logger.info("Starting basis arb monitor...")

        try:
            await self._execute_pending_opportunities(results)
            await self._monitor_open_positions(results)

            results["summary"] = (
                f"Executed {results['phase_a_executed']} opportunities, "
                f"monitored {results['phase_b_monitored']} positions, "
                f"closed {results['phase_b_closed']}"
            )
            logger.info(results["summary"])

            if sns_topic_arn:
                self._publish_alert(results, sns_topic_arn)

        except Exception as e:
            logger.error(f"Monitor failed: {e}", exc_info=True)
            results["summary"] = f"Monitor error: {e}"

        return results

    async def _execute_pending_opportunities(self, results: dict[str, Any]) -> None:
        """Phase A: Convert pending opportunities into live positions."""
        logger.info("Phase A: Executing pending opportunities...")

        try:
            response = self.opportunities_table.scan(
                FilterExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pending": "pending"},
            )
            opportunities = response.get("Items", [])

            for opp in opportunities:
                spot_ticker = opp.get("spot_ticker")
                futures_ticker = opp.get("futures_ticker")
                logger.info(f"Executing opportunity: {spot_ticker} / {futures_ticker}")

                try:
                    # Re-verify basis is still attractive
                    spot_prices = await self.client.get_best_bid_ask(spot_ticker)
                    spot_price = spot_prices["ask"]
                    futures_price = await self.client.get_futures_price(futures_ticker)
                    dte = strategy.days_to_expiry(futures_ticker)

                    if dte is None or dte <= strategy.DAYS_BEFORE_EXPIRY_EXIT:
                        logger.info(f"{futures_ticker}: Too close to expiry ({dte}d), skipping")
                        self._mark_opportunity_stale(opp.get("perp_ticker"), opp.get("scanned_at"))
                        continue

                    if not strategy.is_worth_entering(spot_price, futures_price, dte):
                        basis_apr = strategy.calc_basis_apr(spot_price, futures_price, dte)
                        logger.info(
                            f"{futures_ticker}: Basis {basis_apr*100:.2f}% no longer qualifies, skipping"
                        )
                        self._mark_opportunity_stale(opp.get("perp_ticker"), opp.get("scanned_at"))
                        continue

                    # Get available USD balance
                    usd_balance = await self.client.get_spot_balance("USD")
                    notional_usd = min(
                        self.max_position_usd,
                        usd_balance * self.max_pct_balance,
                    )

                    if notional_usd < 10:
                        logger.warning(f"{spot_ticker}: Insufficient balance (${usd_balance:.2f}), skipping")
                        continue

                    spot_quantity = notional_usd / spot_price
                    basis_usd = (futures_price - spot_price) * spot_quantity

                    # Place spot BUY
                    spot_order_id = await self.client.place_spot_buy(spot_ticker, notional_usd)

                    # Place futures SHORT (short the futures = lock in premium at entry)
                    futures_order_id = await self.client.place_perp_short(futures_ticker, notional_usd)

                    # Wait for fills
                    spot_filled = await self._wait_for_order_fill(spot_order_id)
                    futures_filled = await self._wait_for_order_fill(futures_order_id)

                    if not (spot_filled and futures_filled):
                        logger.error(
                            f"{futures_ticker}: Fill timeout — spot={spot_filled}, futures={futures_filled}"
                        )
                        results["phase_a_errors"].append(
                            f"{futures_ticker}: Incomplete fills"
                        )
                        continue

                    expiry_date = strategy.parse_expiry_date(futures_ticker)
                    position = BasisPosition(
                        position_id=str(uuid.uuid4()),
                        spot_ticker=spot_ticker,
                        futures_ticker=futures_ticker,
                        expiry_date=expiry_date.date().isoformat() if expiry_date else "",
                        days_to_expiry=dte,
                        entry_spot_price=spot_price,
                        entry_futures_price=futures_price,
                        entry_basis_apr=strategy.calc_basis_apr(spot_price, futures_price, dte),
                        notional_usd=notional_usd,
                        spot_quantity=spot_quantity,
                        expected_basis_usd=basis_usd,
                        spot_order_id=spot_order_id,
                        futures_order_id=futures_order_id,
                        status="open",
                        opened_at=datetime.utcnow().isoformat(),
                    )
                    self._write_position(position)
                    self._mark_opportunity_executed(opp.get("perp_ticker"), opp.get("scanned_at"))
                    results["phase_a_executed"] += 1

                    logger.info(
                        f"Position opened: {position.position_id} | "
                        f"{futures_ticker} | basis_apr={position.entry_basis_apr*100:.2f}% | "
                        f"expected_basis=${basis_usd:.2f} | dte={dte}d"
                    )

                    if sns_topic_arn:
                        self._publish_position_opened(position, sns_topic_arn)

                except (CoinbaseAuthError, CoinbaseAPIError) as e:
                    error_msg = f"{futures_ticker}: API error: {e}"
                    logger.error(error_msg)
                    results["phase_a_errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"{futures_ticker}: Execution error: {e}"
                    logger.error(error_msg, exc_info=True)
                    results["phase_a_errors"].append(error_msg)

        except Exception as e:
            logger.error(f"Phase A failed: {e}", exc_info=True)
            results["phase_a_errors"].append(str(e))

    async def _monitor_open_positions(self, results: dict[str, Any]) -> None:
        """Phase B: Check open positions for early exit or near-expiry close."""
        logger.info("Phase B: Monitoring open positions...")

        try:
            response = self.positions_table.scan(
                FilterExpression="#status = :open",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":open": "open"},
            )
            positions = response.get("Items", [])

            for pos_item in positions:
                position = BasisPosition.from_dict(pos_item)
                results["phase_b_monitored"] += 1

                logger.info(f"Monitoring {position.position_id} ({position.futures_ticker})...")

                try:
                    dte = strategy.days_to_expiry(position.futures_ticker)

                    # Near-expiry exit: close spot leg before settlement
                    if strategy.near_expiry(position.futures_ticker):
                        logger.info(
                            f"{position.position_id}: Near expiry ({dte}d), closing spot leg"
                        )
                        await self._close_position(position, "near_expiry")
                        results["phase_b_closed"] += 1
                        continue

                    # Early exit: basis compressed
                    spot_prices = await self.client.get_best_bid_ask(position.spot_ticker)
                    spot_price = spot_prices["mid"]
                    futures_price = await self.client.get_futures_price(position.futures_ticker)
                    current_dte = dte or 1

                    if strategy.is_worth_exiting(spot_price, futures_price, current_dte):
                        current_apr = strategy.calc_basis_apr(spot_price, futures_price, current_dte)
                        logger.info(
                            f"{position.position_id}: Basis compressed to {current_apr*100:.2f}%, exiting"
                        )
                        await self._close_position(position, "basis_compressed")
                        results["phase_b_closed"] += 1
                    else:
                        current_apr = strategy.calc_basis_apr(spot_price, futures_price, current_dte)
                        logger.info(
                            f"{position.position_id}: Holding — basis={current_apr*100:.2f}%, dte={dte}d"
                        )

                except (CoinbaseAuthError, CoinbaseAPIError) as e:
                    error_msg = f"{position.position_id}: API error: {e}"
                    logger.error(error_msg)
                    results["phase_b_errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"{position.position_id}: Monitor error: {e}"
                    logger.error(error_msg, exc_info=True)
                    results["phase_b_errors"].append(error_msg)

        except Exception as e:
            logger.error(f"Phase B failed: {e}", exc_info=True)
            results["phase_b_errors"].append(str(e))

    async def _close_position(self, position: "BasisPosition", exit_reason: str) -> None:
        """
        Close the spot leg. Futures leg settles automatically at expiry.

        P&L = expected_basis_usd (locked in at entry when both legs filled).
        Actual P&L may differ slightly from fees and mid vs fill slippage.
        """
        try:
            position.status = "closing"
            position.last_updated = datetime.utcnow().isoformat()
            self._write_position(position)

            # Close spot leg only
            spot_close_id = await self.client.place_spot_sell(
                position.spot_ticker, position.spot_quantity
            )
            spot_filled = await self._wait_for_order_fill(spot_close_id)

            if not spot_filled:
                logger.error(f"{position.position_id}: Spot close fill timeout")
                position.status = "open"
                position.last_updated = datetime.utcnow().isoformat()
                self._write_position(position)
                return

            position.realized_pnl = position.expected_basis_usd
            position.status = "closed"
            position.closed_at = datetime.utcnow().isoformat()
            position.exit_reason = exit_reason
            position.last_updated = datetime.utcnow().isoformat()
            self._write_position(position)

            logger.info(
                f"Position closed: {position.position_id} | "
                f"exit={exit_reason} | pnl=${position.realized_pnl:.2f}"
            )

        except Exception as e:
            logger.error(f"Close failed for {position.position_id}: {e}", exc_info=True)
            position.status = "open"
            position.last_updated = datetime.utcnow().isoformat()
            self._write_position(position)

    async def _wait_for_order_fill(
        self,
        order_id: str,
        timeout_sec: int = 30,
        poll_interval_sec: float = 2,
    ) -> bool:
        start = datetime.utcnow()
        while (datetime.utcnow() - start).total_seconds() < timeout_sec:
            try:
                status = await self.client.get_order_status(order_id)
                if float(status.get("filled_size", 0)) > 0:
                    logger.info(f"Order {order_id} filled")
                    return True
            except Exception as e:
                logger.warning(f"Error checking order {order_id}: {e}")
            await asyncio.sleep(poll_interval_sec)
        logger.warning(f"Order {order_id} did not fill within {timeout_sec}s")
        return False

    def _write_position(self, position: "BasisPosition") -> None:
        item = position.to_dict()
        item["position_id"] = position.position_id
        self.positions_table.put_item(Item=item)

    def _mark_opportunity_stale(self, perp_ticker: str, scanned_at: Any) -> None:
        try:
            self.opportunities_table.update_item(
                Key={"perp_ticker": perp_ticker, "scanned_at": scanned_at},
                UpdateExpression="SET #status = :stale",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":stale": "stale"},
            )
        except Exception as e:
            logger.warning(f"Failed to mark opportunity stale: {e}")

    def _mark_opportunity_executed(self, perp_ticker: str, scanned_at: Any) -> None:
        try:
            self.opportunities_table.update_item(
                Key={"perp_ticker": perp_ticker, "scanned_at": scanned_at},
                UpdateExpression="SET #status = :executed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":executed": "executed"},
            )
        except Exception as e:
            logger.warning(f"Failed to mark opportunity executed: {e}")

    def _publish_position_opened(self, position: "BasisPosition", sns_topic_arn: str) -> None:
        try:
            message = (
                f"Crypto Basis Arb — Position Opened\n"
                f"====================================\n"
                f"Spot:         {position.spot_ticker}\n"
                f"Futures:      {position.futures_ticker}\n"
                f"Expiry:       {position.expiry_date}  ({position.days_to_expiry}d)\n"
                f"Entry spot:   ${position.entry_spot_price:,.2f}\n"
                f"Entry future: ${position.entry_futures_price:,.2f}\n"
                f"Basis APR:    {position.entry_basis_apr*100:.2f}%\n"
                f"Notional:     ${position.notional_usd:.2f}\n"
                f"Expected P&L: ${position.expected_basis_usd:.2f} at expiry\n"
                f"Position ID:  {position.position_id}"
            )
            sns.publish(
                TopicArn=sns_topic_arn,
                Subject=f"[Crypto Arb] Position Opened: {position.spot_ticker} / {position.futures_ticker}",
                Message=message,
            )
            logger.info(f"Position-opened SNS alert sent for {position.position_id}")
        except Exception as e:
            logger.warning(f"Failed to publish position-opened alert: {e}")

    def _publish_alert(self, results: dict[str, Any], sns_topic_arn: str) -> None:
        try:
            message = (
                f"Basis Arb Monitor Report\n"
                f"========================\n"
                f"{results['summary']}\n\n"
                f"Phase A (Execute): {results['phase_a_executed']} executed, "
                f"{len(results['phase_a_errors'])} errors\n"
                f"Phase B (Monitor): {results['phase_b_monitored']} monitored, "
                f"{results['phase_b_closed']} closed, "
                f"{len(results['phase_b_errors'])} errors\n\n"
                f"Errors:\n"
                f"{json.dumps(results['phase_a_errors'] + results['phase_b_errors'], indent=2)}"
            )
            sns.publish(
                TopicArn=sns_topic_arn,
                Subject="Basis Arb Monitor Report",
                Message=message,
            )
            logger.info("SNS alert published")
        except Exception as e:
            logger.warning(f"Failed to publish SNS alert: {e}")


# Alias for lambda_handlers.py which references FundingRateMonitor by name
FundingRateMonitor = BasisArbMonitor
