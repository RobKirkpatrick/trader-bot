"""
AWS Lambda handler for the sentiment trading bot.

Triggered by EventBridge Scheduler (08:00, 09:35, 12:00, 15:45 ET on weekdays).

Responsibilities:
  1. Pull secrets from AWS Secrets Manager
  2. Inject secrets into the runtime environment
  3. Determine which scan window to run based on the event time
  4. Run the appropriate sentiment scan
  5. For each strong signal: call Claude agent → place order or SNS alert

Environment variables expected on the Lambda function:
  AWS_SECRET_NAME   — Secrets Manager secret name
  SNS_TOPIC_ARN     — target SNS topic ARN
  TRADE_DEBUG       — "true" to receive SNS on every agent decision (default false)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# Lazy imports after secrets are injected
logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Secrets Manager
# ---------------------------------------------------------------------------

def _load_secrets(secret_name: str, region: str = "us-east-2") -> dict:
    """
    Fetch a JSON secret from AWS Secrets Manager.
    Expected secret keys: PUBLIC_API_SECRET, POLYGON_API_KEY
    """
    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        logger.error("Secrets Manager error: %s", exc)
        raise

    secret_str = response.get("SecretString") or ""
    return json.loads(secret_str)


def _inject_secrets(secrets: dict) -> None:
    """Write secrets into os.environ so Settings picks them up."""
    for key, value in secrets.items():
        os.environ[key] = str(value)
    logger.info("Secrets injected: %s", list(secrets.keys()))


# ---------------------------------------------------------------------------
# Window detection
# ---------------------------------------------------------------------------

def _detect_window(event: dict) -> str:
    """
    Determine which scan window fired.

    EventBridge Scheduler (primary): passes {"window": "pre_market"|"market_open"|"midday"}
    as the Lambda event payload — explicit and DST-safe.

    Legacy fallback: EventBridge Rules pass event["time"] (UTC ISO-8601) and
    event["resources"] (rule ARN). Used if the window key is absent.
    """
    # EventBridge Scheduler — explicit window key
    if "window" in event:
        return event["window"]

    # Legacy EventBridge Rules — parse from event time using zoneinfo for DST
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    raw_time = event.get("time", "")
    try:
        utc_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        utc_dt = datetime.now(timezone.utc)

    et_dt     = utc_dt.astimezone(ET)
    et_hour   = et_dt.hour
    et_minute = et_dt.minute

    if et_hour == 8:
        return "pre_market"
    if et_hour == 9 and et_minute >= 30:
        return "market_open"
    if et_hour == 12:
        return "midday"
    if et_hour == 15 and et_minute >= 45:
        return "end_of_day"
    if et_hour == 18 and et_minute >= 45:
        return "suggestions"
    if et_hour == 19:
        return "suggestions"

    resources = event.get("resources", [])
    for r in resources:
        r_lower = r.lower()
        if "pre-market" in r_lower or "premarket" in r_lower:
            return "pre_market"
        if "market-open" in r_lower or "marketopen" in r_lower:
            return "market_open"
        if "midday" in r_lower:
            return "midday"
        if "eod" in r_lower or "end-of-day" in r_lower:
            return "end_of_day"
        if "evening" in r_lower or "weekend" in r_lower or "suggest" in r_lower:
            return "suggestions"

    logger.warning("Could not detect window from event (et_hour=%d); defaulting to midday.", et_hour)
    return "midday"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:  # noqa: ANN001
    """
    Main Lambda entry point.

    Returns a dict with:
      statusCode  — 200 on success
      body        — JSON string with scan summary

    Two invocation paths:
      1. EventBridge Scheduler — {"window": "..."} payload
      2. Lambda Function URL   — HTTP GET with {"rawPath": "/approve", ...}
    """
    logger.info("Lambda triggered. Event: %s", json.dumps(event))

    # 1. Load and inject secrets (needed by both scheduled scans and approval handler)
    secret_name = os.environ.get("AWS_SECRET_NAME", "trading-bot/secrets")
    region = os.environ.get("AWS_REGION", "us-east-2")

    try:
        secrets = _load_secrets(secret_name, region)
        _inject_secrets(secrets)
    except Exception as exc:
        return _error_response(f"Failed to load secrets: {exc}")

    # 2. Route HTTP requests (Lambda Function URL) to the approval handler
    if "rawPath" in event:
        from api.approval_handler import handle_approval  # noqa: PLC0415
        return handle_approval(event)

    # 3. Kill switch — set TRADING_PAUSED=true in .env or Secrets Manager to halt all trades immediately
    from config.settings import settings  # noqa: PLC0415
    if settings.TRADING_PAUSED:
        logger.warning("TRADING_PAUSED=true — skipping all scan windows. No orders will be placed.")
        return {"statusCode": 200, "body": '{"status": "paused"}'}

    # 4. Import modules AFTER secrets are in env (Settings reads os.environ)
    from scheduler.jobs import (  # noqa: PLC0415
        run_pre_market_scan, run_market_open_scan, run_midday_scan, run_end_of_day_scan,
    )

    # 5. Determine which window to run
    window = _detect_window(event)
    logger.info("Running window: %s", window)

    # 6. Execute scan
    try:
        if window == "pre_market":
            result = run_pre_market_scan()
        elif window == "market_open":
            result = run_market_open_scan()
        elif window == "end_of_day":
            result = run_end_of_day_scan()
        elif window == "suggestions":
            from scheduler.suggestions import run_suggestions_scan  # noqa: PLC0415
            result = run_suggestions_scan()
        elif window == "weekly_review":
            from scheduler.weekly_review import run_weekly_review  # noqa: PLC0415
            result = run_weekly_review()
        elif window == "carpet_bagger_scout":
            from carpet_bagger.scout import run as cb_scout  # noqa: PLC0415
            result = cb_scout(cfg=event)
        elif window == "carpet_bagger_monitor":
            from carpet_bagger.monitor import run as cb_monitor  # noqa: PLC0415
            result = cb_monitor(cfg=event)
        elif window == "carpet_bagger_summary":
            from carpet_bagger.monitor import summary as cb_summary  # noqa: PLC0415
            result = cb_summary(cfg=event)
        elif window == "carpet_bagger_force_sell":
            from carpet_bagger.monitor import force_sell as cb_force_sell  # noqa: PLC0415
            result = cb_force_sell(cfg=event)
        elif window == "carpet_bagger_baseball_exit":
            from carpet_bagger.monitor import baseball_exit as cb_baseball_exit  # noqa: PLC0415
            result = cb_baseball_exit(cfg=event)
        elif window == "bracket_buster_scout":
            import asyncio  # noqa: PLC0415
            from bracket_buster import scout as bb_scout  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            from config.settings import settings as _s  # noqa: PLC0415
            _bb_kalshi = KalshiClient(api_key=_s.KALSHI_API_KEY, rsa_private_key_pem=_s.KALSHI_RSA_PRIVATE_KEY)
            _bb_ddb    = boto3.resource("dynamodb", region_name=_s.AWS_REGION)
            _bb_sns    = boto3.client("sns", region_name="us-east-1")
            result = asyncio.run(bb_scout.run(
                kalshi_client=_bb_kalshi,
                dynamodb_client=_bb_ddb,
                sns_client=_bb_sns,
                test_mode=not os.environ.get("BRACKET_BUSTER_ENABLED", "false").lower() == "true",
            ))
        elif window == "bracket_buster_monitor":
            import asyncio  # noqa: PLC0415
            from bracket_buster import monitor as bb_monitor  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            from config.settings import settings as _s  # noqa: PLC0415
            _bb_kalshi = KalshiClient(api_key=_s.KALSHI_API_KEY, rsa_private_key_pem=_s.KALSHI_RSA_PRIVATE_KEY)
            _bb_ddb    = boto3.resource("dynamodb", region_name=_s.AWS_REGION)
            _bb_sns    = boto3.client("sns", region_name="us-east-1")
            result = asyncio.run(bb_monitor.run(
                kalshi_client=_bb_kalshi,
                dynamodb_client=_bb_ddb,
                sns_client=_bb_sns,
                test_mode=not os.environ.get("BRACKET_BUSTER_ENABLED", "false").lower() == "true",
            ))
        elif window == "edgar_scan":
            from scheduler.jobs import run_edgar_scan  # noqa: PLC0415
            result = run_edgar_scan()
        elif window == "macro_trader_scanner":
            import asyncio  # noqa: PLC0415
            from macro_trader.scanner import run as _mt_scanner  # noqa: PLC0415
            if not settings.MACRO_TRADER_ENABLED:
                result = {"status": "disabled", "window": "macro_trader_scanner"}
            else:
                result = asyncio.run(_mt_scanner())
        elif window == "macro_trader_monitor":
            import asyncio  # noqa: PLC0415
            from macro_trader.monitor import run as _mt_monitor  # noqa: PLC0415
            if not settings.MACRO_TRADER_ENABLED:
                result = {"status": "disabled", "window": "macro_trader_monitor"}
            else:
                result = asyncio.run(_mt_monitor())
        elif window == "funding_rate_scanner":
            from funding_rate.lambda_handlers import handler_scanner as _fr_scanner  # noqa: PLC0415
            result = _fr_scanner(event, context)
        elif window == "funding_rate_monitor":
            from funding_rate.lambda_handlers import handler_monitor as _fr_monitor  # noqa: PLC0415
            result = _fr_monitor(event, context)
        elif window == "political_trader_scanner":
            import asyncio  # noqa: PLC0415
            from political_trader.scanner import PoliticalMarketScanner  # noqa: PLC0415
            from political_trader.signal_reader import PoliticalSignalReader  # noqa: PLC0415
            import political_trader.scanner as _pt_scanner_mod  # noqa: PLC0415
            import political_trader.monitor as _pt_monitor_mod  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            if not settings.POLITICAL_TRADER_ENABLED:
                result = {"status": "disabled", "window": "political_trader_scanner"}
            else:
                _pt_scanner_mod.SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
                _pt_monitor_mod.SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
                _pt_kalshi = KalshiClient(api_key=settings.KALSHI_API_KEY, rsa_private_key_pem=settings.KALSHI_RSA_PRIVATE_KEY)
                _pt_reader  = PoliticalSignalReader(kalshi_client=_pt_kalshi)
                _pt_scanner = PoliticalMarketScanner(_pt_kalshi, _pt_reader)
                result = asyncio.run(_pt_scanner.run())
        elif window == "political_trader_monitor":
            import asyncio  # noqa: PLC0415
            from political_trader.monitor import PoliticalMonitor  # noqa: PLC0415
            from political_trader.signal_reader import PoliticalSignalReader  # noqa: PLC0415
            import political_trader.scanner as _pt_scanner_mod  # noqa: PLC0415
            import political_trader.monitor as _pt_monitor_mod  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            if not settings.POLITICAL_TRADER_ENABLED:
                result = {"status": "disabled", "window": "political_trader_monitor"}
            else:
                _pt_scanner_mod.SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
                _pt_monitor_mod.SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
                _pt_kalshi  = KalshiClient(api_key=settings.KALSHI_API_KEY, rsa_private_key_pem=settings.KALSHI_RSA_PRIVATE_KEY)
                _pt_reader  = PoliticalSignalReader(kalshi_client=_pt_kalshi)
                _pt_monitor = PoliticalMonitor(_pt_kalshi, _pt_reader)
                result = asyncio.run(_pt_monitor.run())
        elif window == "weather_trader_scanner":
            import asyncio  # noqa: PLC0415
            from weather_trader.scanner import run_scanner as _wt_scanner  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            if not settings.WEATHER_TRADER_ENABLED:
                result = {"status": "disabled", "window": "weather_trader_scanner"}
            else:
                _wt_kalshi = KalshiClient(api_key=settings.KALSHI_API_KEY, rsa_private_key_pem=settings.KALSHI_RSA_PRIVATE_KEY)
                _wt_ddb    = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
                _wt_sns    = boto3.client("sns", region_name="us-east-1")
                result = asyncio.run(_wt_scanner(kalshi_client=_wt_kalshi, dynamo_client=_wt_ddb, sns_client=_wt_sns))
        elif window == "weather_trader_monitor":
            import asyncio  # noqa: PLC0415
            from weather_trader.monitor import run_monitor as _wt_monitor  # noqa: PLC0415
            from carpet_bagger.kalshi_client import KalshiClient  # noqa: PLC0415
            if not settings.WEATHER_TRADER_ENABLED:
                result = {"status": "disabled", "window": "weather_trader_monitor"}
            else:
                _wt_kalshi = KalshiClient(api_key=settings.KALSHI_API_KEY, rsa_private_key_pem=settings.KALSHI_RSA_PRIVATE_KEY)
                _wt_ddb    = boto3.resource("dynamodb", region_name=settings.AWS_REGION)
                _wt_sns    = boto3.client("sns", region_name="us-east-1")
                result = asyncio.run(_wt_monitor(kalshi_client=_wt_kalshi, dynamo_client=_wt_ddb, sns_client=_wt_sns))
        else:
            logger.error(
                "Unknown window '%s' — refusing to run default scan. "
                "Add a handler in lambda_function.py or remove the EventBridge schedule.",
                window,
            )
            result = {"status": "unknown_window", "window": window, "orders_placed": 0}
    except Exception as exc:
        logger.error("Scan failed: %s", exc, exc_info=True)
        return _error_response(f"Scan failed: {exc}")

    logger.info("Scan complete. Result: %s", json.dumps(result))
    return {
        "statusCode": 200,
        "body": json.dumps(result),
    }


def _error_response(message: str) -> dict:
    logger.error(message)
    return {
        "statusCode": 500,
        "body": json.dumps({"error": message}),
    }
