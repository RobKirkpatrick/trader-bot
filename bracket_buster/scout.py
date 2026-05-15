"""
bracket_buster/scout.py

Scout module: discovers tournament arbitrage opportunities and stores them in DynamoDB.

Runs on a schedule (configurable via settings) to:
1. Fetch all Kalshi markets across tournament series
2. Dynamically discover new tournament series (similar to carpet_bagger)
3. Build team-market mapping
4. Run pure_arb and soft_arb detection
5. Write opportunities to DynamoDB
6. Send SNS alerts with summary
"""

import asyncio
import logging
from datetime import datetime
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
    Main scout execution.

    Args:
        kalshi_client: KalshiClient instance with REST API access
        dynamodb_client: boto3 DynamoDB resource
        sns_client: boto3 SNS client for alerts
        test_mode: If True, don't write to DynamoDB/SNS (for testing)

    Returns:
        Dict with execution results: {
            "pure_arbs_found": int,
            "soft_arbs_found": int,
            "timestamp": str,
            "status": "success" | "error",
            "error_message": str (if error)
        }
    """
    start_time = datetime.utcnow()
    logger.info("Scout run started")

    try:
        # Phase 1: Verify Kalshi is operational
        logger.info("Checking Kalshi trading status...")
        is_trading = await _verify_kalshi_operational(kalshi_client)
        if not is_trading:
            logger.warning("Kalshi not operational, aborting scout")
            return {
                "status": "skipped",
                "reason": "Kalshi not trading",
                "timestamp": start_time.isoformat(),
            }

        # Phase 2: Fetch markets and build market map
        logger.info("Fetching tournament markets...")
        markets = await _fetch_tournament_markets(kalshi_client)
        logger.info(f"Fetched {len(markets)} markets")

        if not markets:
            logger.warning("No markets found")
            return {
                "status": "skipped",
                "reason": "No markets found",
                "timestamp": start_time.isoformat(),
            }

        # Phase 3: Initialize analyzer and build market map
        from .analyzer import BracketAnalyzer

        analyzer = BracketAnalyzer(kalshi_client=kalshi_client)
        analyzer.build_team_market_map(markets)
        logger.info(f"Built market map for {len(analyzer.team_market_map)} teams")

        # Phase 4: Detect opportunities
        logger.info("Detecting pure arbitrage opportunities...")
        pure_arbs = analyzer.find_pure_arb()
        logger.info(f"Found {len(pure_arbs)} pure arb opportunities")

        logger.info("Detecting soft arbitrage opportunities...")
        soft_arbs = analyzer.find_soft_arb()
        logger.info(f"Found {len(soft_arbs)} soft arb opportunities")

        # Phase 5: Calculate position sizes for all opportunities
        logger.info("Calculating position sizes...")
        available_balance = await _get_available_balance(kalshi_client)
        for opp in pure_arbs + soft_arbs:
            sizes = analyzer.calculate_position_sizes(opp, available_balance)
            opp.suggested_size_usd = sizes["total_size_usd"]
            opp.suggested_contracts = sizes["num_contracts"]

        # Phase 6: Persist opportunities to DynamoDB
        if not test_mode:
            logger.info("Writing opportunities to DynamoDB...")
            await _write_opportunities_to_dynamodb(
                dynamodb_client, pure_arbs + soft_arbs
            )

        # Phase 7: Send SNS alert
        if not test_mode and (pure_arbs or soft_arbs):
            logger.info("Sending SNS alert...")
            await _send_alert(sns_client, pure_arbs, soft_arbs, analyzer)

        end_time = datetime.utcnow()
        elapsed = (end_time - start_time).total_seconds()
        logger.info(f"Scout run completed in {elapsed:.1f}s")

        return {
            "status": "success",
            "pure_arbs_found": len(pure_arbs),
            "soft_arbs_found": len(soft_arbs),
            "total_opportunities": len(pure_arbs) + len(soft_arbs),
            "teams_analyzed": len(analyzer.team_market_map),
            "available_balance": available_balance,
            "elapsed_seconds": elapsed,
            "timestamp": start_time.isoformat(),
        }

    except Exception as e:
        logger.error(f"Scout run failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error_message": str(e),
            "timestamp": start_time.isoformat(),
        }


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


async def _verify_kalshi_operational(kalshi_client) -> bool:
    """
    Quick health check on Kalshi API.

    Returns:
        True if Kalshi is trading and responsive
    """
    try:
        # Attempt to fetch exchange status
        status = await kalshi_client.get_exchange_status()
        if status and status.get("status") == "trading":
            return True
        logger.warning(f"Kalshi status: {status}")
        return False
    except Exception as e:
        logger.error(f"Failed to verify Kalshi operational: {e}")
        return False


async def _fetch_tournament_markets(kalshi_client) -> List[Dict]:
    """
    Fetch all tournament markets from Kalshi across configured series.

    Dynamically discovers new tournament series by scanning for keywords
    (similar to carpet_bagger approach).

    Returns:
        List of market dicts from Kalshi
    """
    from . import strategy

    markets = []

    # First, try known series
    for series_code in strategy.TOURNAMENT_SERIES.keys():
        try:
            logger.debug(f"Fetching markets for series: {series_code}")
            series_markets = await kalshi_client.get_markets(
                series_code=series_code, status="open"
            )
            markets.extend(series_markets)
            logger.debug(f"  Got {len(series_markets)} markets from {series_code}")
        except Exception as e:
            logger.warning(f"Failed to fetch {series_code}: {e}")
            continue

    # TODO: Dynamically discover new tournament series
    # This would involve scanning all series for tournament-related keywords:
    # - "CHAMP", "CHAMPIONSHIP"
    # - "FINAL", "F4"
    # - "ELITE", "E8"
    # - "SWEET", "S16"
    # Pseudo-code:
    #   all_series = kalshi_client.get_all_series()
    #   for series in all_series:
    #       if any(keyword in series.name.upper() for keyword in tournament_keywords):
    #           series_markets = kalshi_client.get_markets(series_code=series.code)
    #           markets.extend(series_markets)

    logger.info(f"Fetched total {len(markets)} tournament markets")

    # Filter out settled/closed markets (Kalshi uses "active" for open markets)
    open_markets = [m for m in markets if m.get("status") in ("open", "active")]
    logger.info(f"Filtered to {len(open_markets)} open markets")

    return open_markets


async def _get_available_balance(kalshi_client) -> float:
    """
    Fetch current available balance from Kalshi.

    Returns:
        Available USD balance
    """
    try:
        user = await kalshi_client.get_user()
        balance = float(user.get("balance_cents", 0)) / 100.0
        logger.info(f"Available balance: ${balance:.2f}")
        return balance
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        return 0.0


async def _write_opportunities_to_dynamodb(
    dynamodb_client,
    opportunities: List,
) -> None:
    """
    Write all opportunities to DynamoDB table.

    Table: "bracket-buster-opportunities"
    Partition key: opportunity_id
    GSI: status, arb_type for querying

    Args:
        dynamodb_client: boto3 DynamoDB resource
        opportunities: List of ArbitrageOpportunity objects
    """
    table_name = "bracket-buster-opportunities"
    table = dynamodb_client.Table(table_name)

    for opp in opportunities:
        try:
            item = opp.to_dict()
            table.put_item(Item=item)
            logger.debug(f"Wrote opportunity {opp.opportunity_id} to DynamoDB")
        except Exception as e:
            logger.error(f"Failed to write opportunity {opp.opportunity_id}: {e}")


async def _send_alert(
    sns_client,
    pure_arbs: List,
    soft_arbs: List,
    analyzer,
) -> None:
    """
    Send SNS alert with summary of found opportunities.

    Args:
        sns_client: boto3 SNS client
        pure_arbs: List of pure arb opportunities
        soft_arbs: List of soft arb opportunities
        analyzer: BracketAnalyzer instance (for market context)
    """
    from . import strategy

    topic_arn = os.environ.get("BRACKET_BUSTER_SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.warning("BRACKET_BUSTER_SNS_TOPIC_ARN not set, skipping alert")
        return

    try:
        # Build message
        message_lines = [
            "=== BRACKET BUSTER SCOUT ALERT ===",
            f"Timestamp: {datetime.utcnow().isoformat()}",
            "",
            f"PURE ARBITRAGE: {len(pure_arbs)} opportunities",
        ]

        # Top pure arbs by guaranteed profit
        if pure_arbs:
            top_pure = sorted(
                pure_arbs,
                key=lambda x: x.guaranteed_profit_per_unit,
                reverse=True,
            )[:3]
            for opp in top_pure:
                message_lines.append(
                    f"  • {opp.team_name}: {opp.long_tier} vs {opp.short_tier} "
                    f"(${opp.guaranteed_profit_per_unit:.4f} per unit, "
                    f"{opp.expected_return_pct:.1%} ROI, confidence: {opp.confidence_score:.2f})"
                )

        message_lines.append(f"\nSOFT ARBITRAGE: {len(soft_arbs)} opportunities")

        # Top soft arbs by expected return
        if soft_arbs:
            top_soft = sorted(
                soft_arbs,
                key=lambda x: x.expected_return_pct,
                reverse=True,
            )[:3]
            for opp in top_soft:
                message_lines.append(
                    f"  • {opp.team_name}: {opp.long_tier} {opp.mispriced_side.upper()} "
                    f"({opp.expected_return_pct:.1%} expected return, "
                    f"confidence: {opp.confidence_score:.2f})"
                )

        message_lines.extend(
            [
                "",
                f"Teams with opportunities: {len(analyzer.team_market_map)}",
                f"Total markets analyzed: {sum(len(t) for t in analyzer.team_market_map.values())}",
                "",
                "Scout will execute opportunities in monitor.py within 5 minutes.",
            ]
        )

        message = "\n".join(message_lines)
        subject = f"Bracket Buster: {len(pure_arbs)} pure arbs, {len(soft_arbs)} soft arbs"

        sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject,
            Message=message,
        )

        logger.info(f"Sent SNS alert to {topic_arn}")

    except Exception as e:
        logger.error(f"Failed to send SNS alert: {e}")


# ============================================================================
# AWS LAMBDA HANDLER (for EventBridge integration)
# ============================================================================


async def lambda_handler(event, context):
    """
    AWS Lambda handler for EventBridge-triggered scout runs.

    EventBridge rule triggers this daily/hourly based on schedule.

    Example CloudFormation rule:
        ScheduleExpression: "cron(0 8 * * ? *)"  # 8am UTC daily
    """
    import boto3

    # Initialize clients
    # Note: kalshi_client import and instantiation would happen here
    # For now, sketch the pattern:
    # from carpet_bagger.kalshi_client import KalshiClient
    # kalshi_client = KalshiClient(
    #     api_key=os.environ["KALSHI_API_KEY"],
    #     private_key_pem=os.environ["KALSHI_PRIVATE_KEY"]
    # )

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
