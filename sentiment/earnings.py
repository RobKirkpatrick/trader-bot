"""
Upcoming earnings signal via Alpha Vantage EARNINGS_CALENDAR.

Logic:
  - If a ticker reports earnings within the next N days, it is flagged as
    "earnings imminent" — this adds volatility/conviction to the trade signal.
  - The signal itself is a modifier (0.0 to +0.20) added to the ticker's
    absolute score, representing the elevated significance of the move.
    (Earnings act as a catalyst multiplier, not a directional signal.)

AV free tier supports EARNINGS_CALENDAR — returns CSV.
One call covers all symbols.
"""

import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_HORIZON_DAYS = 7   # Flag earnings within this many days


def fetch_earnings_dates(
    tickers: list[str] | None = None,
    api_key: str | None = None,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
) -> dict[str, str | None]:
    """
    Return a dict mapping ticker → next earnings date string (YYYY-MM-DD)
    for any watchlist ticker reporting within the next `horizon_days`.

    Tickers with no upcoming earnings map to None.
    """
    tickers = [t.upper() for t in (tickers or settings.WATCHLIST)]
    api_key = api_key or settings.ALPHA_VANTAGE_API_KEY

    cutoff = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).date()
    today = datetime.now(timezone.utc).date()

    try:
        resp = requests.get(
            _AV_URL,
            params={
                "function": "EARNINGS_CALENDAR",
                "horizon":  "3month",
                "apikey":   api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()

        # AV returns CSV for this endpoint
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
    except Exception as exc:
        logger.error("AV EARNINGS_CALENDAR failed: %s", exc)
        return {t: None for t in tickers}

    # Check for API error responses embedded in the text
    if not rows or "Information" in resp.text[:200] or "Note" in resp.text[:200]:
        logger.warning("AV earnings: API limit or unexpected response")
        return {t: None for t in tickers}

    ticker_set = set(tickers)
    result: dict[str, str | None] = {t: None for t in tickers}

    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if sym not in ticker_set:
            continue
        date_str = row.get("reportDate", "")
        try:
            report_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= report_date <= cutoff:
            result[sym] = date_str
            logger.info("Earnings imminent — %s reports on %s", sym, date_str)

    reported = {k: v for k, v in result.items() if v}
    if reported:
        logger.info("Tickers with upcoming earnings (%dd window): %s", horizon_days, list(reported.keys()))
    else:
        logger.info("No watchlist earnings in the next %d days", horizon_days)

    return result


def earnings_catalyst_scores(
    tickers: list[str] | None = None,
    api_key: str | None = None,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
) -> dict[str, float]:
    """
    Return a catalyst modifier for each ticker.

    Value:
      +0.15 if earnings are within `horizon_days` (amplifies existing signal)
       0.0  otherwise

    This score is NOT directional — it is added to the abs(blended_score)
    check in the scanner so that earnings-week moves cross the threshold
    more easily.
    """
    dates = fetch_earnings_dates(tickers, api_key, horizon_days)
    return {ticker: (0.15 if date is not None else 0.0) for ticker, date in dates.items()}
