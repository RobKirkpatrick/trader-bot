"""
Carpet Bagger scout — runs 8am ET daily.

Scans all open Kalshi sports events, filters for pre-game favorites
(55–70% probability), and writes them to carpet-bagger-watchlist.

Key requirement: only single-game LIVE events that resolve within 36 hours.
This filters out futures (season win totals, division winners, tournament
bracket qualifiers, etc.) that cannot be exited at a meaningful price spike.
"""

import logging
from datetime import datetime, timezone, timedelta

import boto3

from carpet_bagger.kalshi_client import KalshiClient
from carpet_bagger.models import WatchlistRecord
from carpet_bagger.strategy import (
    SPORT_SERIES, PRE_GAME_MIN, PRE_GAME_MAX, MIN_MINS_TO_GAME, MAX_CLOSE_HOURS,
)

logger = logging.getLogger(__name__)

_TABLE = "carpet-bagger-watchlist"
_REGION = "us-east-2"

# Human-readable labels for known series
_SPORT_LABELS = {
    "KXNBAGAMES":    "NBA",
    "KXNBAGAME":     "NBA",
    "KXNHLGAME":     "NHL",
    "KXNCAABGAME":   "NCAAB-M",
    "KXNCAABBGAME":  "NCAAB-M",
    "KXNCAAWBGAME":  "NCAAW",
    "KXMLBGAME":     "MLB",
}


def _dynamodb():
    return boto3.client("dynamodb", region_name=_REGION)


def _existing_tickers() -> set[str]:
    """Return the set of market_tickers already in the watchlist (status=watching or bought)."""
    db    = _dynamodb()
    items = db.scan(TableName=_TABLE, ProjectionExpression="market_ticker,#s",
                    ExpressionAttributeNames={"#s": "status"}).get("Items", [])
    # Only block re-adding active records (watching/bought). Closed records are fine to ignore.
    return {
        i["market_ticker"]["S"]
        for i in items
        if i.get("status", {}).get("S", "") in ("watching", "bought")
    }


def _write_record(record: WatchlistRecord) -> None:
    _dynamodb().put_item(TableName=_TABLE, Item=record.to_dynamodb())


def _publish_sns(message: str, subject: str) -> None:
    import os
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.warning("SNS_TOPIC_ARN not set — skipping scout notification")
        return
    arn_parts = topic_arn.split(":")
    region    = arn_parts[3] if len(arn_parts) >= 4 else _REGION
    boto3.client("sns", region_name=region).publish(
        TopicArn=topic_arn, Subject=subject[:99], Message=message,
    )


def _build_sports_series(client: KalshiClient) -> list[str]:
    """
    Build the full list of sports game series to scan.

    Starts with the hardcoded SPORT_SERIES (guaranteed individual-game markets),
    then dynamically discovers any additional series from the Kalshi API.
    This catches March Madness, conference tournaments, soccer, etc.
    """
    series = list(SPORT_SERIES)
    try:
        discovered = client.discover_sports_game_series()
        new_series = [s for s in discovered if s not in series]
        if new_series:
            logger.info("Discovered %d additional series beyond hardcoded list: %s", len(new_series), new_series)
        series.extend(new_series)
    except Exception as exc:
        logger.warning("Dynamic series discovery failed, using hardcoded list only: %s", exc)
    return series


def run(cfg: dict | None = None) -> dict:
    """
    Scout entry point. cfg is unused but accepted for Lambda compatibility.
    """
    import os
    from config.settings import settings

    logger.info("=== Carpet Bagger scout starting ===")

    # Load credentials
    api_key = os.environ.get("KALSHI_API_KEY", "")
    rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
    if not api_key or not rsa_key:
        logger.error("KALSHI_API_KEY or KALSHI_RSA_PRIVATE_KEY not set")
        return {"error": "missing credentials"}

    client = KalshiClient(api_key, rsa_key)

    # Check exchange is open
    if not client.is_trading_active():
        logger.info("Kalshi exchange not active — scout skipping")
        return {"skipped": "exchange_inactive"}

    from zoneinfo import ZoneInfo
    existing    = _existing_tickers()
    now_utc     = datetime.now(timezone.utc)
    today_et    = datetime.now(ZoneInfo("America/New_York")).date()
    all_series  = _build_sports_series(client)

    added: dict[str, int] = {s: 0 for s in all_series}
    total_added = 0

    for sport in all_series:
        try:
            markets = client.get_series_markets(sport)
        except Exception as exc:
            logger.error("Failed to fetch markets for %s: %s", sport, exc)
            continue

        if not markets:
            logger.debug("No open markets for series %s", sport)
            continue

        skipped_prob = 0
        for market in markets:
            ticker = market.get("ticker", "")
            if not ticker or ticker in existing:
                continue

            # Probability filter — prefer yes_ask; fall back to last_price if ask not posted
            yes_ask_cents = market.get("yes_ask") or market.get("last_price") or 0
            yes_ask = yes_ask_cents / 100.0

            if not (PRE_GAME_MIN <= yes_ask <= PRE_GAME_MAX):
                skipped_prob += 1
                logger.debug(
                    "Skip %s — yes_ask=%.2f outside [%.2f, %.2f]",
                    ticker, yes_ask, PRE_GAME_MIN, PRE_GAME_MAX,
                )
                continue

            # Futures filtering is done at the series level: only GAME/GAMES
            # series are scanned, so every market here is a single-game event.
            # Kalshi's close_time is the series expiry date (not game time), so
            # it cannot be used to distinguish single-game from futures.
            close_time_str = market.get("close_time") or ""
            open_time_str  = market.get("open_time")  or ""

            # Game start filter — same calendar day in ET only, not starting in < 30 min
            if open_time_str and open_time_str != close_time_str:
                try:
                    game_dt      = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                    game_dt_et   = game_dt.astimezone(ZoneInfo("America/New_York"))
                    mins_to_game = (game_dt - now_utc).total_seconds() / 60
                    if 0 < mins_to_game < MIN_MINS_TO_GAME:
                        logger.debug("Skipping %s — opens in %.0f min (too soon)", ticker, mins_to_game)
                        continue
                    if game_dt_et.date() != today_et:
                        logger.debug("Skipping %s — game on %s, not today (%s)", ticker, game_dt_et.date(), today_et)
                        continue
                except ValueError:
                    pass

            # Teams / title from market subtitle or title
            teams = (
                market.get("subtitle")
                or market.get("title")
                or ticker
            )

            record = WatchlistRecord(
                market_ticker = ticker,
                sport         = sport,
                teams         = teams,
                game_time     = close_time_str,
                pre_game_prob = yes_ask,
                current_prob  = yes_ask,
                status        = "watching",
                last_updated  = now_utc.isoformat(),
            )
            try:
                _write_record(record)
                existing.add(ticker)
                added[sport] += 1
                total_added  += 1
                logger.info("Watchlist: added %s (%s) yes_ask=%.2f", ticker, sport, yes_ask)
            except Exception as exc:
                logger.error("DynamoDB write failed for %s: %s", ticker, exc)

        logger.info(
            "Series %s: %d markets fetched, %d added, %d outside prob filter",
            sport, len(markets), added[sport], skipped_prob,
        )

    # SNS summary
    breakdown_parts = []
    for s in all_series:
        if added[s] > 0 or s in _SPORT_LABELS:
            label = _SPORT_LABELS.get(s, s)
            breakdown_parts.append(f"{label}:{added[s]}")
    breakdown = " | ".join(breakdown_parts) if breakdown_parts else "no series"

    message   = (
        f"Carpet Bagger: {total_added} game{'s' if total_added != 1 else ''} added to watchlist\n"
        f"{breakdown}\n"
        f"Series scanned: {len(all_series)}\n"
        f"Scout time: {now_utc.strftime('%a %b %d, %Y  %I:%M %p UTC')}"
    )
    subject = f"[TraderBot] Carpet Bagger: {total_added} games scouted"
    try:
        _publish_sns(message, subject)
    except Exception as exc:
        logger.warning("SNS failed: %s", exc)

    logger.info("Scout complete: %d added from %d series", total_added, len(all_series))
    return {"window": "carpet_bagger_scout", "added": total_added, "by_sport": added}
