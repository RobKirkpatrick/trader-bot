#!/usr/bin/env python3
"""
Fetch real-time quotes from Public.com.

Usage:
    python public-sentiment-trader/scripts/get_quotes.py AAPL MSFT TSLA
    python public-sentiment-trader/scripts/get_quotes.py AAPL --options   # options chain

Environment variables required:
    PUBLIC_API_SECRET    — API key from Public.com account settings
    PUBLIC_ACCOUNT_ID    — Brokerage account ID
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from broker.public_client import PublicClient


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: get_quotes.py SYMBOL [SYMBOL ...] [--options]")
        sys.exit(1)

    show_options = "--options" in args
    symbols = [a.upper() for a in args if not a.startswith("--")]

    client = PublicClient()

    print(f"\n{'='*55}")
    print(f"  LIVE QUOTES — Public.com")
    print(f"{'='*55}")

    resp = client.get_quotes(symbols)
    quotes = resp.get("quotes", []) if isinstance(resp, dict) else resp

    for q in quotes:
        sym   = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
        last  = q.get("last") or q.get("lastPrice") or "—"
        bid   = q.get("bid") or "—"
        ask   = q.get("ask") or "—"
        chg   = q.get("changePercent") or q.get("percentChange") or ""
        chg_s = f"  ({float(chg):+.2f}%)" if chg else ""
        print(f"\n  {sym}")
        print(f"    Last: ${last}  Bid: ${bid}  Ask: ${ask}{chg_s}")

        if show_options and len(symbols) == 1:
            print(f"\n  OPTIONS CHAIN ({sym}):")
            try:
                exps = client.get_option_expirations(sym)
                if not exps:
                    print("    No expirations available.")
                else:
                    expiry = exps[0]
                    print(f"    Expiration: {expiry}")
                    chain = client.get_option_chain(sym, expiry, "CALL")
                    chain.sort(key=lambda c: float(c.get("strikePrice", 0)))
                    print(f"    {'Strike':>8}  {'Bid':>7}  {'Ask':>7}  {'Volume':>8}  {'OI':>8}")
                    for c in chain[:10]:
                        strike = c.get("strikePrice", "?")
                        cbid   = c.get("bid") or "—"
                        cask   = c.get("ask") or "—"
                        vol    = c.get("volume") or "—"
                        oi     = c.get("openInterest") or "—"
                        print(f"    {strike:>8}  {cbid:>7}  {cask:>7}  {vol:>8}  {oi:>8}")
            except Exception as exc:
                print(f"    Options chain error: {exc}")

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    main()
