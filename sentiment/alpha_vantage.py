"""
Alpha Vantage News Sentiment scanner.

One API call fetches news + built-in sentiment for ALL watched tickers.
Response includes per-ticker relevance and sentiment scores (-1 to +1).

Free tier: 25 requests/day — easily covers 2 scans/day for 10 tickers.
"""

import logging

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

# Alpha Vantage sentiment label → numeric score mapping
_LABEL_SCORES = {
    "Bullish":           0.75,
    "Somewhat-Bullish":  0.35,
    "Neutral":           0.0,
    "Somewhat-Bearish": -0.35,
    "Bearish":          -0.75,
}


def fetch_ticker_sentiments(
    tickers: list[str] | None = None,
    api_key: str | None = None,
) -> dict[str, float]:
    """
    Fetch Alpha Vantage news sentiment for a list of tickers.

    Returns a dict mapping ticker → weighted average sentiment score.
    Tickers with no news return 0.0 (neutral).

    Score range: -1.0 (very bearish) to +1.0 (very bullish).
    """
    tickers = tickers or settings.WATCHLIST
    api_key = api_key or settings.ALPHA_VANTAGE_API_KEY

    # One call for all tickers — AV accepts comma-separated list
    ticker_str = ",".join(t.upper() for t in tickers)

    params = {
        "function": "NEWS_SENTIMENT",
        "tickers":  ticker_str,
        "limit":    200,
        "sort":     "LATEST",
        "apikey":   api_key,
    }

    resp = requests.get(
        "https://www.alphavantage.co/query", params=params, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()

    if "Information" in data:
        logger.warning("Alpha Vantage API limit hit: %s", data["Information"])
        return {}

    if "Note" in data:
        logger.warning("Alpha Vantage rate limit note: %s", data["Note"])
        return {}

    articles = data.get("feed", [])
    logger.info("Alpha Vantage: %d articles fetched for %s", len(articles), ticker_str)

    # Accumulate weighted scores per ticker
    # Weight = ticker relevance_score (0–1) so highly relevant articles count more
    scores: dict[str, list[float]] = {t.upper(): [] for t in tickers}

    for article in articles:
        for ts in article.get("ticker_sentiment", []):
            ticker = ts.get("ticker", "").upper()
            if ticker not in scores:
                continue
            relevance = float(ts.get("relevance_score", 0))
            if relevance < 0.1:
                continue  # skip tangential mentions

            raw_score = ts.get("ticker_sentiment_score")
            if raw_score is not None:
                score = float(raw_score)
            else:
                label = ts.get("ticker_sentiment_label", "Neutral")
                score = _LABEL_SCORES.get(label, 0.0)

            # Weight each article's score by relevance
            scores[ticker].append(score * relevance)

    result: dict[str, float] = {}
    for ticker, weighted in scores.items():
        if weighted:
            result[ticker] = round(sum(weighted) / len(weighted), 4)
        else:
            result[ticker] = 0.0

    for ticker, score in result.items():
        logger.info("AV sentiment — %s: %.4f", ticker, score)

    return result
