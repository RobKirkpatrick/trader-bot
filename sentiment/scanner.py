"""
Multi-source sentiment scanner.

Sources and weights (must sum to 1.0):
  1. Price        (0.50) — Public.com real-time quotes: intraday % move + SPY/QQQ breadth
  2. Finnhub      (0.20) — Pre-computed bullish/bearish % per ticker (free, 60 req/min)
  3. MarketAux    (0.10) — Entity-level sentiment scores from financial news articles
  4. Claude macro (0.10) — NewsAPI headlines scored by Claude Haiku (macro/geopolitical)
  5. Polygon news (0.05) — Per-ticker keyword scoring (supplementary)
  6. WSB pulse    (0.05) — ApeWisdom: rising WSB mentions as crowd attention signal

Earnings modifiers (applied on top of the blended score):
  - AV EARNINGS_CALENDAR: if reporting within 7 days → lower threshold by 0.15
  - Finnhub EPS surprise: most recent quarter beat/miss → ±0.10–0.20 additive to score

Final score range: -1.0 (very bearish) → +1.0 (very bullish)
Signal thresholds defined in config/settings.py (currently ±0.20).
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

import requests

from config.settings import settings
from sentiment.news_macro import get_macro_sentiment, score_tickers_from_prices
from sentiment.market_data import fetch_price_signals
from sentiment.earnings import earnings_catalyst_scores
from sentiment.finnhub_news import fetch_finnhub_sentiment, fetch_finnhub_earnings_surprise
from sentiment.marketaux import fetch_marketaux_sentiment
from sentiment.wsb_pulse import fetch_wsb_scores

logger = logging.getLogger(__name__)

# Blend weights — must sum to 1.0
_W_PRICE     = 0.50
_W_FINNHUB   = 0.20
_W_MARKETAUX = 0.10
_W_MACRO     = 0.10
_W_POLY      = 0.05
_W_WSB       = 0.05

BULLISH_WORDS = {
    "beat", "beats", "surge", "surges", "surging", "record", "growth",
    "bullish", "upgrade", "upgrades", "outperform", "buy", "strong",
    "profit", "profits", "revenue", "raise", "raised", "raises",
    "positive", "gain", "gains", "rally", "rallies", "boost",
    "boosts", "momentum", "opportunity", "breakout", "expansion",
    "upside", "accelerat", "exceed", "exceeds", "exceeded",
}

BEARISH_WORDS = {
    "miss", "misses", "missed", "decline", "declines", "declining",
    "bearish", "downgrade", "downgrades", "underperform", "sell",
    "weak", "loss", "losses", "cut", "cuts", "negative", "drop",
    "drops", "fell", "fall", "falls", "warn", "warns", "warning",
    "lawsuit", "investigation", "recall", "downside", "slowdown",
    "disappoint", "disappoints", "disappointing", "concern", "concerns",
}


@dataclass
class ArticleSentiment:
    ticker: str
    title: str
    published_utc: str
    score: float
    bullish_hits: int
    bearish_hits: int


@dataclass
class TickerSentiment:
    ticker: str
    score: float                    # final blended score
    price_score: float              # Public.com price signal
    macro_score: float              # Claude macro component
    polygon_score: float            # Polygon news keyword component
    finnhub_score: float = 0.0      # Finnhub pre-computed sentiment
    marketaux_score: float = 0.0    # MarketAux entity-level sentiment
    wsb_score: float = 0.0          # ApeWisdom WSB crowd attention
    earnings_surprise: float = 0.0  # Finnhub EPS surprise modifier applied
    av_score: float = 0.0           # kept for schema compatibility (unused)
    article_count: int = 0
    earnings_imminent: bool = False  # reports within 7 days
    signal: str = "neutral"         # "bullish" | "bearish" | "neutral"
    articles: list[ArticleSentiment] = field(default_factory=list)
    macro_events: list[str] = field(default_factory=list)  # top headlines from today's macro scan


class SentimentScanner:
    def __init__(
        self,
        polygon_api_key: str | None = None,
        av_api_key: str | None = None,
        broker_client=None,
    ):
        self._polygon_key   = polygon_api_key or settings.POLYGON_API_KEY
        self._av_key        = av_api_key or settings.ALPHA_VANTAGE_API_KEY
        self._broker_client = broker_client  # PublicClient — reused for quotes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, tickers: list[str] | None = None) -> list[TickerSentiment]:
        """
        Full multi-source scan. Returns results sorted by abs(score) desc.

        Resilience design:
        - Each source is independently fault-tolerant (returns {} on failure).
        - Active weights are renormalized to 1.0 based on which sources returned data.
        - Polygon news has a 60-second time budget to prevent Lambda timeout.
        - When both Finnhub and MarketAux fail, Claude is asked for ticker-level scores.
        """
        scan_start = time.time()
        tickers = [t.upper() for t in (tickers or settings.WATCHLIST)]

        # 1. Price signals — Public.com real-time quotes (1 API call, always available)
        price_scores = self._get_price_scores(tickers)

        # 2. Macro sentiment — NewsAPI headlines → Claude (1 call covers all tickers)
        macro_result  = self._get_macro(tickers)
        macro_score   = macro_result.get("market_sentiment", 0.0)
        macro_summary = macro_result.get("summary", "")
        macro_events  = macro_result.get("key_events", [])
        logger.info("Macro score: %.3f | %s", macro_score, macro_summary)
        if macro_events:
            logger.info("Key events: %s", " | ".join(macro_events[:3]))

        # 3. Finnhub sentiment (pre-computed, fast — 60 req/min limit)
        finnhub_scores = self._get_finnhub_scores(tickers)

        # 4. MarketAux entity-level sentiment (100 req/day — exhausts around scan 4)
        marketaux_scores = self._get_marketaux_scores(tickers)

        # 5. WSB crowd attention (1 call, no key required)
        wsb_scores = self._get_wsb_scores(tickers)

        # 6. Finnhub EPS surprise — directional modifier (fast)
        earn_surprises = self._get_earnings_surprise(tickers)

        # 7. AV earnings calendar — threshold modifier (1 call)
        catalyst = self._get_earnings_catalyst(tickers)

        # 8. Polygon news per-ticker — slowest source (13s delay × N tickers).
        #    Budget: at most 60s so we don't timeout the Lambda.
        elapsed = time.time() - scan_start
        poly_budget = max(0, 60 - int(elapsed))
        polygon_scores = self._get_polygon_scores(tickers, time_budget=poly_budget)

        # 9. Determine which sources returned real data (non-empty, non-all-zero)
        def _has_data(d: dict) -> bool:
            return bool(d) and any(v != 0.0 for v in d.values())

        sources_active = {
            "finnhub":   _has_data(finnhub_scores),
            "marketaux": _has_data(marketaux_scores),
            "poly":      _has_data(polygon_scores),
            "wsb":       _has_data(wsb_scores),
        }
        failed = [k for k, v in sources_active.items() if not v]
        if failed:
            logger.warning("Sources returned no data (will redistribute weight): %s", failed)

        # 10. Claude ticker fallback: when both Finnhub and MarketAux fail, ask Claude
        #     to score tickers individually based on price moves + macro context.
        claude_ticker_scores: dict[str, float] = {}
        if not sources_active["finnhub"] and not sources_active["marketaux"]:
            logger.info("Finnhub + MarketAux both unavailable — requesting Claude ticker analysis")
            price_moves = {
                t: (price_scores.get(t, 0.0) / 0.35) * 100  # reverse normalize to approx %
                for t in tickers
            }
            claude_ticker_scores = self._get_claude_ticker_scores(price_moves, macro_summary)

        # 11. Build dynamic weights — renormalize to 1.0 based on available sources
        w_finnhub   = _W_FINNHUB   if sources_active["finnhub"]   else 0.0
        w_marketaux = _W_MARKETAUX if sources_active["marketaux"] else 0.0
        w_poly      = _W_POLY      if sources_active["poly"]       else 0.0
        w_wsb       = _W_WSB       if sources_active["wsb"]        else 0.0

        # If Claude ticker analysis ran, allocate it the combined weight of failed sources
        w_claude_ticker = 0.0
        if claude_ticker_scores:
            w_claude_ticker = (
                (0.0 if sources_active["finnhub"]   else _W_FINNHUB)
                + (0.0 if sources_active["marketaux"] else _W_MARKETAUX)
            )
            w_finnhub   = 0.0
            w_marketaux = 0.0

        total_w = _W_PRICE + w_finnhub + w_marketaux + _W_MACRO + w_poly + w_wsb + w_claude_ticker
        if total_w <= 0:
            total_w = 1.0

        # 12. Blend per ticker
        results: list[TickerSentiment] = []
        for ticker in tickers:
            price          = price_scores.get(ticker, 0.0)
            finnhub        = finnhub_scores.get(ticker, 0.0)
            marketaux      = marketaux_scores.get(ticker, 0.0)
            macro          = macro_score
            poly           = polygon_scores.get(ticker, 0.0)
            wsb            = wsb_scores.get(ticker, 0.0)
            claude_t       = claude_ticker_scores.get(ticker, 0.0)
            earn_surprise  = earn_surprises.get(ticker, 0.0)
            earn_boost     = catalyst.get(ticker, 0.0)

            blended = (
                price     * (_W_PRICE / total_w)
                + finnhub   * (w_finnhub / total_w)
                + marketaux * (w_marketaux / total_w)
                + macro     * (_W_MACRO / total_w)
                + poly      * (w_poly / total_w)
                + wsb       * (w_wsb / total_w)
                + claude_t  * (w_claude_ticker / total_w)
            )
            # Directional earnings surprise added as an offset (not weighted)
            blended = round(max(-1.0, min(1.0, blended + earn_surprise)), 4)

            # AV earnings calendar: upcoming earnings → RAISE threshold (reduce exposure to binary event risk)
            effective_buy_threshold  = settings.SENTIMENT_BUY_THRESHOLD  + earn_boost
            effective_sell_threshold = settings.SENTIMENT_SELL_THRESHOLD - earn_boost

            if blended >= effective_buy_threshold:
                signal = "bullish"
            elif blended <= effective_sell_threshold:
                signal = "bearish"
            else:
                signal = "neutral"

            ts = TickerSentiment(
                ticker=ticker,
                score=blended,
                price_score=price,
                macro_score=macro,
                polygon_score=poly,
                finnhub_score=finnhub,
                marketaux_score=marketaux,
                wsb_score=wsb,
                earnings_surprise=earn_surprise,
                earnings_imminent=(earn_boost > 0),
                signal=signal,
                macro_events=macro_events[:5],
            )
            results.append(ts)
            logger.info(
                "%s → %.3f (%s) | pr=%.3f fh=%.3f ma=%.3f mc=%.3f poly=%.3f wsb=%.3f claude=%.3f es=%.3f%s",
                ticker, blended, signal,
                price, finnhub, marketaux, macro, poly, wsb, claude_t, earn_surprise,
                " [EARNINGS]" if earn_boost > 0 else "",
            )

        elapsed_total = time.time() - scan_start
        logger.info("Scan complete in %.1fs | active sources: %s | weights total=%.3f",
                    elapsed_total,
                    [k for k, v in sources_active.items() if v] + (["claude_ticker"] if claude_ticker_scores else []),
                    total_w)

        results.sort(key=lambda t: abs(t.score), reverse=True)
        return results

    def strong_signals(self, tickers: list[str] | None = None) -> list[TickerSentiment]:
        """Return only tickers that cross the buy/sell threshold."""
        return [
            r for r in self.scan(tickers)
            if r.score >= settings.SENTIMENT_BUY_THRESHOLD
            or r.score <= settings.SENTIMENT_SELL_THRESHOLD
        ]

    # ------------------------------------------------------------------
    # Internal source fetchers
    # ------------------------------------------------------------------

    def _get_price_scores(self, tickers: list[str]) -> dict[str, float]:
        try:
            return fetch_price_signals(tickers=tickers, broker_client=self._broker_client)
        except Exception as exc:
            logger.error("Price signals failed: %s", exc)
            return {}

    def _get_macro(self, tickers: list[str]) -> dict:
        try:
            return get_macro_sentiment()
        except Exception as exc:
            logger.error("Macro sentiment failed: %s", exc)
            return {"market_sentiment": 0.0, "key_events": [], "summary": "Macro fetch failed."}

    def _get_finnhub_scores(self, tickers: list[str]) -> dict[str, float]:
        try:
            return fetch_finnhub_sentiment(tickers)
        except Exception as exc:
            logger.error("Finnhub sentiment failed: %s", exc)
            return {}

    def _get_marketaux_scores(self, tickers: list[str]) -> dict[str, float]:
        try:
            return fetch_marketaux_sentiment(tickers)
        except Exception as exc:
            logger.error("MarketAux sentiment failed: %s", exc)
            return {}

    def _get_wsb_scores(self, tickers: list[str]) -> dict[str, float]:
        try:
            return fetch_wsb_scores(tickers)
        except Exception as exc:
            logger.error("WSB pulse failed: %s", exc)
            return {}

    def _get_earnings_surprise(self, tickers: list[str]) -> dict[str, float]:
        try:
            return fetch_finnhub_earnings_surprise(tickers)
        except Exception as exc:
            logger.warning("Finnhub earnings surprise failed: %s", exc)
            return {t: 0.0 for t in tickers}

    def _get_polygon_scores(self, tickers: list[str], time_budget: int = 60) -> dict[str, float]:
        """
        Fetch Polygon per-ticker news scores with a hard time budget.

        Polygon free tier requires a 13s delay between calls (5 req/min).
        With 25 tickers that would take 325s — a guaranteed Lambda timeout.
        We stop early when the time budget is exhausted, returning 0.0 for
        remaining tickers. Polygon is 5% weight so partial coverage is fine.
        """
        scores: dict[str, float] = {}
        start = time.time()
        for i, ticker in enumerate(tickers):
            if i > 0:
                # Check budget before sleeping
                if time.time() - start >= time_budget:
                    logger.warning(
                        "Polygon news: stopping after %d/%d tickers — time budget %ds exceeded",
                        i, len(tickers), time_budget,
                    )
                    for remaining in tickers[i:]:
                        scores[remaining] = 0.0
                    break
                time.sleep(settings.POLYGON_REQUEST_DELAY)
            try:
                score = self._polygon_score_ticker(ticker)
                scores[ticker] = score
            except Exception as exc:
                logger.warning("Polygon news failed for %s: %s", ticker, exc)
                scores[ticker] = 0.0
        return scores

    def _get_claude_ticker_scores(
        self, price_moves: dict[str, float], macro_summary: str = ""
    ) -> dict[str, float]:
        """
        Fallback: when Finnhub + MarketAux are both unavailable, ask Claude to
        score each ticker based on intraday price moves and macro context.
        Returns {} on failure (scan continues with price signal only).
        """
        try:
            return score_tickers_from_prices(
                price_moves=price_moves,
                macro_summary=macro_summary,
            )
        except Exception as exc:
            logger.warning("Claude ticker fallback failed: %s", exc)
            return {}

    def _get_earnings_catalyst(self, tickers: list[str]) -> dict[str, float]:
        try:
            return earnings_catalyst_scores(tickers=tickers, api_key=self._av_key)
        except Exception as exc:
            logger.warning("Earnings catalyst failed: %s", exc)
            return {t: 0.0 for t in tickers}

    def _polygon_score_ticker(self, ticker: str) -> float:
        since = (
            datetime.now(timezone.utc)
            - timedelta(hours=settings.NEWS_LOOKBACK_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = requests.get(
            settings.POLYGON_NEWS_URL,
            params={
                "ticker":               ticker,
                "published_utc.gte":    since,
                "limit":                20,
                "apiKey":               self._polygon_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("results", [])
        if not articles:
            return 0.0

        scores = [self._keyword_score(
            f"{a.get('title','')} {a.get('description','')}"
        ) for a in articles]
        return round(sum(scores) / len(scores), 6)

    def _keyword_score(self, text: str) -> float:
        words = text.lower().split()
        bull = sum(1 for w in words if any(w.startswith(b) for b in BULLISH_WORDS))
        bear = sum(1 for w in words if any(w.startswith(b) for b in BEARISH_WORDS))
        return (bull - bear) / max(len(words), 1)
