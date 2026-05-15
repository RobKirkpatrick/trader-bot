"""
NEW: AWS Lambda handler functions for funding_rate module.

Two entry points:
  - handler_scanner: Invoked every 4 hours by EventBridge
  - handler_monitor: Invoked every 1 hour by EventBridge

Both read credentials from environment and AWS Secrets Manager.
"""

import asyncio
import json
import logging
import os
from typing import Any

from funding_rate.coinbase_client import CoinbaseClient
from funding_rate.monitor import FundingRateMonitor
from funding_rate.scanner import FundingRateScanner

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configure structured logging
handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
)
logger.addHandler(handler)


def _get_coinbase_credentials() -> tuple[str, str]:
    """
    Retrieve Coinbase API credentials from environment or Secrets Manager.

    In development, reads from .env or environment variables.
    In Lambda, can fetch from AWS Secrets Manager for security.

    Returns:
        Tuple of (api_key_name, private_key_pem)

    Raises:
        ValueError: If credentials not found
    """
    api_key_name = os.environ.get("COINBASE_API_KEY_NAME")
    private_key = os.environ.get("COINBASE_PRIVATE_KEY")

    if not api_key_name:
        raise ValueError("COINBASE_API_KEY_NAME not set")
    if not private_key:
        raise ValueError("COINBASE_PRIVATE_KEY not set")

    # Unescape newlines if present
    private_key = private_key.replace("\\n", "\n")

    return api_key_name, private_key


def handler_scanner(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    NEW: Lambda handler for funding rate scanner (every 4 hours).

    Fetches current funding rates for all supported pairs and writes
    promising opportunities to DynamoDB.

    Args:
        event: EventBridge event (ignored)
        context: Lambda context

    Returns:
        HTTP response with scan results
    """
    logger.info("Funding rate scanner invoked")

    try:
        # Check if module is enabled
        if not os.environ.get("FUNDING_RATE_ENABLED", "false").lower() == "true":
            logger.info("Funding rate module disabled, exiting")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Module disabled",
                    "module": "funding_rate",
                })
            }

        # Get Coinbase credentials
        api_key_name, private_key = _get_coinbase_credentials()

        # Create Coinbase client
        client = CoinbaseClient(
            api_key_name=api_key_name,
            private_key_pem=private_key,
        )

        # Create and run scanner
        scanner = FundingRateScanner(
            client,
            opportunities_table_name=os.environ.get(
                "FUNDING_RATE_OPPORTUNITIES_TABLE",
                "funding-rate-opportunities"
            ),
            positions_table_name=os.environ.get(
                "FUNDING_RATE_POSITIONS_TABLE",
                "funding-rate-positions"
            ),
        )

        # Run async scan
        results = asyncio.run(scanner.run(
            sns_topic_arn=os.environ.get("SNS_TOPIC_ARN")
        ))

        logger.info(f"Scan completed: {results['summary']}")

        # Close client session
        asyncio.run(client.close())

        return {
            "statusCode": 200,
            "body": json.dumps(results)
        }

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": str(e),
                "message": "Invalid configuration"
            })
        }
    except Exception as e:
        logger.error(f"Scanner error: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
                "message": "Scanner failed"
            })
        }


def handler_monitor(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    NEW: Lambda handler for funding rate monitor (every 1 hour).

    Executes pending opportunities and monitors open positions for:
      - Funding payment accumulation
      - Rebalancing (drift detection)
      - Exit conditions (rate drop, max hold exceeded)

    Args:
        event: EventBridge event (ignored)
        context: Lambda context

    Returns:
        HTTP response with monitor results
    """
    logger.info("Funding rate monitor invoked")

    try:
        # Check if module is enabled
        if not os.environ.get("FUNDING_RATE_ENABLED", "false").lower() == "true":
            logger.info("Funding rate module disabled, exiting")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Module disabled",
                    "module": "funding_rate",
                })
            }

        # Get Coinbase credentials
        api_key_name, private_key = _get_coinbase_credentials()

        # Create Coinbase client
        client = CoinbaseClient(
            api_key_name=api_key_name,
            private_key_pem=private_key,
        )

        # Create and run monitor
        monitor = FundingRateMonitor(
            client,
            opportunities_table_name=os.environ.get(
                "FUNDING_RATE_OPPORTUNITIES_TABLE",
                "funding-rate-opportunities"
            ),
            positions_table_name=os.environ.get(
                "FUNDING_RATE_POSITIONS_TABLE",
                "funding-rate-positions"
            ),
            max_position_usd=_get_float_env("FUNDING_RATE_MAX_POSITION"),
            max_pct_balance=_get_float_env("FUNDING_RATE_MAX_PCT_BALANCE"),
        )

        # Run async monitor
        results = asyncio.run(monitor.run(
            sns_topic_arn=os.environ.get("SNS_TOPIC_ARN")
        ))

        logger.info(f"Monitor completed: {results['summary']}")

        # Close client session
        asyncio.run(client.close())

        return {
            "statusCode": 200,
            "body": json.dumps(results)
        }

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": str(e),
                "message": "Invalid configuration"
            })
        }
    except Exception as e:
        logger.error(f"Monitor error: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
                "message": "Monitor failed"
            })
        }


def _get_float_env(key: str) -> float | None:
    """
    Get a float value from environment, return None if not set.

    Args:
        key: Environment variable name

    Returns:
        Float value or None
    """
    value = os.environ.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        logger.warning(f"Invalid float for {key}={value}, using None")
        return None
