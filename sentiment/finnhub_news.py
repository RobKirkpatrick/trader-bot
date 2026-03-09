"""
Finnhub news sentiment + earnings surprises.

Free tier: 60 API calls/minute — no daily cap.

Two endpoints used:
  /news-sentiment  — pre-computed bullishPercent/bearishPercent per ticker
  /stock/earnings  — most recent quarterly EPS actual vs estimate (surprisePercent)
"""

import logging

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


def fetch_finnhub_sentiment(
    tickers: list[str], api_key: str | None = None
) -> dict[str, float]:
    """
    Return a score in [-1, +1] per ticker based on Finnhub's pre-computed
    news sentiment (bullishPercent minus bearishPercent).

    0.0 returned for tickers with no coverage or on API error.
    """
    api_key = api_key or settings.FINNHUB_API_KEY
    if not api_key:
        logger.debug("FINNHUB_API_KEY not set — skipping Finnhub sentiment")
        return {}

    scores: dict[str, float] = {}
    for ticker in tickers:
        try:
            resp = requests.get(
                f"{_BASE}/news-sentiment",
                params={"symbol": ticker, "token": api_key},
                timeout=10,
            )
            if resp.status_code != 200:
                scores[ticker] = 0.0
                continue
            data = resp.json()
            # Finnhub returns fractions (0.0–1.0), not percentages
            bull = float(data.get("bullishPercent") or 0.5)
            bear = float(data.get("bearishPercent") or 0.5)
            score = round(bull - bear, 4)
            scores[ticker] = score
            logger.info(
                "Finnhub sentiment %s: bull=%.1f%% bear=%.1f%% → %.3f",
                ticker, bull * 100, bear * 100, score,
            )
        except Exception as exc:
            logger.warning("Finnhub sentiment failed for %s: %s", ticker, exc)
            scores[ticker] = 0.0

    return scores


def fetch_finnhub_earnings_surprise(
    tickers: list[str], api_key: str | None = None
) -> dict[str, float]:
    """
    Return a directional score modifier in [-0.20, +0.20] based on the most
    recent quarterly EPS surprise.

    Mapping: surprise_pct × 0.02, clamped ±0.20
      +10% beat → +0.20   (max boost)
       +5% beat → +0.10
        0%      →  0.00
       -5% miss → -0.10
      -10% miss → -0.20   (max drag)
    """
    api_key = api_key or settings.FINNHUB_API_KEY
    if not api_key:
        return {t: 0.0 for t in tickers}

    modifiers: dict[str, float] = {}
    for ticker in tickers:
        try:
            resp = requests.get(
                f"{_BASE}/stock/earnings",
                params={"symbol": ticker, "limit": 1, "token": api_key},
                timeout=10,
            )
            if resp.status_code != 200:
                modifiers[ticker] = 0.0
                continue
            data = resp.json()
            if not data:
                modifiers[ticker] = 0.0
                continue
            surprise_pct = float(data[0].get("surprisePercent") or 0)
            modifier = round(max(-0.20, min(0.20, surprise_pct * 0.02)), 4)
            modifiers[ticker] = modifier
            if abs(modifier) > 0.01:
                logger.info(
                    "Finnhub earnings %s: surprise=%.1f%% → modifier %.3f",
                    ticker, surprise_pct, modifier,
                )
        except Exception as exc:
            logger.warning("Finnhub earnings failed for %s: %s", ticker, exc)
            modifiers[ticker] = 0.0

    return modifiers
