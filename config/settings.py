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

    # Off-hours suggestion engine
    SUGGESTION_TOKEN_SECRET: str = os.getenv("SUGGESTION_TOKEN_SECRET", "")
    SUGGESTION_EXPIRY_HOURS: int = 20
    SUGGESTION_DOLLARS_DEFAULT: float = 3.0
    LAMBDA_FUNCTION_URL: str = os.getenv("LAMBDA_FUNCTION_URL", "")

    # Watchlist — tickers to scan
    # Mix of high-conviction mega-caps + options-affordable mid-priced names ($10–$50)
    # where 1 ATM contract fits within the ~$150 position budget
    WATCHLIST: list[str] = [
        # Mega-cap tech (high sentiment signal, stock buys)
        "AAPL", "MSFT", "TSLA", "NVDA", "AMD",
        "META", "AMZN", "GOOGL",
        # Broad market ETFs (put spreads on bearish macro signals)
        "SPY", "QQQ", "IWM",
        # Energy / Hormuz trade — oil supply disruption plays
        "XLE", "OXY", "BNO",  # BNO = Brent crude ETF, most direct Iran/Hormuz proxy
        # Financials — $20–$70, options ~$0.30–$1.00/contract ($30–$100)
        "BAC", "C", "INTC",
        # AI / data plays — $20–$35, options ~$0.50–$1.00 ($50–$100)
        "PLTR", "SOFI",
        # Consumer / social — $8–$15, options ~$0.20–$0.60 ($20–$60)
        "SNAP", "LYFT", "F",
        # Biotech / pharma (beaten-down, cheap options) — $25–$45
        "PFE", "MRNA",
        # EV — volatile, active options, $10–$15
        "RIVN",
        # Index options (cash-settled) — newly available on Public as of Mar 2026
        # SPX/NDX/VIX options are European-style, no early assignment risk
        "SPX", "NDX", "VIX", "CBTX",
    ]


settings = Settings()
