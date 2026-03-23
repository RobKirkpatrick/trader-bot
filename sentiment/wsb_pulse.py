"""
Reddit crowd attention signal via ApeWisdom.

Free, no API key required. Data refreshed every ~30 minutes.
One request returns the top 50 mentioned tickers per subreddit.

Subreddits polled: r/wallstreetbets, r/stocks
A ticker trending on both gets full signal; trending on only one scores half
(divided by total subreddit count, not count that returned data — intentional).

Use case: detect when our watchlist tickers are trending on Reddit — rising
crowd attention is a mild leading indicator of momentum (not fundamentals).
"""

import logging

import requests

logger = logging.getLogger(__name__)

_SUBREDDITS = {
    "wallstreetbets": "https://apewisdom.io/api/v1.0/filter/wallstreetbets",
    "stocks": "https://apewisdom.io/api/v1.0/filter/stocks",
}
_SUBREDDIT_COUNT = len(_SUBREDDITS)


def _score_item(item: dict) -> float:
    mentions_now = int(item.get("mentions", 0) or 0)
    mentions_24h = int(item.get("mentions_24h_ago", 1) or 1)
    rank = int(item.get("rank", 999) or 999)
    change_ratio = mentions_now / max(mentions_24h, 1)

    if change_ratio >= 2.0 and rank <= 10:
        return 1.0
    elif change_ratio >= 1.5 and rank <= 20:
        return 0.5
    elif change_ratio >= 1.2:
        return 0.2
    elif change_ratio < 0.5:
        return -0.2
    return 0.0


def fetch_wsb_scores(tickers: list[str]) -> dict[str, float]:
    """
    Return a crowd attention score in [-1.0, +1.0] per ticker.

    Score logic (based on mentions_now / mentions_24h_ago ratio and rank):
      +1.0  → viral (>2× mention growth, rank ≤ 10)
      +0.5  → trending (>1.5× growth, rank ≤ 20)
      +0.2  → mild uptick (>1.2× growth)
       0.0  → not in top 50 or no meaningful change
      -0.2  → fading (<0.5× mentions vs yesterday)

    Scores from each subreddit are summed then divided by _SUBREDDIT_COUNT (2),
    so a ticker must trend on both to earn full signal.

    Weighted at 5% in the final blend, so max contribution is ±0.05 to the
    blended score — a tiebreaker, not a primary driver.
    """
    ticker_set = {t.upper() for t in tickers}
    raw = {t: 0.0 for t in tickers}

    for subreddit, url in _SUBREDDITS.items():
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            logger.warning("ApeWisdom %s fetch failed: %s", subreddit, exc)
            continue

        for item in results:
            sym = (item.get("ticker") or "").upper()
            if sym not in ticker_set:
                continue

            score = _score_item(item)
            raw[sym] += score
            if score != 0:
                mentions_now = int(item.get("mentions", 0) or 0)
                mentions_24h = int(item.get("mentions_24h_ago", 1) or 1)
                rank = int(item.get("rank", 999) or 999)
                change_ratio = mentions_now / max(mentions_24h, 1)
                logger.info(
                    "r/%s %s: %d→%d mentions (rank %d, ratio %.2f) → %.1f",
                    subreddit, sym, mentions_24h, mentions_now, rank, change_ratio, score,
                )

    return {t: round(raw[t] / _SUBREDDIT_COUNT, 4) for t in tickers}
