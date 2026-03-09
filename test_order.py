"""
test_order.py — Manual test script for a live $5 notional buy of CAT.

Run locally:
  cd /Users/rob/Desktop/Workspace/Python/Venmo
  pip3 install -r requirements.txt
  python3 test_order.py

Flow:
  1. Authenticate with Public.com
  2. Fetch account ID
  3. Run preflight (validate + estimate cost)
  4. Show preflight summary and ask for confirmation
  5. Place the order
  6. Poll for execution status

WARNING: This places a REAL order on your live Public.com account.
"""

import json
import os
import time
from dotenv import load_dotenv

load_dotenv()

from broker.public_client import PublicClient

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------
SYMBOL = "CAT"
AMOUNT = "5.00"   # $5 notional
SIDE   = "BUY"
ORDER_TYPE = "MARKET"


def fmt(label: str, value) -> str:
    return f"  {label:<28} {value}"


def main() -> None:
    client = PublicClient()

    # ------------------------------------------------------------------
    # 1. Auth + account + live balance
    # ------------------------------------------------------------------
    print("\n=== Public.com Order Test ===\n")
    print("Authenticating and fetching account details...")
    try:
        account_id = client.get_account_id()
        print(f"  Account ID      : {account_id}")
    except Exception as exc:
        print(f"  ERROR fetching account: {exc}")
        return

    try:
        portfolio = client.get_portfolio()
        bp = portfolio.get("buyingPower", {})
        cash_bp   = bp.get("cashOnlyBuyingPower", "n/a")
        margin_bp = bp.get("buyingPower", "n/a")

        # Find cash equity value
        cash_equity = next(
            (e["value"] for e in portfolio.get("equity", []) if e.get("type") == "CASH"),
            "n/a",
        )
        open_positions = len(portfolio.get("positions", []))

        print(f"  Cash balance    : ${cash_equity}")
        print(f"  Cash buying pwr : ${cash_bp}")
        print(f"  Margin buying p : ${margin_bp}")
        print(f"  Open positions  : {open_positions}")
    except Exception as exc:
        print(f"  WARNING: Could not fetch portfolio: {exc}")
        cash_bp = "n/a"

    # ------------------------------------------------------------------
    # 2. Preflight
    # ------------------------------------------------------------------
    print(f"\nRunning preflight for ${AMOUNT} {SIDE} {SYMBOL} ({ORDER_TYPE})...")
    try:
        pf = client.preflight_order(
            symbol=SYMBOL,
            side=SIDE,
            order_type=ORDER_TYPE,
            amount=AMOUNT,
        )
    except Exception as exc:
        print(f"  Preflight ERROR: {exc}")
        return

    print("\n--- Preflight Result ---")
    cost_fields = {
        "estimatedCost":       pf.get("estimatedCost"),
        "orderValue":          pf.get("orderValue"),
        "estimatedCommission": pf.get("estimatedCommission"),
        "estimatedQuantity":   pf.get("estimatedQuantity"),
        "buyingPowerRequired": pf.get("buyingPowerRequirement"),
        "estimatedExecFee":    pf.get("estimatedExecutionFee"),
    }
    for label, value in cost_fields.items():
        if value is not None:
            print(fmt(label + ":", value))

    reg_fees = pf.get("regulatoryFees", {})
    if reg_fees:
        print(fmt("regulatoryFees:", json.dumps(reg_fees)))

    print(f"\n  Raw preflight response:\n{json.dumps(pf, indent=4)}")

    # ------------------------------------------------------------------
    # 3. Confirm
    # ------------------------------------------------------------------
    print(f"\n{'='*50}")
    print(f"  About to place: {SIDE} ${AMOUNT} of {SYMBOL} (MARKET, DAY)")
    print(f"  On account    : {account_id}")
    print(f"{'='*50}")
    confirm = input("\n  Type 'yes' to place the order, anything else to cancel: ").strip().lower()

    if confirm != "yes":
        print("  Cancelled. No order placed.")
        return

    # ------------------------------------------------------------------
    # 4. Place order
    # ------------------------------------------------------------------
    print("\nPlacing order...")
    try:
        result = client.place_order(
            symbol=SYMBOL,
            side=SIDE,
            order_type=ORDER_TYPE,
            amount=AMOUNT,
        )
    except Exception as exc:
        print(f"  Order ERROR: {exc}")
        return

    order_id = result.get("orderId", "")
    print(f"  Order submitted! orderId: {order_id}")

    # ------------------------------------------------------------------
    # 5. Poll for status (up to 15s)
    # ------------------------------------------------------------------
    if not order_id:
        print("  No orderId returned — check Public.com app for status.")
        return

    print("\nPolling for execution status...")
    for attempt in range(5):
        time.sleep(3)
        try:
            status = client.get_order(order_id)
            print(f"  [{attempt+1}] {json.dumps(status, indent=2)}")
            state = status.get("status") or status.get("orderStatus", "")
            if state.upper() in ("FILLED", "CANCELLED", "REJECTED"):
                break
        except Exception as exc:
            print(f"  Status poll error: {exc}")
            break

    print("\nDone. Check your Public.com app to confirm.")


if __name__ == "__main__":
    main()
