"""
Off-hours trade suggestion engine.

Runs at 6:45 PM ET weekdays and 10:00 AM ET Saturdays.

Flow:
  1. Fetch macro headlines (NewsAPI — same source as regular scans)
  2. Optionally fetch current/EOD prices (Public.com — may be unavailable on weekends)
  3. Call Claude Haiku with headlines + prices → 3 specific buy ideas ($2–$5)
  4. Generate HMAC-signed approval URLs (20-hour expiry)
  5. Email the 3 suggestions with one-click approve links

Security:
  - Approval URLs are signed with HMAC-SHA256 using SUGGESTION_TOKEN_SECRET
  - No token database — all data lives in the URL params, validated by HMAC
  - Token expiry enforced via unix timestamp in the URL
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import anthropic
import boto3

from config.settings import settings
from sentiment.news_macro import fetch_macro_headlines

logger = logging.getLogger(__name__)

# Extended ticker universe for suggestions — 80+ liquid US stocks and ETFs
SUGGESTION_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "GOOG", "TSLA", "AMD",
    # Consumer / retail
    "COST", "WMT", "TGT", "AMZN", "NKE", "SBUX", "MCD", "DIS",
    # Financials
    "JPM", "BAC", "GS", "MS", "V", "MA", "PYPL", "AXP",
    # Healthcare & biotech
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "CVS",
    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY",
    # Industrials
    "HON", "CAT", "DE", "BA", "UPS", "FDX",
    # Aerospace & Defense (geopolitical macro plays)
    "ITA", "XAR", "LMT", "RTX", "NOC", "GD", "LDOS",
    # Semiconductors
    "NVDA", "AMD", "INTC", "QCOM", "AVGO", "TSM", "SOXX", "SMH",
    # Broad market ETFs
    "SPY", "QQQ", "IWM", "VOO", "VTI",
    # Sector ETFs
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLRE",
    # Commodities / safe havens (macro / uncertainty plays)
    "GLD", "SLV", "USO", "UNG", "PDBC",
    # Innovation / growth
    "ARKK", "ARKG", "ARKW",
    # International / emerging markets
    "EFA", "EEM", "FXI", "EWJ",
    # Volatility / hedges
    "VXX",
    # Other large-caps worth watching
    "CRM", "NFLX", "ADBE", "ORCL", "NOW", "UBER", "LYFT", "SNAP", "SPOT",
    "ZM", "SHOP", "SQ", "COIN", "MSTR",
]

_SUGGESTION_SYSTEM = """You are a trading assistant for a retail investor with a small brokerage account (~$1,000).

Generate exactly 3 specific, actionable buy suggestions for tonight or this weekend.

Rules:
- Each suggestion must be between $2 and $5
- Prefer divergence plays: stock is down on price but news/sentiment is positive (good entry)
- Include thematic macro plays based on geopolitical or economic events (ETFs are great for this)
- Only suggest liquid US stocks and ETFs — no penny stocks, no crypto ETFs
- Be specific in the rationale: reference the actual news event or price action

Available tickers to choose from:
{universe}

Respond ONLY with a valid JSON array — no markdown, no preamble:
[
  {{"ticker": "AAPL", "rationale": "Apple fell 2.1% today despite strong product event reception. The dip looks like an overreaction.", "dollars": 3.0}},
  {{"ticker": "ITA",  "rationale": "Geopolitical tensions rising in the Middle East — defense ETFs historically outperform in these environments.", "dollars": 5.0}},
  {{"ticker": "NVDA", "rationale": "AI chip demand remains robust per recent analyst reports; modest pullback is a good entry point.", "dollars": 2.0}}
]"""


def _make_approval_token(ticker: str, dollars: float, expires_ts: int, secret: str) -> str:
    """Generate HMAC-SHA256 signature for an approval link."""
    payload = f"{ticker}:{dollars:.2f}:{expires_ts}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_approval_url(ticker: str, dollars: float, function_url: str, secret: str) -> str:
    """Build a signed, time-limited approval URL."""
    expires_ts = int(time.time()) + settings.SUGGESTION_EXPIRY_HOURS * 3600
    token = _make_approval_token(ticker, dollars, expires_ts, secret)
    base = function_url.rstrip("/")
    return (
        f"{base}/approve"
        f"?ticker={ticker}"
        f"&dollars={dollars:.2f}"
        f"&expires={expires_ts}"
        f"&token={token}"
    )


def _make_approve_all_url(suggestions: list[dict], function_url: str, secret: str) -> str:
    """Build a single signed URL that approves all suggestions at once."""
    expires_ts = int(time.time()) + settings.SUGGESTION_EXPIRY_HOURS * 3600
    batch = ",".join(f"{s['ticker']}:{s['dollars']:.2f}" for s in suggestions)
    payload = f"batch:{batch}:{expires_ts}"
    token = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    base = function_url.rstrip("/")
    return f"{base}/approve?batch={batch}&expires={expires_ts}&token={token}"


def generate_suggestions(
    headlines: list[str],
    prices: dict[str, float] | None = None,
) -> list[dict]:
    """
    Call Claude Haiku to generate 3 trade suggestions.

    Args:
        headlines: List of macro news headlines from NewsAPI
        prices:    Optional dict of {ticker: current_price} for context

    Returns:
        List of dicts: [{"ticker": str, "rationale": str, "dollars": float}]
        Returns [] on failure (caller handles gracefully).
    """
    universe_str = ", ".join(SUGGESTION_UNIVERSE)
    system = _SUGGESTION_SYSTEM.format(universe=universe_str)

    # Build user content
    parts = []
    if headlines:
        headlines_text = "\n".join(f"- {h}" for h in headlines[:50])
        parts.append(f"Current news headlines:\n{headlines_text}")
    else:
        parts.append(
            "No live headlines available. Use your knowledge of recent macro events "
            "to suggest timely plays."
        )

    if prices:
        price_lines = "\n".join(f"  {sym}: ${p:.2f}" for sym, p in sorted(prices.items()))
        parts.append(f"\nCurrent/recent prices:\n{price_lines}")

    now_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    parts.append(f"\nToday's date: {now_str}")

    user_content = "\n".join(parts)

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        suggestions = json.loads(raw)

        # Validate and clamp dollars
        result = []
        for s in suggestions[:3]:
            ticker  = str(s.get("ticker", "")).upper().strip()
            dollars = float(s.get("dollars", settings.SUGGESTION_DOLLARS_DEFAULT))
            dollars = round(max(2.0, min(5.0, dollars)), 2)
            rationale = str(s.get("rationale", "")).strip()
            if ticker and rationale:
                result.append({"ticker": ticker, "rationale": rationale, "dollars": dollars})

        logger.info("Claude generated %d suggestions: %s", len(result), [s["ticker"] for s in result])
        return result

    except json.JSONDecodeError as exc:
        logger.error("Claude suggestions returned non-JSON: %s", exc)
        return []
    except Exception as exc:
        logger.error("Claude suggestions failed: %s", exc)
        return []


def _build_suggestion_email(
    suggestions: list[dict],
    function_url: str,
    secret: str,
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")
    lines = [
        "TraderBot — Evening Suggestions",
        now_str,
        "─" * 42,
        "Market is closed. Here are tonight's 3 trade ideas:",
        "",
    ]

    for i, s in enumerate(suggestions, 1):
        ticker   = s["ticker"]
        dollars  = s["dollars"]
        rationale = s["rationale"]
        url = _make_approval_url(ticker, dollars, function_url, secret) if function_url and secret else "(approval URL unavailable)"

        lines += [
            "━" * 42,
            f"{i}. {ticker} — ${dollars:.2f}",
            rationale,
            "",
            f"→ Approve: {url}",
            "",
        ]

    # Approve-all link
    if function_url and secret and len(suggestions) > 1:
        all_url = _make_approve_all_url(suggestions, function_url, secret)
        tickers = ", ".join(s["ticker"] for s in suggestions)
        total   = sum(s["dollars"] for s in suggestions)
        lines += [
            "━" * 42,
            f"→ Approve All ({tickers} — ${total:.2f} total): {all_url}",
            "",
        ]

    lines += [
        "━" * 42,
        f"Links expire in {settings.SUGGESTION_EXPIRY_HOURS} hours.",
        "Orders queue until market open if clicked after hours.",
    ]
    return "\n".join(lines)


def run_suggestions_scan() -> dict:
    """
    Main entry point for the off-hours suggestion window.

    Called by lambda_function.handler() when window == "suggestions".
    Also callable locally for testing.
    """
    from scheduler.jobs import _publish_sns  # avoid circular import

    logger.info("=== Evening Suggestions scan starting ===")

    # 1. Fetch macro headlines
    try:
        headlines = fetch_macro_headlines()
        logger.info("Fetched %d headlines for suggestions", len(headlines))
    except Exception as exc:
        logger.warning("Headlines fetch failed: %s", exc)
        headlines = []

    # 2. Optionally fetch current prices (graceful — fails on weekends/after hours)
    prices: dict[str, float] = {}
    try:
        from broker.public_client import PublicClient
        from sentiment.market_data import fetch_price_signals
        client = PublicClient()
        quotes = client.get_quotes(SUGGESTION_UNIVERSE[:30])  # limit to avoid quota
        for q in quotes.get("quotes", []):
            sym = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
            raw = q.get("last") or q.get("lastPrice") or q.get("price")
            if sym and raw:
                try:
                    prices[sym] = float(raw)
                except (ValueError, TypeError):
                    pass
        logger.info("Fetched %d current prices for suggestion context", len(prices))
    except Exception as exc:
        logger.info("Price fetch skipped (off-hours): %s", exc)

    # 3. Generate suggestions via Claude
    suggestions = generate_suggestions(headlines, prices or None)

    if not suggestions:
        logger.warning("No suggestions generated — skipping email")
        return {"window": "suggestions", "suggestions": 0}

    # 4. Build and send email
    function_url = settings.LAMBDA_FUNCTION_URL
    secret       = settings.SUGGESTION_TOKEN_SECRET

    if not function_url:
        logger.warning("LAMBDA_FUNCTION_URL not set — approval links will be unavailable")
    if not secret:
        logger.warning("SUGGESTION_TOKEN_SECRET not set — approval links will be unsigned")

    message = _build_suggestion_email(suggestions, function_url, secret)
    subject = f"[TraderBot] Tonight's 3 trade ideas — {datetime.now(timezone.utc).strftime('%b %d')}"
    logger.info(message)

    try:
        _publish_sns(message, subject=subject)
    except Exception as exc:
        logger.error("SNS publish failed for suggestions: %s", exc)

    return {
        "window": "suggestions",
        "suggestions": len(suggestions),
        "tickers": [s["ticker"] for s in suggestions],
    }
