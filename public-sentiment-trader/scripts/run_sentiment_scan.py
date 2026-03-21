#!/usr/bin/env python3
"""
Run a full multi-source sentiment scan across the watchlist.

Blends 6 sources (price, Finnhub, MarketAux, Claude macro, Polygon, WSB)
into a composite score from -1.0 (bearish) to +1.0 (bullish).

Usage:
    python public-sentiment-trader/scripts/run_sentiment_scan.py
    python public-sentiment-trader/scripts/run_sentiment_scan.py --tickers AAPL MSFT NVDA

Environment variables required:
    PUBLIC_API_SECRET, PUBLIC_ACCOUNT_ID
    ANTHROPIC_API_KEY  (Claude macro scoring)
    POLYGON_API_KEY    (news sentiment)
    FINNHUB_API_KEY    (pre-computed sentiment)
    MARKETAUX_API_KEY  (entity-level sentiment)
    NEWS_API_KEY       (macro headlines)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from sentiment.scanner import SentimentScanner


def main() -> None:
    args = sys.argv[1:]
    custom_tickers = []
    if "--tickers" in args:
        idx = args.index("--tickers")
        custom_tickers = [t.upper() for t in args[idx + 1:]]

    watchlist = custom_tickers or settings.WATCHLIST

    print(f"\n{'='*60}")
    print(f"  SENTIMENT SCAN  ({len(watchlist)} tickers)")
    print(f"  Buy threshold:  ≥ {settings.SENTIMENT_BUY_THRESHOLD:+.2f}")
    print(f"  Call threshold: ≥ {settings.SENTIMENT_OPTIONS_CALL_THRESHOLD:+.2f}")
    print(f"  Sell threshold: ≤ {settings.SENTIMENT_SELL_THRESHOLD:+.2f}")
    print(f"{'='*60}")

    scanner = SentimentScanner()
    results = scanner.scan(watchlist)

    bullish = [r for r in results if r.score >= settings.SENTIMENT_BUY_THRESHOLD]
    bearish = [r for r in results if r.score <= settings.SENTIMENT_SELL_THRESHOLD]
    neutral = [r for r in results if r not in bullish and r not in bearish]

    if bullish:
        print(f"\n  BULLISH SIGNALS ({len(bullish)}):")
        print(f"  {'Ticker':<8} {'Score':>7}  {'Price':>7}  {'Finnhub':>8}  {'Macro':>7}  {'WSB':>6}  {'Call?':>6}")
        print(f"  {'-'*56}")
        for r in sorted(bullish, key=lambda x: x.score, reverse=True):
            call = "YES" if r.score >= settings.SENTIMENT_OPTIONS_CALL_THRESHOLD else ""
            earn = " [EARN]" if r.earnings_imminent else ""
            print(
                f"  {r.ticker:<8} {r.score:>+7.3f}  {r.price_score:>+7.3f}  "
                f"{r.finnhub_score:>+8.3f}  {r.macro_score:>+7.3f}  "
                f"{r.wsb_score:>+6.3f}  {call:>6}{earn}"
            )

    if bearish:
        print(f"\n  BEARISH SIGNALS ({len(bearish)}):")
        for r in sorted(bearish, key=lambda x: x.score):
            print(f"  {r.ticker:<8} {r.score:>+7.3f}")

    if not bullish and not bearish:
        print(f"\n  No signals above threshold.")

    print(f"\n  Neutral ({len(neutral)} tickers — closest to threshold):")
    top3 = sorted(neutral, key=lambda x: abs(x.score), reverse=True)[:3]
    for r in top3:
        print(f"  {r.ticker:<8} {r.score:>+7.3f}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
