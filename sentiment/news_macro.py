"""
Macro sentiment via NewsAPI top-headlines + Claude.

Uses /v2/top-headlines (business category) which is available on the
NewsAPI free/developer tier. Falls back to Claude's own market knowledge
if headlines cannot be fetched.

Claude returns:
  - market_sentiment: float  (-1.0 very bearish → +1.0 very bullish)
  - key_events:       list   short bullets of what's driving sentiment
  - summary:          str    one-sentence market mood
"""

import json
import logging

import anthropic
import requests

from config.settings import settings

logger = logging.getLogger(__name__)

_CLAUDE_SYSTEM = """You are a professional financial analyst assessing short-term US equity market sentiment.

If news headlines are provided, base your assessment on them.
If no headlines are provided, use your training knowledge of recent macro trends.

Respond ONLY with a JSON object — no markdown, no prose:

{
  "market_sentiment": <float between -1.0 and 1.0>,
  "key_events": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],
  "summary": "<one sentence>"
}

-1.0 = extremely bearish (crash, war, major crisis)
 0.0 = neutral / mixed
+1.0 = extremely bullish (strong earnings, rate cuts, peace deal)"""


def fetch_macro_headlines(api_key: str | None = None) -> list[str]:
    """
    Pull top business headlines from NewsAPI.
    Uses /v2/top-headlines which works on the free developer tier.
    Returns an empty list on failure (Claude will still run without headlines).
    """
    api_key = api_key or settings.NEWS_API_KEY
    if not api_key:
        return []

    headlines: list[str] = []
    seen: set[str] = set()

    # Pull from business + general categories
    for category in ("business", "general"):
        try:
            resp = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "category": category,
                    "language": "en",
                    "pageSize": 20,
                    "apiKey":   api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            for article in resp.json().get("articles", []):
                title = (article.get("title") or "").strip()
                if title and title not in seen and title != "[Removed]":
                    seen.add(title)
                    headlines.append(title)
        except Exception as exc:
            logger.warning("NewsAPI top-headlines (%s) failed: %s", category, exc)

    logger.info("NewsAPI: %d top headlines fetched", len(headlines))
    return headlines


def score_macro_sentiment(
    headlines: list[str],
    anthropic_api_key: str | None = None,
) -> dict:
    """
    Pass headlines to Claude for market sentiment scoring.
    If headlines is empty, Claude uses its own knowledge.

    Returns:
        {
            "market_sentiment": float,
            "key_events":       list[str],
            "summary":          str,
        }
    """
    api_key = anthropic_api_key or settings.ANTHROPIC_API_KEY
    client = anthropic.Anthropic(api_key=api_key)

    if headlines:
        headlines_text = "\n".join(f"- {h}" for h in headlines[:60])
        user_content = f"Recent news headlines:\n\n{headlines_text}"
    else:
        user_content = (
            "No live headlines available. Please assess current US equity market "
            "sentiment based on recent macro trends, Fed policy, geopolitical events, "
            "and any major economic developments you are aware of."
        )

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        result["market_sentiment"] = max(-1.0, min(1.0, float(result["market_sentiment"])))
        logger.info(
            "Claude macro sentiment: %.3f — %s",
            result["market_sentiment"],
            result.get("summary", ""),
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("Claude returned non-JSON: %s", exc)
        return {"market_sentiment": 0.0, "key_events": [], "summary": "Parse error."}
    except Exception as exc:
        logger.error("Claude API error: %s", exc)
        return {"market_sentiment": 0.0, "key_events": [], "summary": f"API error: {exc}"}


def get_macro_sentiment() -> dict:
    """Convenience wrapper: fetch headlines then score them."""
    headlines = fetch_macro_headlines()
    return score_macro_sentiment(headlines)


_CLAUDE_TICKER_SYSTEM = """You are a short-term equity trader. Some market data sources are unavailable.
Based on intraday price movements and any macro context provided, give a short-term sentiment
score for each ticker.

Consider: is the move company-specific or macro-driven? Is it a continuation or overreaction?

Respond ONLY with a JSON object — no markdown, no prose:
{
  "AAPL": <float -1.0 to 1.0>,
  "TSLA": <float -1.0 to 1.0>
}

-1.0 = very bearish (strong sell signal)
 0.0 = neutral
+1.0 = very bullish (strong buy signal)

Only include tickers from the input list. Do not add commentary."""


def score_tickers_from_prices(
    price_moves: dict[str, float],
    macro_summary: str = "",
    anthropic_api_key: str | None = None,
) -> dict[str, float]:
    """
    When external data sources (Finnhub, MarketAux) are unavailable, ask Claude
    to assess individual ticker sentiment based on intraday price moves.

    Args:
        price_moves:   {ticker: intraday_change_pct}  e.g. {"AAPL": -2.1, "TSLA": 3.4}
        macro_summary: one-sentence macro context from the regular Claude macro call
        anthropic_api_key: override API key

    Returns:
        {ticker: score in [-1.0, +1.0]}  — empty dict on failure
    """
    if not price_moves:
        return {}

    api_key = anthropic_api_key or settings.ANTHROPIC_API_KEY
    if not api_key:
        return {}

    moves_text = "\n".join(
        f"{t}: {'+' if v >= 0 else ''}{v:.2f}%" for t, v in sorted(price_moves.items())
    )
    macro_line = f"\nMacro context: {macro_summary}" if macro_summary else ""
    user_content = (
        f"Today's intraday price moves:{macro_line}\n\n{moves_text}\n\n"
        f"Score each ticker's short-term sentiment:"
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_CLAUDE_TICKER_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        scores = {
            t.upper(): max(-1.0, min(1.0, float(v)))
            for t, v in result.items()
            if isinstance(v, (int, float))
        }
        logger.info("Claude ticker scores (%d tickers): %s", len(scores), scores)
        return scores
    except json.JSONDecodeError as exc:
        logger.error("Claude ticker score returned non-JSON: %s", exc)
        return {}
    except Exception as exc:
        logger.error("Claude ticker score failed: %s", exc)
        return {}
