"""
Off-hours trade suggestion engine.

Runs at 7:00 PM ET weekdays (5:00 PM MT) and 10:00 AM ET Saturdays.

Flow:
  1. Fetch macro headlines (NewsAPI — same source as regular scans)
  2. Pull today's CloudWatch research log (signals, macro score, what traded)
  3. Optionally fetch current/EOD prices (Public.com — may be unavailable on weekends)
  4. Call Claude Sonnet with the full day's research → 3 specific buy ideas
  5. Generate HMAC-signed approval URLs (20-hour expiry)
  6. Email the research digest + 3 suggestions with one-click approve links

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

_SUGGESTION_SYSTEM = """\
You are a senior trading analyst reviewing a full day of automated market research for a retail investor (~$1,000 account).

Your job:
1. Digest the day's research (macro sentiment, price signals, news, what the bot already traded)
2. Identify the 3 best buy opportunities for tomorrow's open
3. Avoid anything the bot already bought today (listed in the research)

Rules:
- Each suggestion must be between $2 and $5
- Prefer divergence plays: strong fundamentals/news but price hasn't moved yet, or pullback after positive catalyst
- Include thematic macro plays based on the day's geopolitical or economic events
- Only suggest liquid US stocks and ETFs from the universe provided — no penny stocks, no crypto ETFs
- Be specific: reference the actual signal, price action, or news item driving the idea
- Avoid tickers the bot already bought today

Available tickers:
{universe}

Respond ONLY with valid JSON — no markdown, no preamble:
[
  {{"ticker": "AAPL", "rationale": "Fell 2.1% today despite strong product event. Oversold on high WSB volume — good entry.", "dollars": 3.0}},
  {{"ticker": "ITA",  "rationale": "Macro: escalating Iran conflict. Defense ETFs historically outperform. Scanner scored macro -0.62 (bearish) which makes defense a natural hedge.", "dollars": 5.0}},
  {{"ticker": "NVDA", "rationale": "Price signal +0.64, WSB rank 4 with 7x mention surge. Already trading well above threshold — adding here before tomorrow's open.", "dollars": 2.0}}
]"""


def _fetch_todays_research() -> str:
    """
    Pull today's Lambda CloudWatch logs and extract the trading research digest:
    macro score, top signals, what was traded, stop-losses triggered.

    Returns a plain-text summary string for Claude's context.
    Falls back to empty string gracefully.
    """
    import os
    from datetime import date

    log_group = os.environ.get(
        "AWS_LAMBDA_LOG_GROUP_NAME", "/aws/lambda/trading-bot-sentiment"
    )
    today_prefix = date.today().strftime("%Y/%m/%d")

    keywords = [
        "Macro score", "Claude macro sentiment", "Key events",
        "→", "bullish", "bearish", "neutral",
        "Order placed", "BUY", "SELL", "orders_placed", "signals_found",
        "stop-loss", "Stop loss", "STOP",
        "Account —", "Buying power",
        "Scan complete",
    ]

    try:
        logs_client = boto3.client("logs", region_name=settings.AWS_REGION)
        streams = logs_client.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=today_prefix,
            orderBy="LastEventTime",
            descending=True,
            limit=6,
        ).get("logStreams", [])
    except Exception as exc:
        logger.debug("CloudWatch research fetch skipped: %s", exc)
        return ""

    lines: list[str] = []
    for stream in streams:
        try:
            events = logs_client.get_log_events(
                logGroupName=log_group,
                logStreamName=stream["logStreamName"],
                startFromHead=True,
                limit=300,
            ).get("events", [])
            for ev in events:
                msg = ev.get("message", "")
                if any(kw.lower() in msg.lower() for kw in keywords):
                    # Strip timestamp/request-id prefix, keep the content
                    clean = msg.split("\t")[-1].strip()
                    if clean and len(clean) < 400:
                        lines.append(clean)
        except Exception:
            continue

    if not lines:
        return ""

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)

    return "\n".join(unique[:120])  # cap at 120 lines


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
    research_log: str = "",
    prices: dict[str, float] | None = None,
) -> list[dict]:
    """
    Call Claude Sonnet to generate 3 trade suggestions informed by today's research.

    Args:
        headlines:    Macro news headlines from NewsAPI
        research_log: Today's CloudWatch log digest (signals, macro, trades)
        prices:       Optional dict of {ticker: current_price}

    Returns:
        List of dicts: [{"ticker": str, "rationale": str, "dollars": float}]
    """
    universe_str = ", ".join(SUGGESTION_UNIVERSE)
    system = _SUGGESTION_SYSTEM.format(universe=universe_str)

    parts = []

    if research_log:
        parts.append(f"TODAY'S TRADING RESEARCH LOG:\n{research_log}")

    if headlines:
        headlines_text = "\n".join(f"- {h}" for h in headlines[:50])
        parts.append(f"CURRENT NEWS HEADLINES:\n{headlines_text}")
    else:
        parts.append(
            "No live headlines available. Use your knowledge of recent macro events "
            "to suggest timely plays."
        )

    if prices:
        price_lines = "\n".join(f"  {sym}: ${p:.2f}" for sym, p in sorted(prices.items()))
        parts.append(f"CURRENT/RECENT PRICES:\n{price_lines}")

    now_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    parts.append(f"Today's date: {now_str}")

    user_content = "\n\n".join(parts)

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip() if message.content else ""
        logger.info("Claude raw response (%d chars): %s", len(raw), raw[:300])

        if not raw:
            logger.error("Claude suggestions returned empty response (stop_reason=%s)", message.stop_reason)
            return []

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        # Extract JSON array even if Claude added preamble text
        if not raw.startswith("["):
            start = raw.find("[")
            end   = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                raw = raw[start:end + 1]
            else:
                logger.error("No JSON array found in Claude response")
                return []

        suggestions = json.loads(raw)

        result = []
        for s in suggestions[:3]:
            ticker    = str(s.get("ticker", "")).upper().strip()
            dollars   = float(s.get("dollars", settings.SUGGESTION_DOLLARS_DEFAULT))
            dollars   = round(max(2.0, min(5.0, dollars)), 2)
            rationale = str(s.get("rationale", "")).strip()
            if ticker and rationale:
                result.append({"ticker": ticker, "rationale": rationale, "dollars": dollars})

        logger.info("Claude Sonnet generated %d suggestions: %s", len(result), [s["ticker"] for s in result])
        return result

    except json.JSONDecodeError as exc:
        logger.error("Claude suggestions returned non-JSON (raw=%r): %s", raw[:300] if 'raw' in vars() else "N/A", exc)
        return []
    except Exception as exc:
        logger.error("Claude suggestions failed: %s", exc)
        return []


def _build_suggestion_email(
    suggestions: list[dict],
    function_url: str,
    secret: str,
    research_log: str = "",
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")
    lines = [
        "TraderBot — Evening Research Digest",
        now_str,
        "─" * 42,
    ]

    # Research digest section
    if research_log:
        lines += [
            "TODAY'S RESEARCH SUMMARY",
            "─" * 42,
            research_log[:2000],  # cap for email readability
            "",
        ]

    lines += [
        "─" * 42,
        "TOMORROW'S 3 TRADE IDEAS (Claude Sonnet)",
        "",
    ]

    for i, s in enumerate(suggestions, 1):
        ticker    = s["ticker"]
        dollars   = s["dollars"]
        rationale = s["rationale"]
        url = (
            _make_approval_url(ticker, dollars, function_url, secret)
            if function_url and secret
            else "(approval URL unavailable)"
        )

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

    # 2. Pull today's research from CloudWatch
    research_log = ""
    try:
        research_log = _fetch_todays_research()
        logger.info("Research log: %d chars", len(research_log))
    except Exception as exc:
        logger.warning("Research log fetch failed: %s", exc)

    # 3. Optionally fetch current prices (graceful — fails on weekends/after hours)
    prices: dict[str, float] = {}
    try:
        from broker.public_client import PublicClient
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

    # 4. Generate suggestions via Claude Sonnet
    suggestions = generate_suggestions(headlines, research_log=research_log, prices=prices or None)

    if not suggestions:
        logger.warning("No suggestions generated — skipping email")
        return {"window": "suggestions", "suggestions": 0}

    # 5. Build and send email
    function_url = settings.LAMBDA_FUNCTION_URL
    secret       = settings.SUGGESTION_TOKEN_SECRET

    if not function_url:
        logger.warning("LAMBDA_FUNCTION_URL not set — approval links will be unavailable")
    if not secret:
        logger.warning("SUGGESTION_TOKEN_SECRET not set — approval links will be unsigned")

    message = _build_suggestion_email(suggestions, function_url, secret, research_log=research_log)
    subject = f"[TraderBot] Evening digest + 3 picks — {datetime.now(timezone.utc).strftime('%b %d')}"
    logger.info(message)

    try:
        _publish_sns(message, subject=subject)
    except Exception as exc:
        logger.error("SNS publish failed for suggestions: %s", exc)

    return {
        "window": "suggestions",
        "suggestions": len(suggestions),
        "tickers": [s["ticker"] for s in suggestions],
        "research_chars": len(research_log),
    }
