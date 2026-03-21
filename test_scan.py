"""
test_scan.py — Run a live sentiment scan locally without placing orders.

Usage:
  cd /path/to/trader-bot
  python3 test_scan.py
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from broker.public_client import PublicClient
from sentiment.scanner import SentimentScanner
from config.settings import settings


def main() -> None:
    print("\n=== Live Sentiment Scan (no orders) ===\n")

    client = PublicClient()

    # Show account state
    try:
        bp = client.get_buying_power()
        print(f"  Buying power : ${bp:,.2f}")
    except Exception as exc:
        print(f"  Buying power : ERROR — {exc}")

    try:
        positions = client.get_positions()
        print(f"  Positions    : {len(positions)} open")
        for p in positions:
            sym = p.get("instrument", {}).get("symbol") or p.get("symbol") or p.get("ticker", "?")
            qty = p.get("quantity") or p.get("shares", "?")
            print(f"    {sym}: {qty} shares")
    except Exception as exc:
        print(f"  Positions    : ERROR — {exc}")

    print(f"\n  Thresholds   : bullish ≥ {settings.SENTIMENT_BUY_THRESHOLD}  "
          f"bearish ≤ {settings.SENTIMENT_SELL_THRESHOLD}")
    print()

    # Run scan
    scanner = SentimentScanner(broker_client=client)
    results = scanner.scan()

    print("\n─" * 42)
    print(f"{'TICKER':<8} {'SCORE':>7}  {'SIGNAL':<8}  "
          f"{'PRICE':>7}  {'MACRO':>7}  {'NEWS':>7}  EARNINGS")
    print("─" * 68)

    strong = []
    for ts in results:
        earn = "YES" if ts.earnings_imminent else ""
        flag = " ◄" if (ts.score >= settings.SENTIMENT_BUY_THRESHOLD
                        or ts.score <= settings.SENTIMENT_SELL_THRESHOLD) else ""
        print(f"  {ts.ticker:<6} {ts.score:>+7.3f}  {ts.signal:<8}  "
              f"{ts.price_score:>+7.3f}  {ts.macro_score:>+7.3f}  "
              f"{ts.polygon_score:>+7.3f}  {earn}{flag}")
        if flag:
            strong.append(ts)

    print("─" * 68)
    print(f"\n  Strong signals: {len(strong)}")
    if strong:
        for ts in strong:
            print(f"    ▶ {ts.ticker}: {ts.signal.upper()} score={ts.score:+.3f}")

    print("\n  ◄ = would trade if market open")
    print()


if __name__ == "__main__":
    main()
