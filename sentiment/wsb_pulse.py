"""
WallStreetBets crowd attention signal via ApeWisdom.

Free, no API key required. Data refreshed every ~30 minutes.
One request returns the top 50 mentioned tickers on r/wallstreetbets.

Use case: detect when our watchlist tickers are trending on WSB — rising
crowd attention is a mild leading indicator of momentum (not fundamentals).
"""

import logging

import requests

logger = logging.getLogger(__name__)

_URL = "https://apewisdom.io/api/v1.0/filter/wallstreetbets"


def fetch_wsb_scores(tickers: list[str]) -> dict[str, float]:
    """
    Return a crowd attention score in [-1.0, +1.0] per ticker.

    Score logic (based on mentions_now / mentions_24h_ago ratio and rank):
      +1.0  → viral (>2× mention growth, rank ≤ 10)
      +0.5  → trending (>1.5× growth, rank ≤ 20)
      +0.2  → mild uptick (>1.2× growth)
       0.0  → not in top 50 or no meaningful change
      -0.2  → fading (<0.5× mentions vs yesterday)

    Weighted at 5% in the final blend, so max contribution is ±0.05 to the
    blended score — a tiebreaker, not a primary driver.
    """
    ticker_set = {t.upper() for t in tickers}
    scores = {t: 0.0 for t in tickers}

    try:
        resp = requests.get(_URL, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        logger.warning("ApeWisdom WSB fetch failed: %s", exc)
        return scores

    for item in results:
        sym = (item.get("ticker") or "").upper()
        if sym not in ticker_set:
            continue

        mentions_now = int(item.get("mentions", 0) or 0)
        mentions_24h = int(item.get("mentions_24h_ago", 1) or 1)
        rank = int(item.get("rank", 999) or 999)
        change_ratio = mentions_now / max(mentions_24h, 1)

        if change_ratio >= 2.0 and rank <= 10:
            score = 1.0
        elif change_ratio >= 1.5 and rank <= 20:
            score = 0.5
        elif change_ratio >= 1.2:
            score = 0.2
        elif change_ratio < 0.5:
            score = -0.2
        else:
            score = 0.0

        scores[sym] = round(score, 4)
        if score != 0:
            logger.info(
                "WSB %s: %d→%d mentions (rank %d, ratio %.2f) → %.1f",
                sym, mentions_24h, mentions_now, rank, change_ratio, score,
            )

    return scores
