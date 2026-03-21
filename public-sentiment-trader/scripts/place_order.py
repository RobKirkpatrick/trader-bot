#!/usr/bin/env python3
"""
Place a stock order on Public.com with preflight check.

Always runs a preflight (cost estimate + buying power check) before placing.
Requires explicit confirmation unless --confirm flag is passed.

Usage:
    python public-sentiment-trader/scripts/place_order.py --symbol AAPL --side buy --dollars 50
    python public-sentiment-trader/scripts/place_order.py --symbol MSFT --side sell --shares 0.5
    python public-sentiment-trader/scripts/place_order.py --symbol AAPL --side buy --dollars 50 --confirm

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

    def get_arg(flag: str) -> str | None:
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return None

    symbol  = (get_arg("--symbol") or "").upper()
    side    = (get_arg("--side") or "").lower()
    dollars = get_arg("--dollars")
    shares  = get_arg("--shares")
    confirm = "--confirm" in args

    if not symbol or side not in ("buy", "sell"):
        print("Usage: place_order.py --symbol AAPL --side buy --dollars 50 [--confirm]")
        sys.exit(1)
    if not dollars and not shares:
        print("Specify --dollars or --shares")
        sys.exit(1)

    client = PublicClient()

    # Preflight
    print(f"\nPreflight check: {side.upper()} {symbol} ({'$' + dollars if dollars else shares + ' shares'})...")
    try:
        if dollars:
            pf = client.preflight_order(symbol, side.upper(), amount=dollars)
        else:
            pf = client.preflight_order(symbol, side.upper(), quantity=shares)
        est_cost = pf.get("estimatedCost") or pf.get("buyingPowerRequirement") or "unknown"
        print(f"  Estimated cost: ${est_cost}")
        print(f"  Preflight OK")
    except Exception as exc:
        print(f"  Preflight warning (non-fatal): {exc}")

    # Confirmation
    if not confirm:
        resp = input(f"\nPlace {side.upper()} order for {symbol}? [yes/no]: ").strip().lower()
        if resp != "yes":
            print("Aborted — no order placed.")
            sys.exit(0)

    # Place order
    print(f"\nPlacing {side.upper()} {symbol}...")
    try:
        if dollars:
            order = client.place_order(symbol, side.upper(), order_type="MARKET", amount=dollars)
        else:
            order = client.place_order(symbol, side.upper(), order_type="MARKET", quantity=shares)
        order_id = order.get("orderId", "unknown")
        print(f"  Order placed: {order_id}")
        print(f"  Status: {order.get('status', 'submitted')}")
    except Exception as exc:
        print(f"  Order failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
