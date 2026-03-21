#!/usr/bin/env python3
"""
Fetch and display the current Public.com portfolio.

Usage:
    python public-sentiment-trader/scripts/get_portfolio.py

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
    client = PublicClient()

    # Account balance
    bal = client.get_account_balance()
    cash    = float(bal.get("cash_balance") or bal.get("buying_power") or 0)
    equity  = float(bal.get("equity") or 0)
    print(f"\n{'='*50}")
    print(f"  PUBLIC.COM PORTFOLIO")
    print(f"{'='*50}")
    print(f"  Cash / Buying Power: ${cash:,.2f}")
    if equity:
        print(f"  Total Equity:        ${equity:,.2f}")

    # Positions
    positions = client.get_positions()
    if not positions:
        print("\n  No open positions.")
    else:
        print(f"\n  Open Positions ({len(positions)}):")
        print(f"  {'Symbol':<20} {'Qty':>8} {'Avg Cost':>10} {'Mkt Value':>10} {'P&L':>10}")
        print(f"  {'-'*58}")

        symbols = []
        for p in positions:
            sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
            if sym:
                symbols.append(sym)

        # Fetch current prices
        price_map: dict[str, float] = {}
        if symbols:
            try:
                resp = client.get_quotes(symbols)
                for q in (resp.get("quotes", []) if isinstance(resp, dict) else resp):
                    s = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
                    for field in ("last", "lastPrice", "bid", "ask"):
                        v = q.get(field)
                        if v:
                            try:
                                price_map[s] = float(v)
                                break
                            except (ValueError, TypeError):
                                pass
            except Exception as exc:
                print(f"  (Price fetch warning: {exc})")

        total_value = 0.0
        total_cost  = 0.0
        for p in positions:
            sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
            qty = float(p.get("quantity") or p.get("shares") or 0)
            cb  = p.get("costBasis")
            avg = float(cb.get("unitCost") if isinstance(cb, dict) else (cb or 0)) or 0
            current = price_map.get(sym, avg)
            mkt_val = qty * current
            pnl     = (current - avg) * qty if avg > 0 else 0.0
            total_value += mkt_val
            total_cost  += avg * qty
            pnl_str = f"${pnl:+.2f}" if avg > 0 else "n/a"
            print(f"  {sym:<20} {qty:>8.4f} {avg:>10.2f} {mkt_val:>10.2f} {pnl_str:>10}")

        total_pnl = total_value - total_cost
        print(f"  {'-'*58}")
        print(f"  {'TOTAL':<20} {'':>8} {'':>10} {total_value:>10.2f} ${total_pnl:+.2f}")

    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
