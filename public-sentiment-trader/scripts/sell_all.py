#!/usr/bin/env python3
"""
Sell all positions in the current Public.com portfolio.

Usage:
    python public-sentiment-trader/scripts/sell_all.py [--confirm]

Environment variables required:
    PUBLIC_API_SECRET    — API key from Public.com account settings
    PUBLIC_ACCOUNT_ID    — Brokerage account ID
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from broker.public_client import PublicClient


def main() -> None:
    confirm = "--confirm" in sys.argv
    client = PublicClient()
    positions = client.get_positions()

    if not positions:
        print("No open positions to sell.")
        return

    print(f"Found {len(positions)} open positions.\n")
    for p in positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        qty = float(p.get("quantity") or p.get("shares") or 0)
        if not sym or qty <= 0:
            continue
        print(f"Preparing to SELL ALL: {sym} ({qty} shares)")
        if not confirm:
            print("  (Dry run: add --confirm to actually place orders)")
            continue
        try:
            pf = client.preflight_order(sym, "SELL", quantity=str(qty))
            est_proceeds = pf.get("estimatedProceeds") or pf.get("buyingPowerRequirement") or "unknown"
            print(f"  Preflight OK. Estimated proceeds: {est_proceeds}")
            result = client.place_order(sym, "SELL", quantity=str(qty))
            order_id = result.get("orderId")
            print(f"  Order submitted. Order ID: {order_id}")
            time.sleep(0.5)  # avoid rate limits
        except Exception as exc:
            print(f"  ERROR placing sell order for {sym}: {exc}")

    print("\nSell all complete.")

if __name__ == "__main__":
    main()
