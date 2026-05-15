#!/usr/bin/env python3
"""
Basis arb connectivity test — nearest BTC quarterly futures + annualized basis.

Run with venv active:
  source .venv/bin/activate && python3 test_basis_arb.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    from funding_rate.coinbase_client import CoinbaseAuthError, CoinbaseClient
    from funding_rate.strategy import calc_basis_apr, days_to_expiry, parse_expiry_date

    api_key_name = os.getenv("COINBASE_API_KEY_NAME", "")
    private_key = os.getenv("COINBASE_PRIVATE_KEY", "").replace("\\n", "\n")

    if not api_key_name or not private_key:
        print("ERROR: COINBASE_API_KEY_NAME / COINBASE_PRIVATE_KEY not set in .env")
        sys.exit(1)

    client = CoinbaseClient(api_key_name=api_key_name, private_key_pem=private_key)

    try:
        print("Coinbase API — Basis Arb Test")
        print("=" * 40)

        # Coinbase futures use abbreviated tickers: BIT=Bitcoin, ET=Ethereum, SOL=Solana
        for spot_ticker, base_asset in [("BTC-USD", "BIT"), ("ETH-USD", "ET"), ("SOL-USD", "SOL")]:
            print(f"\n{base_asset}:")

            # Spot price
            spot_prices = await client.get_best_bid_ask(spot_ticker)
            spot_price = spot_prices["mid"]
            print(f"  Spot ({spot_ticker}): ${spot_price:,.2f}")

            # Nearest quarterly futures
            futures_product = await client.get_active_futures(base_asset)
            if futures_product is None:
                print(f"  No active futures contracts found for {base_asset}")
                continue

            futures_ticker = futures_product["product_id"]
            futures_price = await client.get_futures_price(futures_ticker)
            dte = days_to_expiry(futures_ticker)
            expiry = parse_expiry_date(futures_ticker)
            basis_apr = calc_basis_apr(spot_price, futures_price, dte or 1)

            print(f"  Nearest futures : {futures_ticker}")
            print(f"  Futures price   : ${futures_price:,.2f}")
            print(f"  Expiry date     : {expiry.date() if expiry else 'unknown'} ({dte}d)")
            print(f"  Basis (abs)     : ${futures_price - spot_price:,.2f}  "
                  f"({(futures_price - spot_price) / spot_price * 100:.3f}%)")
            print(f"  Basis APR       : {basis_apr * 100:.2f}%  "
                  f"({'QUALIFIES ✓' if basis_apr > 0.08 else 'below 8% threshold'})")

    except CoinbaseAuthError as e:
        print(f"\nAUTH FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
