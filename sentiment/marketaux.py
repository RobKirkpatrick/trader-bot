"""
MarketAux financial news with entity-level sentiment scores.

Free tier: 100 requests/day — covers 10 tickers × 3 scans = 30 req/day comfortably.
Each article returns per-entity sentiment_score in [-1, +1].
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.marketaux.com/v1/news/all"


def fetch_marketaux_sentiment(
    tickers: list[str], api_key: str | None = None
) -> dict[str, float]:
    """
    Return average entity-level sentiment score [-1, +1] per ticker.

    Pulls articles published in the last NEWS_LOOKBACK_HOURS window.
    Returns {} if API key is not configured.
    """
    api_key = api_key or settings.MARKETAUX_API_KEY
    if not api_key:
        logger.debug("MARKETAUX_API_KEY not set — skipping MarketAux sentiment")
        return {}

    since = (
        datetime.now(timezone.utc) - timedelta(hours=settings.NEWS_LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M")

    scores: dict[str, float] = {}
    for ticker in tickers:
        try:
            resp = requests.get(
                _BASE,
                params={
                    "symbols": ticker,
                    "filter_entities": "true",
                    "language": "en",
                    "published_after": since,
                    "api_token": api_key,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                scores[ticker] = 0.0
                continue

            articles = resp.json().get("data", [])
            if not articles:
                scores[ticker] = 0.0
                continue

            # Collect entity-level sentiment scores for this ticker
            ticker_scores: list[float] = []
            for article in articles:
                for entity in article.get("entities", []):
                    sym = (entity.get("symbol") or "").upper()
                    if sym == ticker.upper():
                        s = entity.get("sentiment_score")
                        if s is not None:
                            ticker_scores.append(float(s))

            if ticker_scores:
                avg = round(sum(ticker_scores) / len(ticker_scores), 4)
                scores[ticker] = avg
                logger.info(
                    "MarketAux %s: %d score(s) from %d article(s) → avg %.3f",
                    ticker, len(ticker_scores), len(articles), avg,
                )
            else:
                scores[ticker] = 0.0

        except Exception as exc:
            logger.warning("MarketAux failed for %s: %s", ticker, exc)
            scores[ticker] = 0.0

    return scores
