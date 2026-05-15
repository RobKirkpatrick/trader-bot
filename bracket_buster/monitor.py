"""
bracket_buster/monitor.py

Monitor module: executes found opportunities and manages open positions.

Runs on a timer (5-minute intervals) with two main phases:

Phase A: New Opportunities → Execute
    For each new opportunity in "bracket-buster-opportunities" table:
    - Pure arb: place both legs simultaneously (YES on long, NO on short)
    - Soft arb: place single YES or NO leg
    - Store position in DynamoDB with status="open"

Phase B: Open Positions → Monitor & Exit
    For each open position in "bracket-buster-positions" table:
    - Check if either market has settled
    - Calculate current P&L (mark-to-market)
    - For pure arbs on settlement: should always be profitable
    - For soft arbs: check profit target and stop loss
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os

logger = logging.getLogger(__name__)


async def run(
    kalshi_client,
    dynamodb_client,
    sns_client,
    test_mode: bool = False,
) -> Dict:
    """
    Main monitor execution.

    Args:
        kalshi_client: KalshiClient instance
        dynamodb_client: boto3 DynamoDB resource
        sns_client: boto3 SNS client
        test_mode: If True, don't place real orders

    Returns:
        Dict with execution results
    """
    start_time = datetime.utcnow()
    logger.info("Monitor run started")

    try:
        # Phase A: Execute new opportunities
        logger.info("Phase A: Executing new opportunities...")
        num_executed = await _phase_a_execute_opportunities(
            kalshi_client, dynamodb_client, sns_client, test_mode
        )
        logger.info(f"Executed {num_executed} new opportunities")

        # Phase B: Monitor and manage open positions
        logger.info("Phase B: Monitoring open positions...")
        num_monitored = await _phase_b_monitor_positions(
            kalshi_client, dynamodb_client, sns_client, test_mode
        )
        logger.info(f"Monitored {num_monitored} open positions")

        # Phase C: Reconciliation
        logger.info("Phase C: Reconciliation...")
        num_orphaned = await _phase_c_reconcile(
            kalshi_client, dynamodb_client, sns_client
        )
        if num_orphaned > 0:
            logger.warning(f"Found {num_orphaned} orphaned positions")

        end_time = datetime.utcnow()
        elapsed = (end_time - start_time).total_seconds()

        logger.info(f"Monitor run completed in {elapsed:.1f}s")

        return {
            "status": "success",
            "opportunities_executed": num_executed,
            "positions_monitored": num_monitored,
            "orphaned_positions": num_orphaned,
            "elapsed_seconds": elapsed,
            "timestamp": start_time.isoformat(),
        }

    except Exception as e:
        logger.error(f"Monitor run failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error_message": str(e),
            "timestamp": start_time.isoformat(),
        }


# ============================================================================
# PHASE A: EXECUTE NEW OPPORTUNITIES
# ============================================================================


async def _phase_a_execute_opportunities(
    kalshi_client,
    dynamodb_client,
    sns_client,
    test_mode: bool,
) -> int:
    """
    Execute new opportunities from "bracket-buster-opportunities" table.

    Reads opportunities with status="new", places orders, creates positions.

    Returns:
        Number of opportunities executed
    """
    opportunities_table = dynamodb_client.Table("bracket-buster-opportunities")
    positions_table = dynamodb_client.Table("bracket-buster-positions")

    # Query for new opportunities
    try:
        response = opportunities_table.query(
            IndexName="status-index",
            KeyConditionExpression="status = :s",
            ExpressionAttributeValues={":s": "new"},
            Limit=10,  # Don't execute all at once
        )
        opportunities = response.get("Items", [])
        logger.info(f"Found {len(opportunities)} new opportunities to execute")
    except Exception as e:
        logger.error(f"Failed to query opportunities: {e}")
        return 0

    num_executed = 0

    for opp_dict in opportunities:
        try:
            # Reconstruct opportunity object
            from .models import ArbitrageOpportunity

            opp = ArbitrageOpportunity(**opp_dict)

            logger.info(f"Executing opportunity: {opp.opportunity_id}")

            if opp.arb_type == "pure_arb":
                position = await _execute_pure_arb(
                    kalshi_client, opp, test_mode
                )
            else:
                position = await _execute_soft_arb(
                    kalshi_client, opp, test_mode
                )

            if position:
                # Write position to DynamoDB
                if not test_mode:
                    positions_table.put_item(Item=position.to_dict())
                    logger.info(f"Stored position {position.position_id}")

                # Mark opportunity as executed
                opportunities_table.update_item(
                    Key={"opportunity_id": opp.opportunity_id},
                    UpdateExpression="SET #s = :status, linked_position_id = :pos_id",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": "executed",
                        ":pos_id": position.position_id,
                    },
                )

                num_executed += 1

                # Send SNS alert
                if not test_mode:
                    await _send_execution_alert(sns_client, opp, position)

        except Exception as e:
            logger.error(f"Failed to execute opportunity {opp_dict.get('opportunity_id')}: {e}")
            # Mark as failed
            try:
                opportunities_table.update_item(
                    Key={"opportunity_id": opp_dict.get("opportunity_id")},
                    UpdateExpression="SET #s = :status",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":status": "failed"},
                )
            except:
                pass
            continue

    return num_executed


async def _execute_pure_arb(kalshi_client, opp, test_mode: bool):
    """
    Execute pure arbitrage: place both YES and NO orders simultaneously.

    For pure arb:
    - Long leg: BUY YES on underpriced market
    - Short leg: BUY NO on overpriced market

    Both orders placed as limit orders for best execution.

    Returns:
        BracketPosition object if successful, None if failed
    """
    from .models import BracketPosition
    from . import strategy

    logger.info(f"Executing pure arb: {opp.team_name} ({opp.long_ticker} vs {opp.short_ticker})")

    try:
        # Place long leg: BUY YES
        long_order_id = None
        if not test_mode:
            long_order_id = await _place_buy_yes_order(
                kalshi_client,
                ticker=opp.long_ticker,
                price=opp.long_yes_ask,
                contracts=opp.suggested_contracts,
            )
            logger.info(f"Long order placed: {long_order_id}")

        # Place short leg: BUY NO
        # Note: On Kalshi API, buying NO means placing a BUY order on NO side
        # This requires a method like: place_no_buy(ticker, no_price, contracts)
        # The no_price = 1 - yes_ask
        short_order_id = None
        if not test_mode:
            short_order_id = await _place_buy_no_order(
                kalshi_client,
                ticker=opp.short_ticker,
                no_price=1.0 - opp.short_yes_ask,
                contracts=opp.suggested_contracts,
            )
            logger.info(f"Short order placed: {short_order_id}")

        # Create position
        position = BracketPosition(
            arb_type="pure_arb",
            team_name=opp.team_name,
            sport=opp.sport,
            long_ticker=opp.long_ticker,
            short_ticker=opp.short_ticker,
            long_tier=opp.long_tier,
            short_tier=opp.short_tier,
            long_entry_price=opp.long_yes_ask,
            short_entry_price=1.0 - opp.short_yes_ask,
            long_contracts=opp.suggested_contracts,
            short_contracts=opp.suggested_contracts,
            long_order_id=long_order_id or "test_long",
            short_order_id=short_order_id or "test_short",
            status="open",
            guaranteed_profit=opp.guaranteed_profit_per_unit * opp.suggested_contracts,
            guaranteed_profit_pct=opp.expected_return_pct,
            long_cost_basis=opp.suggested_size_usd * 0.5,
            short_cost_basis=opp.suggested_size_usd * 0.5,
            notes=opp.notes,
        )

        logger.info(
            f"Pure arb position created: {position.position_id} "
            f"(guaranteed profit: ${position.guaranteed_profit:.2f})"
        )
        return position

    except Exception as e:
        logger.error(f"Failed to execute pure arb: {e}", exc_info=True)
        return None


async def _execute_soft_arb(kalshi_client, opp, test_mode: bool):
    """
    Execute soft arbitrage: place single-leg position.

    For soft arb:
    - If underpriced on YES side: BUY YES
    - If overpriced on NO side: BUY NO

    Returns:
        BracketPosition object if successful, None if failed
    """
    from .models import BracketPosition
    from . import strategy

    logger.info(
        f"Executing soft arb: {opp.team_name} {opp.long_tier} "
        f"(side: {opp.mispriced_side})"
    )

    try:
        order_id = None
        if opp.mispriced_side == "yes":
            # Underpriced YES: BUY YES
            if not test_mode:
                order_id = await _place_buy_yes_order(
                    kalshi_client,
                    ticker=opp.mispriced_ticker,
                    price=opp.current_price,
                    contracts=opp.suggested_contracts,
                )
                logger.info(f"Soft arb YES order placed: {order_id}")

            position = BracketPosition(
                arb_type="soft_arb",
                team_name=opp.team_name,
                sport=opp.sport,
                long_ticker=opp.mispriced_ticker,
                short_ticker=None,
                long_tier=opp.long_tier,
                short_tier=None,
                long_entry_price=opp.current_price,
                short_entry_price=0.0,
                long_contracts=opp.suggested_contracts,
                short_contracts=0,
                long_order_id=order_id or "test_order",
                short_order_id=None,
                status="open",
                soft_arb_side="yes",
                soft_arb_exit_price_target=min(opp.current_price + strategy.CONVERGENCE_PROFIT_TARGET, 1.0),
                soft_arb_stop_loss_price=strategy.SOFT_ARB_STOP_LOSS,
                long_cost_basis=opp.suggested_size_usd,
                short_cost_basis=0.0,
                notes=f"{opp.notes} | Fair value: {opp.fair_value_estimate:.2%}",
            )

        else:
            # Overpriced NO: BUY NO
            no_price = 1.0 - opp.current_price
            if not test_mode:
                order_id = await _place_buy_no_order(
                    kalshi_client,
                    ticker=opp.mispriced_ticker,
                    no_price=no_price,
                    contracts=opp.suggested_contracts,
                )
                logger.info(f"Soft arb NO order placed: {order_id}")

            position = BracketPosition(
                arb_type="soft_arb",
                team_name=opp.team_name,
                sport=opp.sport,
                long_ticker=opp.mispriced_ticker,
                short_ticker=None,
                long_tier=opp.long_tier,
                short_tier=None,
                long_entry_price=no_price,
                short_entry_price=0.0,
                long_contracts=opp.suggested_contracts,
                short_contracts=0,
                long_order_id=order_id or "test_order",
                short_order_id=None,
                status="open",
                soft_arb_side="no",
                soft_arb_exit_price_target=max(no_price - strategy.CONVERGENCE_PROFIT_TARGET, 0.0),
                soft_arb_stop_loss_price=strategy.SOFT_ARB_STOP_LOSS,
                long_cost_basis=opp.suggested_size_usd,
                short_cost_basis=0.0,
                notes=f"{opp.notes} | Fair value: {opp.fair_value_estimate:.2%}",
            )

        logger.info(
            f"Soft arb position created: {position.position_id} "
            f"(expected return: {opp.expected_return_pct:.1%})"
        )
        return position

    except Exception as e:
        logger.error(f"Failed to execute soft arb: {e}", exc_info=True)
        return None


# ============================================================================
# PHASE B: MONITOR OPEN POSITIONS
# ============================================================================


async def _phase_b_monitor_positions(
    kalshi_client,
    dynamodb_client,
    sns_client,
    test_mode: bool,
) -> int:
    """
    Monitor all open positions for:
    - Market settlement (close other leg)
    - Soft arb: profit target / stop loss / time limit

    Returns:
        Number of positions monitored
    """
    positions_table = dynamodb_client.Table("bracket-buster-positions")

    # Query for open positions
    try:
        response = positions_table.query(
            IndexName="status-index",
            KeyConditionExpression="status = :s",
            ExpressionAttributeValues={":s": "open"},
        )
        positions_list = response.get("Items", [])
        logger.info(f"Found {len(positions_list)} open positions")
    except Exception as e:
        logger.error(f"Failed to query positions: {e}")
        return 0

    num_monitored = 0

    for pos_dict in positions_list:
        try:
            from .models import BracketPosition

            position = BracketPosition(**pos_dict)

            # Fetch latest market prices
            long_market = await _fetch_market(kalshi_client, position.long_ticker)
            short_market = None
            if position.short_ticker:
                short_market = await _fetch_market(kalshi_client, position.short_ticker)

            # Update position P&L
            long_price = long_market.get("yes_ask", position.long_entry_price)
            short_price = (
                short_market.get("no_ask", 1.0) if short_market else 0.0
            )
            position.mark_to_market(long_price, short_price)

            # Check for settlement
            long_settled = long_market.get("status") == "settled"
            short_settled = short_market and short_market.get("status") == "settled"

            if long_settled or short_settled:
                logger.info(f"Position {position.position_id}: market settled")
                position.status = "closing"
                await _close_position(kalshi_client, position, test_mode)

            elif position.is_soft_arb():
                # Check soft arb exit conditions
                await _check_soft_arb_exit(position, kalshi_client, test_mode)

            # Update position in DynamoDB
            if not test_mode:
                positions_table.put_item(Item=position.to_dict())

            num_monitored += 1

        except Exception as e:
            logger.error(f"Failed to monitor position {pos_dict.get('position_id')}: {e}")
            continue

    return num_monitored


async def _check_soft_arb_exit(position, kalshi_client, test_mode: bool) -> None:
    """
    Check soft arb position for profit target or stop loss.

    Modifies position.status if exit is triggered.
    """
    from . import strategy

    if not position.is_soft_arb():
        return

    current_price = position.long_current_price if position.soft_arb_side == "yes" else position.long_current_price

    # Check profit target
    if position.soft_arb_side == "yes":
        # Bought YES, want price to go down
        if current_price <= position.soft_arb_exit_price_target:
            logger.info(f"Soft arb {position.position_id}: profit target reached")
            position.status = "closing"
            await _close_position(kalshi_client, position, test_mode)
            return

        # Check stop loss
        if current_price > position.long_entry_price * 1.5:  # Arbitrary: 50% loss
            logger.info(f"Soft arb {position.position_id}: stop loss triggered")
            position.status = "closing"
            await _close_position(kalshi_client, position, test_mode)
            return

    else:
        # Bought NO (1 - YES), want price to go up
        if current_price >= position.soft_arb_exit_price_target:
            logger.info(f"Soft arb {position.position_id}: profit target reached")
            position.status = "closing"
            await _close_position(kalshi_client, position, test_mode)
            return

        # Check stop loss
        if current_price < position.long_entry_price * 0.5:  # 50% loss
            logger.info(f"Soft arb {position.position_id}: stop loss triggered")
            position.status = "closing"
            await _close_position(kalshi_client, position, test_mode)
            return

    # Check time limit
    if position.is_soft_arb():
        age = datetime.utcnow() - datetime.fromisoformat(position.opened_at)
        if age > timedelta(hours=strategy.MAX_HOLD_TIME_SOFT_ARB_HOURS):
            logger.info(f"Soft arb {position.position_id}: max hold time exceeded")
            position.status = "closing"
            await _close_position(kalshi_client, position, test_mode)


async def _close_position(kalshi_client, position, test_mode: bool) -> None:
    """
    Close out a position by selling all held contracts.

    For pure arb: sell remaining leg (other already settled)
    For soft arb: sell the single leg position
    """
    logger.info(f"Closing position {position.position_id}")

    try:
        if position.long_contracts > 0 and position.long_order_id:
            if not test_mode:
                # Place SELL order for long contracts
                await _place_sell_yes_order(
                    kalshi_client,
                    ticker=position.long_ticker,
                    contracts=position.long_contracts,
                )

        if position.short_contracts > 0 and position.short_order_id:
            if not test_mode:
                # Place SELL order for short contracts (sell NO = place SELL on NO side)
                await _place_sell_no_order(
                    kalshi_client,
                    ticker=position.short_ticker,
                    contracts=position.short_contracts,
                )

        position.status = "closed"
        position.closed_at = datetime.utcnow().isoformat()
        logger.info(f"Position closed: {position.position_id}")

    except Exception as e:
        logger.error(f"Failed to close position: {e}")


# ============================================================================
# PHASE C: RECONCILIATION
# ============================================================================


async def _phase_c_reconcile(kalshi_client, dynamodb_client, sns_client) -> int:
    """
    Reconcile DynamoDB positions with Kalshi portfolio.

    Find any positions in our table that aren't in Kalshi portfolio (orphaned).
    Alert on these.

    Returns:
        Number of orphaned positions found
    """
    logger.info("Reconciling positions with Kalshi portfolio...")

    try:
        # Get all orders from Kalshi
        kalshi_orders = await kalshi_client.get_orders()
        kalshi_order_ids = {o.get("order_id") for o in kalshi_orders}

        # Get all positions from DynamoDB
        positions_table = dynamodb_client.Table("bracket-buster-positions")
        response = positions_table.scan()
        positions_list = response.get("Items", [])

        # Check for orphans
        orphaned = 0
        for pos_dict in positions_list:
            pos_id = pos_dict.get("position_id")
            long_order_id = pos_dict.get("long_order_id")
            short_order_id = pos_dict.get("short_order_id")

            if long_order_id and long_order_id not in kalshi_order_ids:
                if short_order_id and short_order_id not in kalshi_order_ids:
                    # Both orders are missing from Kalshi
                    logger.warning(f"Orphaned position: {pos_id} (orders not in Kalshi)")
                    orphaned += 1

        return orphaned

    except Exception as e:
        logger.error(f"Reconciliation failed: {e}")
        return 0


# ============================================================================
# ORDER EXECUTION HELPERS
# ============================================================================


async def _place_buy_yes_order(
    kalshi_client,
    ticker: str,
    price: float,
    contracts: int,
) -> str:
    """
    Place order to BUY YES contracts.

    Returns:
        Order ID
    """
    order = await kalshi_client.place_order(
        ticker=ticker,
        side="yes",
        order_type="limit",
        limit_price=price,
        quantity=contracts,
    )
    return order.get("order_id", "")


async def _place_buy_no_order(
    kalshi_client,
    ticker: str,
    no_price: float,
    contracts: int,
) -> str:
    """
    Place order to BUY NO contracts.

    On Kalshi, this is: side="no", order_type="limit", limit_price=no_price

    Note: This assumes KalshiClient has been updated with NO-side order support.

    Returns:
        Order ID
    """
    # TODO: Verify KalshiClient supports side="no"
    order = await kalshi_client.place_order(
        ticker=ticker,
        side="no",
        order_type="limit",
        limit_price=no_price,
        quantity=contracts,
    )
    return order.get("order_id", "")


async def _place_sell_yes_order(
    kalshi_client,
    ticker: str,
    contracts: int,
) -> str:
    """
    Place order to SELL YES contracts (market order for liquidity).
    """
    order = await kalshi_client.place_order(
        ticker=ticker,
        side="yes",
        order_type="market",
        quantity=contracts,
    )
    return order.get("order_id", "")


async def _place_sell_no_order(
    kalshi_client,
    ticker: str,
    contracts: int,
) -> str:
    """
    Place order to SELL NO contracts (market order for liquidity).
    """
    order = await kalshi_client.place_order(
        ticker=ticker,
        side="no",
        order_type="market",
        quantity=contracts,
    )
    return order.get("order_id", "")


async def _fetch_market(kalshi_client, ticker: str) -> Dict:
    """
    Fetch current market data for a ticker.
    """
    try:
        market = await kalshi_client.get_market(ticker)
        return market
    except Exception as e:
        logger.error(f"Failed to fetch market {ticker}: {e}")
        return {}


# ============================================================================
# ALERTS
# ============================================================================


async def _send_execution_alert(sns_client, opp, position) -> None:
    """
    Send SNS alert when position is executed.
    """
    topic_arn = os.environ.get("BRACKET_BUSTER_SNS_TOPIC_ARN", "")
    if not topic_arn:
        return

    try:
        message = (
            f"Position Executed: {position.team_name}\n"
            f"Type: {opp.arb_type}\n"
            f"Tiers: {opp.long_tier} vs {opp.short_tier}\n"
            f"Capital: ${position.total_capital_deployed():.2f}\n"
            f"Expected Profit: ${position.guaranteed_profit:.2f}\n"
        )

        sns_client.publish(
            TopicArn=topic_arn,
            Subject=f"[EXECUTION] {position.team_name}",
            Message=message,
        )
    except Exception as e:
        logger.error(f"Failed to send execution alert: {e}")


# ============================================================================
# AWS LAMBDA HANDLER
# ============================================================================


async def lambda_handler(event, context):
    """
    AWS Lambda handler for EventBridge-triggered monitor runs.

    Runs every 5 minutes.
    """
    import boto3

    dynamodb = boto3.resource("dynamodb")
    sns = boto3.client("sns")

    # TODO: Instantiate real kalshi_client
    kalshi_client = None

    result = await run(
        kalshi_client=kalshi_client,
        dynamodb_client=dynamodb,
        sns_client=sns,
        test_mode=False,
    )

    return {
        "statusCode": 200 if result["status"] == "success" else 500,
        "body": result,
    }
