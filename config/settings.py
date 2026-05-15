import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Public.com
    PUBLIC_API_SECRET: str = os.getenv("PUBLIC_API_SECRET", "")
    PUBLIC_AUTH_URL: str = "https://api.public.com/userapiauthservice/personal/access-tokens"
    PUBLIC_TOKEN_VALIDITY_MINUTES: int = 60

    # Polygon.io
    POLYGON_API_KEY: str = os.getenv("POLYGON_API_KEY", "")
    POLYGON_NEWS_URL: str = "https://api.polygon.io/v2/reference/news"

    # Alpha Vantage
    ALPHA_VANTAGE_API_KEY: str = os.getenv("ALPHA_VANTAGE_API_KEY", "")

    # NewsAPI
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")

    # Anthropic
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Finnhub (free tier: 60 req/min — news sentiment + earnings surprises)
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

    # MarketAux (free tier: 100 req/day — entity-level news sentiment)
    MARKETAUX_API_KEY: str = os.getenv("MARKETAUX_API_KEY", "")

    # Kalshi (Carpet Bagger — prediction market trading)
    KALSHI_API_KEY:         str = os.getenv("KALSHI_API_KEY", "")
    KALSHI_RSA_PRIVATE_KEY: str = os.getenv("KALSHI_RSA_PRIVATE_KEY", "")

    # AWS
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-2")
    AWS_SECRET_NAME: str = os.getenv("AWS_SECRET_NAME", "trading-bot/secrets")
    SNS_TOPIC_ARN: str = os.getenv("SNS_TOPIC_ARN", "")

    # Debug mode — when True, fires an SNS alert on EVERY Claude agent decision
    # (including rejections) so you can see why trades are/aren't being placed.
    TRADE_DEBUG: bool = os.getenv("TRADE_DEBUG", "false").lower() == "true"

    # ---------------------------------------------------------------------------
    # User risk profile — configure these in .env or Secrets Manager
    # ---------------------------------------------------------------------------
    # conservative: 5% max position, no options, cautious VIX handling
    # moderate:     10-15% max position, calls only (default)
    # aggressive:   20% max position, calls enabled, higher concentration
    RISK_TOLERANCE: str = os.getenv("RISK_TOLERANCE", "moderate").lower()

    OPTIONS_CALLS_ENABLED: bool = os.getenv("OPTIONS_CALLS_ENABLED", "true").lower() == "true"
    CARPET_BAGGER_ENABLED: bool = os.getenv("CARPET_BAGGER_ENABLED", "true").lower() == "true"
    CARPET_BAGGER_MAX_POSITION: float = float(os.getenv("CARPET_BAGGER_MAX_POSITION", "1.00"))
    BRACKET_BUSTER_ENABLED: bool = os.getenv("BRACKET_BUSTER_ENABLED", "false").lower() == "true"
    BRACKET_BUSTER_MAX_POSITION: float = float(os.getenv("BRACKET_BUSTER_MAX_POSITION", "5.00"))

    # Risk parameters (overridden by RISK_TOLERANCE if set)
    MAX_POSITION_PCT: float = {
        "conservative": 0.05,
        "moderate":     0.15,
        "aggressive":   0.20,
    }.get(os.getenv("RISK_TOLERANCE", "moderate").lower(), 0.15)
    STOP_LOSS_PCT: float = 0.07         # 7% stop loss
    DAILY_LOSS_LIMIT_PCT: float = 0.10  # 10% daily loss limit

    # Polygon rate limiting (free tier = 5 req/min → 12s delay; paid = 0.2s)
    POLYGON_REQUEST_DELAY: float = 13.0

    # Sentiment thresholds — more aggressive (was ±0.30)
    SENTIMENT_BUY_THRESHOLD: float = 0.25   # min score to flag as bullish (raised from 0.20 to reduce chasing)
    SENTIMENT_SELL_THRESHOLD: float = -0.20  # max score to flag as bearish
    SENTIMENT_OPTIONS_CALL_THRESHOLD: float = 0.35  # min score to try buying a call
    NEWS_LOOKBACK_HOURS: int = 24            # hours of news to scan

    # Scheduling (ET)
    PRE_MARKET_HOUR: int = 8
    PRE_MARKET_MINUTE: int = 0
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 35
    MIDDAY_HOUR: int = 12
    MIDDAY_MINUTE: int = 0
    EOD_HOUR: int = 15
    EOD_MINUTE: int = 30
    EVENING_HOUR: int = 19       # 7:00 PM ET (5:00 PM MT) — off-hours suggestion scan
    EVENING_MINUTE: int = 0
    WEEKEND_HOUR: int = 10       # 10:00 AM ET Saturday — weekend suggestion scan
    WEEKEND_MINUTE: int = 0

    # Weekly performance benchmark — HYSA rate to compare against
    HYSA_APY: float = 0.056   # 5.6% annual — update if your HYSA rate changes

    # Macro Trader — Kalshi economic prediction market bridge
    MACRO_TRADER_ENABLED: bool = os.getenv("MACRO_TRADER_ENABLED", "false").lower() == "true"
    MACRO_TRADER_MAX_POSITION: float = float(os.getenv("MACRO_TRADER_MAX_POSITION", "10.00"))
    MACRO_TRADER_MIN_SIGNAL: float = float(os.getenv("MACRO_TRADER_MIN_SIGNAL", "0.50"))
    MACRO_TRADER_MIN_CONFIDENCE: float = float(os.getenv("MACRO_TRADER_MIN_CONFIDENCE", "0.65"))
    MACRO_TRADER_MIN_EDGE: float = float(os.getenv("MACRO_TRADER_MIN_EDGE", "0.08"))
    MACRO_TRADER_MAX_BID_ASK_SPREAD: float = float(os.getenv("MACRO_TRADER_MAX_BID_ASK_SPREAD", "0.05"))
    MACRO_SIGNAL_CACHE_TABLE: str = os.getenv("MACRO_SIGNAL_CACHE_TABLE", "macro-signal-cache")
    MACRO_OPPORTUNITIES_TABLE: str = os.getenv("MACRO_OPPORTUNITIES_TABLE", "macro-opportunities")
    MACRO_POSITIONS_TABLE: str = os.getenv("MACRO_POSITIONS_TABLE", "macro-positions")

    # Coinbase Advanced API (funding_rate module)
    COINBASE_API_KEY_NAME: str = os.getenv("COINBASE_API_KEY_NAME", "")
    COINBASE_PRIVATE_KEY: str = os.getenv("COINBASE_PRIVATE_KEY", "")
    FUNDING_RATE_ENABLED: bool = os.getenv("FUNDING_RATE_ENABLED", "false").lower() == "true"

    # Spot crypto via Public.com / Zero Hash
    CRYPTO_ENABLED: bool = os.getenv("CRYPTO_ENABLED", "false").lower() == "true"
    _CRYPTO_SYMBOLS: frozenset[str] = frozenset({"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK"})
    FUNDING_RATE_MAX_POSITION: float = float(os.getenv("FUNDING_RATE_MAX_POSITION", "100.00"))
    FUNDING_RATE_MIN_APR: float = float(os.getenv("FUNDING_RATE_MIN_APR", "0.10"))
    FUNDING_RATE_EXIT_APR: float = float(os.getenv("FUNDING_RATE_EXIT_APR", "0.05"))

    # Political Trader — Kalshi political prediction markets
    POLITICAL_TRADER_ENABLED: bool = os.getenv("POLITICAL_TRADER_ENABLED", "false").lower() == "true"
    POLITICAL_TRADER_MAX_POSITION: float = float(os.getenv("POLITICAL_TRADER_MAX_POSITION", "15.00"))
    POLITICAL_TRADER_MIN_SIGNAL: float = float(os.getenv("POLITICAL_TRADER_MIN_SIGNAL", "0.45"))
    POLITICAL_TRADER_MIN_CONFIDENCE: float = float(os.getenv("POLITICAL_TRADER_MIN_CONFIDENCE", "0.60"))
    POLITICAL_OPPORTUNITIES_TABLE: str = os.getenv("POLITICAL_OPPORTUNITIES_TABLE", "political-opportunities")
    POLITICAL_POSITIONS_TABLE: str = os.getenv("POLITICAL_POSITIONS_TABLE", "political-positions")

    # Weather Trader — NWS-edge Kalshi weather prediction markets
    WEATHER_TRADER_ENABLED: bool = os.getenv("WEATHER_TRADER_ENABLED", "false").lower() == "true"
    WEATHER_TRADER_MAX_POSITION: float = float(os.getenv("WEATHER_TRADER_MAX_POSITION", "20.00"))
    WEATHER_TRADER_MIN_EDGE: float = float(os.getenv("WEATHER_TRADER_MIN_EDGE", "0.10"))
    WEATHER_OPPORTUNITIES_TABLE: str = os.getenv("WEATHER_OPPORTUNITIES_TABLE", "weather-opportunities")
    WEATHER_POSITIONS_TABLE: str = os.getenv("WEATHER_POSITIONS_TABLE", "weather-positions")

    # Kill switch — set TRADING_PAUSED=true in .env or Secrets Manager to halt all trades immediately
    TRADING_PAUSED: bool = os.getenv("TRADING_PAUSED", "false").lower() == "true"

    # Sell authorization — when True, intraday rotation closes and EOD stop-losses are
    # NOT auto-executed. Instead an email is sent describing what would have been sold.
    # Set to False (default) to allow the bot to sell automatically.
    REQUIRE_SELL_APPROVAL: bool = os.getenv("REQUIRE_SELL_APPROVAL", "false").lower() == "true"

    # Off-hours suggestion engine
    SUGGESTION_TOKEN_SECRET: str = os.getenv("SUGGESTION_TOKEN_SECRET", "")
    SUGGESTION_EXPIRY_HOURS: int = 20
    SUGGESTION_DOLLARS_DEFAULT: float = 3.0
    LAMBDA_FUNCTION_URL: str = os.getenv("LAMBDA_FUNCTION_URL", "")

    # Macro position tickers — read from env so EOD email can track any trade
    MACRO_TRADE_STOCK_TICKER: str = os.getenv("MACRO_TRADE_STOCK_TICKER", "XLE")
    MACRO_TRADE_CALL_TICKER:  str = os.getenv("MACRO_TRADE_CALL_TICKER",  "OXY")

    # ---------------------------------------------------------------------------
    # Tiered watchlist — controls directional bias per ticker.
    #   BEARISH: puts only (negative signal required)
    #   BULLISH: calls only (positive signal required)
    #   NEUTRAL: agent decides direction based on signal
    # WATCHLIST is kept for backward compatibility (scanner + blacklist filter).
    # ---------------------------------------------------------------------------
    _wl_bearish_env = os.getenv("WATCHLIST_BEARISH", "RIVN,INTC,SOFI,PFE,MRNA,F,VXX")
    WATCHLIST_BEARISH: list[str] = [t.strip().upper() for t in _wl_bearish_env.split(",") if t.strip()]

    _wl_bullish_env = os.getenv("WATCHLIST_BULLISH", "XLE,OXY,BNO,LMT,RTX")
    WATCHLIST_BULLISH: list[str] = [t.strip().upper() for t in _wl_bullish_env.split(",") if t.strip()]

    _wl_neutral_env = os.getenv("WATCHLIST_NEUTRAL", "AAPL,MSFT,NVDA,AMD,META,AMZN,GOOGL,SPY,QQQ,BAC,C,BTC,ETH,SOL")
    WATCHLIST_NEUTRAL: list[str] = [t.strip().upper() for t in _wl_neutral_env.split(",") if t.strip()]

    # Combined watchlist — union of all tiers (backward compat for scanner / blacklist check)
    @classmethod
    def _build_watchlist(cls) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for t in cls.WATCHLIST_BEARISH + cls.WATCHLIST_BULLISH + cls.WATCHLIST_NEUTRAL:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    # Blacklist — comma-separated BLACKLIST env var. Tickers listed here are
    # permanently excluded from sentiment scanning and all trade execution.
    _blacklist_env = os.getenv("BLACKLIST", "X,LYFT,SNAP,PLTR")
    BLACKLIST: set[str] = {t.strip().upper() for t in _blacklist_env.split(",") if t.strip()}


settings = Settings()

# Build combined WATCHLIST after instantiation so all tier lists are resolved
settings.WATCHLIST = settings._build_watchlist()


def get_ticker_bias(ticker: str) -> str:
    """Return directional bias for a ticker: 'bearish', 'bullish', or 'neutral'."""
    t = ticker.strip().upper()
    if t in settings.WATCHLIST_BEARISH:
        return "bearish"
    if t in settings.WATCHLIST_BULLISH:
        return "bullish"
    return "neutral"


def get_conflicted_tickers() -> dict[str, list[str]]:
    """
    Return any ticker that appears in more than one list (bearish, bullish, neutral, blacklist).

    A conflicted ticker is blocked from trading until the conflict is resolved in .env.

    Returns: {ticker: ["bearish", "blacklist"]} for each conflict found.
    """
    membership: dict[str, list[str]] = {}
    sources = [
        ("bearish",   settings.WATCHLIST_BEARISH),
        ("bullish",   settings.WATCHLIST_BULLISH),
        ("neutral",   settings.WATCHLIST_NEUTRAL),
        ("blacklist", list(settings.BLACKLIST)),
    ]
    for list_name, tickers in sources:
        for t in tickers:
            membership.setdefault(t, []).append(list_name)

    return {t: lists for t, lists in membership.items() if len(lists) > 1}
