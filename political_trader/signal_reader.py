# NEW: Political signal reader: news sentiment, polling momentum, market momentum
"""
Integrates three signal sources for political market entry decisions:
1. Claude-scored political news sentiment
2. FiveThirtyEight polling momentum
3. Kalshi market momentum (smart money signal)
"""

import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import re

import requests
from anthropic import Anthropic

from .models import PoliticalSignal
from .strategy import (
    SIGNAL_WEIGHTS,
    FIVETHIRTYEIGHT_BASE_URL,
    REALCLEARPOLITICS_BASE_URL,
    POLLING_LOOKBACK_DAYS,
    MARKET_MOMENTUM_WINDOW_HOURS,
)

logger = logging.getLogger(__name__)

# NEW: Anthropic client for signal assessment
client = Anthropic()


class PoliticalSignalReader:
    """
    Reads and combines political market signals from multiple sources.
    """

    def __init__(self, kalshi_client=None, newsapi_key: Optional[str] = None):
        """
        Args:
            kalshi_client: Shared carpet_bagger.kalshi_client.KalshiClient instance
            newsapi_key: NewsAPI key for fetching headlines
        """
        self.kalshi_client = kalshi_client
        self.newsapi_key = newsapi_key
        self.session = requests.Session()

    # ========================================================================
    # NEWS SENTIMENT SIGNAL
    # ========================================================================

    def get_news_signal(
        self, market_title: str, resolution_date: str, candidate_or_party: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch recent political headlines from NewsAPI and score via Claude Sonnet.

        Args:
            market_title: e.g., "Democrats win Senate majority — 2026"
            resolution_date: ISO date string for market resolution
            candidate_or_party: Optional filter (e.g., "Joe Biden", "Republican")

        Returns:
            {
                "score": float [-1.0, +1.0],
                "confidence": float [0.0, 1.0],
                "summary": str,
                "raw_headlines": list
            }
        """
        if not self.newsapi_key:
            logger.warning("NewsAPI key not configured; returning neutral signal")
            return {
                "score": 0.0,
                "confidence": 0.0,
                "summary": "No news API configured",
                "raw_headlines": [],
            }

        # NEW: Extract query terms from market title
        query_terms = self._extract_political_query(market_title, candidate_or_party)

        try:
            # NEW: Fetch recent headlines
            headlines = self._fetch_political_headlines(query_terms)
            if not headlines:
                logger.warning(f"No headlines found for {query_terms}")
                return {
                    "score": 0.0,
                    "confidence": 0.0,
                    "summary": "No recent headlines found",
                    "raw_headlines": [],
                }

            # NEW: Score via Claude Sonnet
            result = self._score_headlines_via_claude(
                market_title, resolution_date, headlines
            )
            result["raw_headlines"] = headlines[:3]  # Include top 3 for audit trail
            return result

        except Exception as e:
            logger.error(f"Error reading news signal: {e}", exc_info=True)
            return {
                "score": 0.0,
                "confidence": 0.0,
                "summary": f"Error: {str(e)}",
                "raw_headlines": [],
            }

    def _extract_political_query(self, market_title: str, candidate_or_party: Optional[str]) -> str:
        """Extract search query from market title and optional candidate/party."""
        # NEW: Simple extraction; could use Claude Haiku for complex titles
        if candidate_or_party:
            return candidate_or_party
        # Extract key words (Senate, House, President, etc.)
        match = re.search(r"(\w+\s+\w*(?:election|vote|control|wins|seat))", market_title, re.I)
        if match:
            return match.group(1)
        return market_title[:50]  # Fallback to first part of title

    def _fetch_political_headlines(self, query: str, lookback_days: int = 7) -> list:
        """
        Fetch recent political headlines from NewsAPI.
        NEW: Filtered to politics/election/policy categories.
        """
        try:
            # NEW: Use NewsAPI with political filters
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 20,
                "apiKey": self.newsapi_key,
                "from": (datetime.utcnow() - timedelta(days=lookback_days)).isoformat(),
            }

            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            headlines = []
            for article in data.get("articles", []):
                headlines.append(
                    {
                        "title": article.get("title", ""),
                        "description": article.get("description", ""),
                        "source": article.get("source", {}).get("name", ""),
                        "published_at": article.get("publishedAt", ""),
                    }
                )
            return headlines

        except Exception as e:
            logger.error(f"NewsAPI fetch error: {e}")
            return []

    def _score_headlines_via_claude(
        self, market_title: str, resolution_date: str, headlines: list
    ) -> Dict[str, Any]:
        """
        Use Claude Sonnet to score political news sentiment.
        NEW: Political-specific prompt with framing on who gains/loses.
        """
        try:
            # NEW: Format headlines for Claude
            headlines_text = "\n".join(
                [
                    f"- {h['title']} ({h['source']}, {h['published_at'][:10]})"
                    for h in headlines
                ]
            )

            prompt = f"""You are scoring political news sentiment for a prediction market trader.

Market: {market_title}
Resolution: {resolution_date}

Recent headlines:
{headlines_text}

Score the current news momentum on a scale of -1.0 to +1.0:
- -1.0 = news strongly favors NO outcome (strong negative signal)
- 0.0 = neutral or mixed signals
- +1.0 = news strongly favors YES outcome (strong positive signal)

Also rate your confidence in this assessment (0.0 = no confidence, 1.0 = very confident).

Focus on: Which party/candidate is gaining momentum? What policy developments help/hurt?

Return a JSON object with exactly these fields:
{{
  "score": <float between -1.0 and 1.0>,
  "confidence": <float between 0.0 and 1.0>,
  "summary": "<one sentence summary of the political momentum>"
}}"""

            message = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )

            # NEW: Parse Claude's JSON response
            response_text = message.content[0].text
            json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                # NEW: Validate bounds
                result["score"] = max(-1.0, min(1.0, float(result.get("score", 0.0))))
                result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
                return result
            else:
                logger.warning("Could not parse Claude response as JSON")
                return {"score": 0.0, "confidence": 0.0, "summary": "Parse error"}

        except Exception as e:
            logger.error(f"Claude scoring error: {e}", exc_info=True)
            return {"score": 0.0, "confidence": 0.0, "summary": f"Error: {str(e)}"}

    # ========================================================================
    # POLLING MOMENTUM SIGNAL
    # ========================================================================

    def get_polling_momentum(
        self, candidate_or_party: str, race: str
    ) -> Optional[float]:
        """
        Fetch polling averages and calculate 7-day momentum.
        NEW: Supports US presidential, Senate, gubernatorial races.

        Args:
            candidate_or_party: e.g., "Joe Biden" or "Democrats"
            race: e.g., "president", "senate", "governor"

        Returns:
            Normalized momentum [-1.0, +1.0] or None if no data found.
            ±10 polling points = ±1.0
        """
        try:
            # NEW: Try multiple polling APIs
            momentum = self._get_fivethirtyeight_momentum(candidate_or_party, race)
            if momentum is not None:
                return momentum

            momentum = self._get_realclearpolitics_momentum(candidate_or_party)
            if momentum is not None:
                return momentum

            logger.warning(f"No polling data found for {candidate_or_party} {race}")
            return None

        except Exception as e:
            logger.error(f"Polling momentum error: {e}")
            return None

    def _get_fivethirtyeight_momentum(self, candidate_or_party: str, race: str) -> Optional[float]:
        """NEW: Fetch FiveThirtyEight polling average and calculate momentum."""
        try:
            # NEW: Map race type to FiveThirtyEight URL
            if race.lower() == "president":
                url = f"{FIVETHIRTYEIGHT_BASE_URL}president-general/2026/"
            elif race.lower() == "senate":
                url = f"{FIVETHIRTYEIGHT_BASE_URL}senate/2026/"
            elif race.lower() == "governor":
                url = f"{FIVETHIRTYEIGHT_BASE_URL}gubernatorial/2026/"
            else:
                return None

            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()

            # NEW: FiveThirtyEight returns HTML; extract JSON from page
            # This is a simplified approach; real implementation would parse HTML
            # or use their unofficial API if available
            logger.debug(f"FiveThirtyEight fetch successful for {race}")

            # Placeholder: would need HTML parsing or API access
            # Return None for now; production would integrate properly
            return None

        except Exception as e:
            logger.debug(f"FiveThirtyEight fetch failed: {e}")
            return None

    def _get_realclearpolitics_momentum(self, candidate_or_party: str) -> Optional[float]:
        """NEW: Fetch RealClearPolitics polling average."""
        try:
            # NEW: RealClearPolitics API endpoint
            url = f"{REALCLEARPOLITICS_BASE_URL}rcp_poll_average.json"
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # NEW: Search for candidate/party in polling data
            # RCP returns list of polling results; calculate 7-day momentum
            candidate_key = candidate_or_party.lower()

            for poll_entry in data:
                if candidate_key in poll_entry.get("name", "").lower():
                    # Calculate momentum between current and 7 days ago
                    current = float(poll_entry.get("average", 0.0))
                    previous = float(poll_entry.get("7day_ago", current))
                    momentum_points = current - previous

                    # Normalize to [-1.0, +1.0]: ±10 points = ±1.0
                    normalized = momentum_points / 10.0
                    return max(-1.0, min(1.0, normalized))

            logger.debug(f"Candidate {candidate_or_party} not found in RCP data")
            return None

        except Exception as e:
            logger.debug(f"RCP fetch failed: {e}")
            return None

    # ========================================================================
    # MARKET MOMENTUM SIGNAL
    # ========================================================================

    def get_market_momentum(
        self, market_ticker: str
    ) -> Optional[float]:
        """
        Fetch Kalshi market history and calculate 24h price momentum.
        NEW: Smart money signal—has market moved toward YES or NO recently?

        Args:
            market_ticker: e.g., "USCASEN23-D"

        Returns:
            Normalized momentum [-1.0, +1.0] or None if unavailable.
            +0.5 = 5 cent move toward YES = moderate bullish
            -0.5 = 5 cent move toward NO = moderate bearish
        """
        if not self.kalshi_client:
            logger.debug("Kalshi client not available; skipping market momentum")
            return None

        try:
            # NEW: Fetch current market state
            current_market = self.kalshi_client.get_market(market_ticker)
            if not current_market:
                logger.warning(f"Market not found: {market_ticker}")
                return None

            current_yes_price = float(current_market.get("yes_ask", 0.50))

            # NEW: Fetch 24-hour history
            # (assumes kalshi_client has market_history method or similar)
            history = self.kalshi_client.get_market_history(
                market_ticker, lookback_hours=MARKET_MOMENTUM_WINDOW_HOURS
            )

            if not history or len(history) < 2:
                logger.debug(f"Insufficient history for {market_ticker}")
                return None

            # NEW: Get price from 24 hours ago
            oldest_entry = history[0]
            historical_yes_price = float(oldest_entry.get("yes_bid", 0.50))

            # NEW: Calculate price change
            price_change = current_yes_price - historical_yes_price

            # Normalize: ±10 cents = ±1.0 (very strong move)
            normalized = price_change / 0.10
            return max(-1.0, min(1.0, normalized))

        except Exception as e:
            logger.error(f"Market momentum error for {market_ticker}: {e}")
            return None

    # ========================================================================
    # COMBINED SIGNAL & IMPLIED PROBABILITY
    # ========================================================================

    def calculate_combined_signal(
        self, news: float, polling: Optional[float], momentum: Optional[float]
    ) -> float:
        """
        Weighted combination of signal sources.
        NEW: Handles missing signals gracefully (polling/momentum may be unavailable).

        Args:
            news: [-1.0, +1.0] news sentiment
            polling: [-1.0, +1.0] or None
            momentum: [-1.0, +1.0] or None

        Returns:
            Combined signal [-1.0, +1.0]
        """
        weights = SIGNAL_WEIGHTS.copy()
        total_weight = 0.0
        weighted_sum = 0.0

        # NEW: Always include news
        weighted_sum += news * weights["news_sentiment"]
        total_weight += weights["news_sentiment"]

        # NEW: Include polling if available
        if polling is not None:
            weighted_sum += polling * weights["polling_momentum"]
            total_weight += weights["polling_momentum"]
        else:
            logger.debug("Polling data unavailable; reducing polling weight")

        # NEW: Include market momentum if available
        if momentum is not None:
            weighted_sum += momentum * weights["market_momentum"]
            total_weight += weights["market_momentum"]
        else:
            logger.debug("Market momentum unavailable; reducing momentum weight")

        # NEW: Normalize by available weights
        if total_weight > 0:
            combined = weighted_sum / total_weight
        else:
            combined = 0.0

        return max(-1.0, min(1.0, combined))

    def get_implied_probability(self, combined_signal: float) -> float:
        """
        Convert combined signal to implied YES probability.
        NEW: Linear mapping with 50% baseline.

        Args:
            combined_signal: [-1.0, +1.0]

        Returns:
            Probability [0.0, 1.0] (fair value for YES outcome)
        """
        # NEW: Mapping: signal -1.0 -> 10%, 0.0 -> 50%, +1.0 -> 90%
        implied_prob = 0.50 + (combined_signal * 0.40)
        return max(0.0, min(1.0, implied_prob))

    # ========================================================================
    # COMPLETE SIGNAL ASSESSMENT
    # ========================================================================

    def assess_market(
        self,
        market_ticker: str,
        market_title: str,
        series: str,
        resolution_date: str,
        current_yes_price: float,
        candidate_or_party: Optional[str] = None,
    ) -> PoliticalSignal:
        """
        Comprehensive signal assessment for a market.
        NEW: Integrates news, polling, market momentum; calculates edge.

        Args:
            market_ticker: e.g., "USCASEN23-D"
            market_title: Full market description
            series: Series code (e.g., "KXSENATE")
            resolution_date: ISO date string
            current_yes_price: Current Kalshi YES price
            candidate_or_party: Optional lookup hint

        Returns:
            PoliticalSignal object with complete assessment
        """
        logger.info(f"Assessing market: {market_ticker} ({market_title})")

        # NEW: Get all three signals
        news_result = self.get_news_signal(market_title, resolution_date, candidate_or_party)
        news_signal = news_result.get("score", 0.0)
        news_confidence = news_result.get("confidence", 0.0)
        news_summary = news_result.get("summary", "")

        polling_momentum = self.get_polling_momentum(
            candidate_or_party or market_title, "senate"
        )  # TODO: detect race type

        market_momentum = self.get_market_momentum(market_ticker)

        # NEW: Calculate combined signal and implied probability
        combined_signal = self.calculate_combined_signal(news_signal, polling_momentum, market_momentum)
        implied_prob = self.get_implied_probability(combined_signal)

        # NEW: Calculate edge (fair value - market price)
        edge = (implied_prob - current_yes_price)

        return PoliticalSignal(
            market_ticker=market_ticker,
            market_title=market_title,
            series=series,
            resolution_date=resolution_date,
            news_signal=news_signal,
            news_confidence=news_confidence,
            news_summary=news_summary,
            polling_momentum=polling_momentum,
            polling_summary=None,  # Could enhance with polling summary
            market_momentum=market_momentum,
            market_momentum_direction="up" if (market_momentum or 0) > 0 else "down",
            combined_signal=combined_signal,
            implied_probability=implied_prob,
            edge_vs_market=edge,
            current_yes_price=current_yes_price,
            current_no_price=1.0 - current_yes_price,
            bid_ask_spread=0.02,  # TODO: fetch actual spread
            recommendation="BUY_YES" if edge > 0 else "BUY_NO" if edge < 0 else "HOLD",
        )
