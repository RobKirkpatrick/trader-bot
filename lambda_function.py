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

    # 3. Import modules AFTER secrets are in env (Settings reads os.environ)
    from scheduler.jobs import (  # noqa: PLC0415
        run_pre_market_scan, run_market_open_scan, run_midday_scan, run_end_of_day_scan,
    )

    # 4. Determine which window to run
    window = _detect_window(event)
    logger.info("Running window: %s", window)

    # 5. Execute scan
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
        elif window == "edgar_scan":
            from scheduler.jobs import run_edgar_scan  # noqa: PLC0415
            result = run_edgar_scan()
        else:
            result = run_midday_scan()
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
