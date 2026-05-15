"""
Basis arb scanner — discovers cash-and-carry opportunities in Coinbase dated futures.

Runs every 4 hours (EventBridge trigger). For each pair in FUTURES_PAIRS:
  1. Fetch nearest-expiry futures contract
  2. Fetch spot mid price
  3. Calculate annualized basis APR
  4. If APR > MIN_BASIS_APR and no open position exists, write to DynamoDB
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import boto3

from . import strategy
from .coinbase_client import CoinbaseClient, CoinbaseAPIError, CoinbaseAuthError
from .models import BasisOpportunity

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")


class BasisArbScanner:
    """
    Scans dated futures basis and writes opportunities to DynamoDB.

    For each spot/futures pair in strategy.FUTURES_PAIRS, checks if:
    1. A nearest-expiry futures contract exists
    2. Annualized basis APR > MIN_BASIS_APR
    3. No existing open position for this spot ticker
    If all true, writes a BasisOpportunity for the monitor to execute.
    """

    def __init__(
        self,
        coinbase_client: CoinbaseClient,
        opportunities_table_name: str = "funding-rate-opportunities",
        positions_table_name: str = "funding-rate-positions",
    ):
        self.client = coinbase_client
        self.opportunities_table = dynamodb.Table(opportunities_table_name)
        self.positions_table = dynamodb.Table(positions_table_name)

    async def run(self, sns_topic_arn: Optional[str] = None) -> dict[str, Any]:
        """Execute a full scan cycle."""
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "pairs_checked": 0,
            "opportunities_found": [],
            "existing_positions": [],
            "errors": [],
            "summary": "",
        }

        logger.info("Starting basis arb scan...")

        try:
            for spot_ticker, base_asset in strategy.FUTURES_PAIRS.items():
                results["pairs_checked"] += 1
                try:
                    await self._scan_pair(spot_ticker, base_asset, results)
                except CoinbaseAuthError as e:
                    error_msg = f"Auth error scanning {base_asset}: {e}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                except CoinbaseAPIError as e:
                    error_msg = f"API error scanning {base_asset}: {e}"
                    logger.error(error_msg)
                    results["errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"Unexpected error scanning {base_asset}: {e}"
                    logger.error(error_msg, exc_info=True)
                    results["errors"].append(error_msg)

            results["summary"] = (
                f"Scanned {results['pairs_checked']} pairs, "
                f"found {len(results['opportunities_found'])} opportunities, "
                f"skipped {len(results['existing_positions'])} (positions exist), "
                f"errors: {len(results['errors'])}"
            )
            logger.info(results["summary"])

            if sns_topic_arn:
                self._publish_alert(results, sns_topic_arn)

        except Exception as e:
            logger.error(f"Scanner failed: {e}", exc_info=True)
            results["summary"] = f"Scanner error: {e}"

        return results

    async def _scan_pair(
        self,
        spot_ticker: str,
        base_asset: str,
        results: dict[str, Any],
    ) -> None:
        """Scan a single spot/futures pair for a basis opportunity."""

        # Find nearest-expiry futures contract
        futures_product = await self.client.get_active_futures(base_asset)
        if futures_product is None:
            logger.info(f"{base_asset}: No active futures contracts found")
            return

        futures_ticker = futures_product["product_id"]
        dte = strategy.days_to_expiry(futures_ticker)
        if dte is None or dte <= strategy.DAYS_BEFORE_EXPIRY_EXIT:
            logger.info(
                f"{base_asset}: Nearest contract {futures_ticker} too close to expiry ({dte}d)"
            )
            return

        # Fetch prices
        spot_prices = await self.client.get_best_bid_ask(spot_ticker)
        spot_price = spot_prices["mid"]
        futures_price = await self.client.get_futures_price(futures_ticker)

        basis_apr = strategy.calc_basis_apr(spot_price, futures_price, dte)
        logger.info(
            f"{base_asset}: spot={spot_price:.2f}, futures={futures_price:.2f} "
            f"({futures_ticker}), dte={dte}d, basis_apr={basis_apr*100:.2f}%"
        )

        # Check for existing open position
        existing = self._get_existing_position(spot_ticker)
        if existing:
            logger.info(f"{base_asset}: Existing open position, skipping")
            results["existing_positions"].append({
                "spot_ticker": spot_ticker,
                "position_id": existing.get("position_id"),
                "status": existing.get("status"),
            })
            return

        # Check basis threshold
        if not strategy.is_worth_entering(spot_price, futures_price, dte):
            logger.info(
                f"{base_asset}: Basis {basis_apr*100:.2f}% below MIN "
                f"({strategy.MIN_BASIS_APR*100:.2f}%), skipping"
            )
            return

        expiry_date = strategy.parse_expiry_date(futures_ticker)
        opportunity = BasisOpportunity(
            spot_ticker=spot_ticker,
            futures_ticker=futures_ticker,
            scanned_at=int(datetime.utcnow().timestamp()),
            spot_price=spot_price,
            futures_price=futures_price,
            basis_apr=basis_apr,
            days_to_expiry=dte,
            expiry_date=expiry_date.date().isoformat() if expiry_date else "",
            status="pending",
        )
        self._write_opportunity(opportunity)

        results["opportunities_found"].append({
            "spot_ticker": spot_ticker,
            "futures_ticker": futures_ticker,
            "spot_price": spot_price,
            "futures_price": futures_price,
            "basis_apr": f"{basis_apr*100:.2f}%",
            "days_to_expiry": dte,
        })
        logger.info(f"{base_asset}: Opportunity written (APR={basis_apr*100:.2f}%, dte={dte}d)")

    def _get_existing_position(self, spot_ticker: str) -> Optional[dict]:
        """Check DynamoDB for an open position on this spot ticker."""
        try:
            response = self.positions_table.scan(
                FilterExpression="spot_ticker = :st AND #status IN (:open, :closing)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":st": spot_ticker,
                    ":open": "open",
                    ":closing": "closing",
                },
            )
            items = response.get("Items", [])
            return items[0] if items else None
        except Exception as e:
            logger.warning(f"Error checking existing position: {e}")
            return None

    def _write_opportunity(self, opportunity: "BasisOpportunity") -> None:
        """Write opportunity to DynamoDB (reuses funding-rate-opportunities table)."""
        self.opportunities_table.put_item(Item={
            "perp_ticker": opportunity.spot_ticker,   # hash key (reusing column name)
            "scanned_at": opportunity.scanned_at,
            "spot_ticker": opportunity.spot_ticker,
            "futures_ticker": opportunity.futures_ticker,
            "spot_price": str(opportunity.spot_price),
            "futures_price": str(opportunity.futures_price),
            "basis_apr": str(opportunity.basis_apr),
            "days_to_expiry": opportunity.days_to_expiry,
            "expiry_date": opportunity.expiry_date,
            "status": opportunity.status,
        })

    def _publish_alert(self, results: dict[str, Any], sns_topic_arn: str) -> None:
        try:
            message = (
                f"Basis Arb Scan Complete\n"
                f"=======================\n"
                f"{results['summary']}\n\n"
                f"Opportunities:\n{json.dumps(results['opportunities_found'], indent=2)}\n\n"
                f"Errors:\n{json.dumps(results['errors'], indent=2)}"
            )
            sns.publish(
                TopicArn=sns_topic_arn,
                Subject="Basis Arb Scan Report",
                Message=message,
            )
            logger.info("SNS alert published")
        except Exception as e:
            logger.warning(f"Failed to publish SNS alert: {e}")


# Alias for lambda_handlers.py which instantiates FundingRateScanner by name
FundingRateScanner = BasisArbScanner
