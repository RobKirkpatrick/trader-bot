"""
Claude trade decision agent.

make_trade_decision() takes a fully assembled data bundle and asks
Claude Sonnet to decide whether to place a trade and on which contract.

The caller (scheduler/jobs.py) is responsible for:
  - Assembling the bundle (live balance, quote, options, sentiment)
  - Acting on the returned decision (placing orders, sending SNS, logging)

Claude always returns valid JSON. On any failure, returns execute=false.
"""

import json
import logging

import anthropic

from config.settings import settings

logger = logging.getLogger(__name__)

_CLIENT: "anthropic.Anthropic | None" = None


def _get_client() -> "anthropic.Anthropic":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _CLIENT


def _safe_reject(reason: str) -> dict:
    return {
        "execute":               False,
        "reason":                reason,
        "confidence":            "low",
        "contract":              {},
        "action":                "buy",
        "limit_price":           None,
        "stop_loss":             None,
        "position_size_dollars": 0.0,
    }


_SYSTEM_PROMPT = (
    "You are an options trading agent. You are conservative, "
    "risk-aware, and only trade high-conviction setups. "
    "Your position sizing is based on live account balance "
    "provided in every request — never assume a fixed account size. "
    "Never risk more than 5% of current cash_balance on a single trade. "
    "Directional bias rules: "
    "When a symbol is SPY, QQQ, or IWM and the sentiment score is strongly negative "
    "(below -0.35) AND macro score is also negative, prefer PUT options over calls — "
    "broad market weakness is a valid thesis, not just individual stock signals. "
    "When a symbol is an oil/energy name (XLE, OXY, CVX, XOM) and macro headlines "
    "indicate a geopolitical supply disruption (Hormuz, sanctions, conflict), "
    "treat this as a high-conviction CALL setup regardless of short-term price action. "
    "Macro thesis trades have a longer time horizon — prefer 30-60 DTE contracts. "
    "Always respond in valid JSON only. No prose, no explanation "
    "outside the JSON structure."
)

_USER_TEMPLATE = """\
Here is the current market data and portfolio state:
{data_bundle}{edgar_section}
Should I place a trade? Respond in this exact JSON format:
{{
  "execute": true or false,
  "reason": "one sentence max",
  "contract": {{
    "symbol": "string or null",
    "strike": null or float,
    "expiry": "string or null",
    "type": "call, put, or stock"
  }},
  "action": "buy or sell",
  "limit_price": null or float,
  "stop_loss": null or float,
  "position_size_dollars": float,
  "confidence": "high, medium, or low"
}}"""


def make_trade_decision(data_bundle: dict) -> dict:
    """
    Pass the assembled market data bundle to Claude Sonnet and get a structured
    trade decision back.

    Args:
        data_bundle: dict with keys: sentiment, quote, top_contracts, portfolio, risk_rules

    Returns dict with:
        execute (bool), reason (str), contract (dict), action (str),
        limit_price (float|None), stop_loss (float|None),
        position_size_dollars (float), confidence (str)
    """
    client = _get_client()

    # Inject EDGAR filing guidance when an 8-K is the signal source
    edgar = data_bundle.get("edgar_catalyst")
    if edgar and edgar.get("filing_text"):
        edgar_section = (
            "\n\nSEC 8-K FILING CONTEXT:\n"
            "This signal comes from an SEC 8-K filing. Read it carefully — pay attention to: "
            "deal terms, consideration amounts, language around leadership departure "
            "('no disagreement with the board' = negotiated exit = bullish signal), "
            "and whether multiple high-impact items appear together.\n"
            f"Catalyst: {edgar.get('catalyst')}  |  Items: {edgar.get('items')}  |  "
            f"Direction: {edgar.get('direction')}\n"
            f"Filing text:\n{edgar['filing_text']}\n"
        )
    else:
        edgar_section = ""

    user_content = _USER_TEMPLATE.format(
        data_bundle=json.dumps(data_bundle, indent=2),
        edgar_section=edgar_section,
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        decision = json.loads(raw)

        # Clamp position_size to the 5% hard cap
        cash = data_bundle.get("portfolio", {}).get("cash_balance", 0.0)
        max_trade = cash * 0.05 if cash > 0 else 1.0
        pos_size = float(decision.get("position_size_dollars") or max_trade)
        decision["position_size_dollars"] = min(pos_size, max_trade)

        # Fill any missing fields with safe defaults
        decision.setdefault("execute",    False)
        decision.setdefault("reason",     "")
        decision.setdefault("confidence", "low")
        decision.setdefault("contract",   {})
        decision.setdefault("action",     "buy")
        decision.setdefault("limit_price", None)
        decision.setdefault("stop_loss",  None)

        logger.info(
            "Agent decision: execute=%s confidence=%s reason=%s pos=$%.2f",
            decision["execute"],
            decision["confidence"],
            decision["reason"],
            decision["position_size_dollars"],
        )
        return decision

    except json.JSONDecodeError as exc:
        logger.error("Agent returned non-JSON: %s", exc)
        return _safe_reject("Agent returned non-JSON response")
    except Exception as exc:
        logger.error("Agent API error: %s", exc)
        return _safe_reject(f"Agent unavailable: {exc}")


def build_data_bundle(
    ts,                         # TickerSentiment
    quote: dict,
    top_contracts: list[dict],
    account_balance: dict,
    open_positions: list[dict],
    daily_pnl: float = 0.0,
    total_exposure: float = 0.0,
    edgar_context: dict | None = None,
) -> dict:
    """
    Assemble the full data bundle to pass to make_trade_decision().

    Args:
        ts:              TickerSentiment from the scanner
        quote:           {bid, ask, last, volume} from PublicOptionsProvider
        top_contracts:   up to 5 contracts from PublicOptionsProvider.get_best_contracts()
        account_balance: {cash_balance, buying_power, portfolio_value} from PublicClient  # Live from Public.com API — do not hardcode
        open_positions:  raw position list from PublicClient
        daily_pnl:       calculated from open positions
        total_exposure:  total dollars at risk across open positions
    """
    cash = account_balance.get("cash_balance", 0.0)  # Live from Public.com API — do not hardcode

    # Sentiment confidence based on score magnitude
    abs_score = abs(ts.score)
    if abs_score >= 0.40:
        confidence = "high"
    elif abs_score >= 0.25:
        confidence = "medium"
    else:
        confidence = "low"

    # Best available headline
    headline = ""
    if ts.articles:
        headline = ts.articles[0].title
    elif ts.signal != "neutral":
        headline = f"{ts.ticker} {ts.signal} signal (score={ts.score:+.3f})"

    # Dominant source
    source_scores = {
        "price":     abs(ts.price_score),
        "finnhub":   abs(ts.finnhub_score),
        "marketaux": abs(ts.marketaux_score),
        "polygon":   abs(ts.polygon_score),
        "wsb":       abs(ts.wsb_score),
        "macro":     abs(ts.macro_score),
    }
    source = max(source_scores, key=source_scores.get)

    bundle: dict = {
        "sentiment": {
            "symbol":       ts.ticker,
            "score":        round(ts.score, 4),
            "direction":    ts.signal,
            "confidence":   confidence,
            "headline":     headline,
            "source":       source,
            "macro_score":  round(ts.macro_score, 4),
            "macro_events": ts.macro_events,
        },
        "quote": quote,
        "top_contracts": top_contracts,
        "portfolio": {
            "cash_balance":    cash,           # Live from Public.com API — do not hardcode
            "buying_power":    account_balance.get("buying_power", cash),       # Live from Public.com API — do not hardcode
            "portfolio_value": account_balance.get("portfolio_value", 0.0),     # Live from Public.com API — do not hardcode
            "open_positions":  len(open_positions),
            "total_exposure":  round(total_exposure, 2),
            "daily_pnl":       round(daily_pnl, 2),
        },
        "risk_rules": {
            "max_single_trade":      round(cash * 0.05, 2),   # Live from Public.com API — do not hardcode
            "max_daily_loss":        round(cash * 0.05, 2),   # Live from Public.com API — do not hardcode

            "stop_loss_pct":         0.07,
            "min_reward_risk_ratio": 1.2,
        },
    }

    if edgar_context:
        bundle["edgar_catalyst"] = {
            "catalyst":    edgar_context.get("catalyst"),
            "direction":   edgar_context.get("direction"),
            "items":       edgar_context.get("items", []),
            "score":       edgar_context.get("score", 0.0),
            "company":     edgar_context.get("company_name"),
            "filed_at":    edgar_context.get("filed_at"),
            "priority":    edgar_context.get("priority", False),
            "filing_text": edgar_context.get("filing_text", ""),
        }

    return bundle
