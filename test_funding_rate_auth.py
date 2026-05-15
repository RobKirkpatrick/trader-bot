#!/usr/bin/env python3
"""
Quick validation script: Coinbase API auth + BTC-PERP-INTX funding rate.

Run with venv active:
  source .venv/bin/activate && python3 test_funding_rate_auth.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    from funding_rate.coinbase_client import CoinbaseAuthError, CoinbaseClient

    api_key_name = os.getenv("COINBASE_API_KEY_NAME", "")
    private_key = os.getenv("COINBASE_PRIVATE_KEY", "")

    if not api_key_name or api_key_name.startswith("organizations/xxx"):
        print("ERROR: COINBASE_API_KEY_NAME not set in .env")
        print("  Add your key from: coinbase.com → Settings → API")
        sys.exit(1)

    if not private_key or "BEGIN EC PRIVATE KEY" not in private_key:
        print("ERROR: COINBASE_PRIVATE_KEY not set in .env")
        sys.exit(1)

    # Unescape newlines (common when pasted as single-line in .env)
    private_key = private_key.replace("\\n", "\n")

    print(f"Key name: {api_key_name}")
    print("Connecting to Coinbase Advanced Trade API...")

    client = CoinbaseClient(api_key_name=api_key_name, private_key_pem=private_key)

    try:
        # 1. Auth health check
        active = await client.is_trading_active()
        print(f"API reachable: {active}")

        # 2. Discover available perpetual/futures products
        print("\nDiscovering available futures products...")
        response = await client._request("GET", "/api/v3/brokerage/products",
                                         params={"product_type": "FUTURE", "limit": 20})
        future_ids = [p["product_id"] for p in response.get("products", [])]
        perp_ids = [pid for pid in future_ids if "PERP" in pid or "INTX" in pid]
        print(f"  Total futures products found: {len(future_ids)}")
        if perp_ids:
            print(f"  Perpetual (PERP/INTX): {perp_ids}")
        else:
            print(f"  No PERP/INTX products — this account is Coinbase Advanced Trade (US).")
            print(f"  INTX perpetuals require Coinbase International Exchange:")
            print(f"    → Sign up at international.coinbase.com (non-US or institutional)")
            print(f"  Sample futures available: {future_ids[:5]}")

        # 3. BTC spot — confirms spot trading works fine
        prices = await client.get_best_bid_ask("BTC-USD")
        print(f"\nBTC-USD spot (confirms spot auth works):")
        print(f"  Bid: ${prices['bid']:,.2f}   Ask: ${prices['ask']:,.2f}   Mid: ${prices['mid']:,.2f}")

        # 4. Attempt funding rate (will 404 without INTX access)
        print("\nAttempting BTC-PERP-INTX funding rate (requires INTX account)...")
        try:
            rate_8hr = await client.get_funding_rate("BTC-PERP-INTX")
            from funding_rate.strategy import annualize_funding_rate
            apr = annualize_funding_rate(rate_8hr)
            print(f"  8-hour funding rate : {rate_8hr:.6f}  ({rate_8hr * 100:.4f}%)")
            print(f"  Annualized APR      : {apr:.4f}  ({apr * 100:.2f}%)")
        except Exception as e:
            print(f"  404 as expected without INTX account: {e}")

    except CoinbaseAuthError as e:
        print(f"\nAUTH FAILED: {e}")
        print("Check that your API key has 'Advanced Trade' permissions and the private key matches.")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
    finally:
        await client.close()

    print("\nAuth OK — ready to deploy.")


if __name__ == "__main__":
    asyncio.run(main())
