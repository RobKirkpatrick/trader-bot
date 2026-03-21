"""
Carpet Bagger scout — runs 8am ET daily.

Scans all open Kalshi sports events, filters for pre-game favorites
(55–70% probability), and writes them to carpet-bagger-watchlist.

Key requirement: only single-game LIVE events that resolve within 36 hours.
This filters out futures (season win totals, division winners, tournament
bracket qualifiers, etc.) that cannot be exited at a meaningful price spike.
"""

import logging
import re as _re
from datetime import datetime, timezone, timedelta

import boto3

from carpet_bagger.kalshi_client import KalshiClient, parse_market_price
from carpet_bagger.models import WatchlistRecord
from carpet_bagger.monitor import _game_date_from_ticker, _MONTH_MAP
from carpet_bagger.strategy import (
    SPORT_SERIES, BLOCKED_SPORT_SERIES, PRE_GAME_MIN, PRE_GAME_MAX, MIN_MINS_TO_GAME, MAX_GAME_AGE_MINS, MAX_CLOSE_HOURS,
)


def _game_datetime_from_ticker(ticker: str) -> datetime | None:
    """
    Extract game start datetime from a Kalshi ticker.
    Kalshi encodes date+time as YYMONDDHHMMTEAMS, e.g.:
      KXNCAABBGAME-26MAR131930TAMOKL-TAM → 2026-03-13 19:30 ET
    Returns UTC datetime if parseable, else None.
    """
    from zoneinfo import ZoneInfo
    m = _re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{4})', ticker.upper())
    if not m:
        return None
    try:
        year  = 2000 + int(m.group(1))
        month = _MONTH_MAP[m.group(2)]
        day   = int(m.group(3))
        hhmm  = m.group(4)
        hour, minute = int(hhmm[:2]), int(hhmm[2:])
        et = ZoneInfo("America/New_York")
        dt_et = datetime(year, month, day, hour, minute, tzinfo=et)
        return dt_et.astimezone(timezone.utc)
    except (ValueError, KeyError):
        return None

logger = logging.getLogger(__name__)

_TABLE = "carpet-bagger-watchlist"
_REGION = "us-east-2"

# Human-readable labels for known series
_SPORT_LABELS = {
    "KXNBAGAMES":   "NBA",
    "KXNBAGAME":    "NBA",
    "KXNHLGAME":    "NHL",
    "KXNCAAMBGAME": "NCAAB-M",   # NCAA Men's Basketball (correct series)
    "KXNCAABGAME":  "NCAAB-M",   # legacy/dead series
    "KXNCAAWBGAME": "NCAAW",
    "KXMLBGAME":    "MLB",
    "KXMLBSTGAME":  "MLB-ST",    # Spring Training
    "KXEPLGAME":    "EPL",       # English Premier League (likely ticker format)
    "KXPLGAME":     "EPL",
    "KXPREMGAME":   "EPL",
    "KXUCLGAME":    "UCL",       # Champions League
    "KXUEFAGAME":   "UEFA",
    "KXMLS":        "MLS",
    "KXMLSGAME":    "MLS",
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
        new_series = [s for s in discovered if s not in series and s not in BLOCKED_SPORT_SERIES]
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

            # Probability filter — handles both new dollars format and legacy cents format
            yes_ask = parse_market_price(market, "yes_ask") or parse_market_price(market, "last_price")

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

            # Game date filter — use date embedded in ticker as the authoritative source.
            # Allow games up to MAX_CLOSE_HOURS ahead so we can pre-load March Madness
            # games scouted on Sunday for Tuesday tip-offs, etc.
            # The monitor will not BUY until game_date == today — scout just pre-populates.
            ticker_date = _game_date_from_ticker(ticker)
            if ticker_date is not None:
                days_ahead = (ticker_date - today_et).days
                if days_ahead > (MAX_CLOSE_HOURS // 24 + 1):
                    logger.debug("Skipping %s — game date %s is >%dh away", ticker, ticker_date, MAX_CLOSE_HOURS)
                    continue
                if ticker_date < today_et:
                    logger.debug("Skipping %s — game date %s was yesterday or earlier", ticker, ticker_date)
                    continue

            # Timing guard — use game time from ticker (authoritative); fall back to open_time
            game_dt = _game_datetime_from_ticker(ticker)
            if game_dt is None and open_time_str and open_time_str != close_time_str:
                try:
                    game_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                except ValueError:
                    pass
            if game_dt is not None:
                mins_to_game = (game_dt - now_utc).total_seconds() / 60
                if 0 < mins_to_game < MIN_MINS_TO_GAME:
                    logger.debug("Skipping %s — tips off in %.0f min (too soon)", ticker, mins_to_game)
                    continue
                if mins_to_game < -MAX_GAME_AGE_MINS:
                    logger.debug("Skipping %s — game started %.0f min ago (too late to enter)", ticker, -mins_to_game)
                    continue

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
