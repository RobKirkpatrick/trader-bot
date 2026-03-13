"""
Hormuz Strait Closure — One-Time Macro Trade (Hybrid)
======================================================
Standalone script. Not integrated with the automated bot.

Position structure ($750 total):
  1. XLE Stock — $400 (fractional, no expiry)
  2. OXY Call  — $200, 1 contract (leverage on spike)
  3. $150 cash reserve — held for ceasefire dip entry

Run: source .venv/bin/activate && python scripts/hormuz_trade.py
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone, date

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.public_client import PublicClient
from config.settings import settings

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOTAL_BUDGET  = 750.00
XLE_BUDGET    = 400.00   # stock buy — fractional, market order
OXY_BUDGET    = 200.00   # 1 call contract — limit order at ask
CASH_RESERVE  = 150.00
OXY_DTE_MIN   = 30
OXY_DTE_MAX   = 45

THESIS = (
    "Strait of Hormuz closure (Feb 28, 2026). ~90% tanker traffic reduction, "
    "15M barrel/day shortfall. Iran actively mining — physical reopening takes "
    "weeks beyond any ceasefire. XLE stock (no expiry) + OXY call (spike leverage). "
    "Source: OilPrice.com, March 11 2026."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_expiry(expirations: list[str], dte_min: int, dte_max: int) -> str | None:
    today = date.today()
    candidates = []
    for exp_str in expirations:
        dte = (date.fromisoformat(exp_str) - today).days
        if dte_min <= dte <= dte_max:
            candidates.append((dte, exp_str))
    if not candidates:
        return None
    mid = (dte_min + dte_max) / 2
    candidates.sort(key=lambda x: abs(x[0] - mid))
    return candidates[0][1]


def _first_otm_call(chain: list[dict], current_price: float) -> dict | None:
    otm = [c for c in chain if float(c["strikePrice"]) > current_price]
    if not otm:
        return None
    otm.sort(key=lambda c: float(c["strikePrice"]))
    return otm[0]


def _dynamodb():
    return boto3.client("dynamodb", region_name=settings.AWS_REGION)


def _publish_sns(message: str, subject: str) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", settings.SNS_TOPIC_ARN)
    if not topic_arn:
        print("Warning: SNS_TOPIC_ARN not set — skipping alert")
        return
    region = topic_arn.split(":")[3] if len(topic_arn.split(":")) >= 4 else settings.AWS_REGION
    boto3.client("sns", region_name=region).publish(
        TopicArn=topic_arn, Subject=subject[:99], Message=message,
    )


def _log_to_dynamodb(trade_id: str, positions: dict, total_deployed: float) -> None:
    _dynamodb().put_item(
        TableName="trading-bot-logs",
        Item={
            "trade_id":          {"S": trade_id},
            "type":              {"S": "manual_macro_trade"},
            "strategy":          {"S": "hormuz_strait_closure"},
            "timestamp":         {"S": datetime.now(timezone.utc).isoformat()},
            "positions":         {"S": json.dumps(positions)},
            "total_deployed":    {"N": str(round(total_deployed, 2))},
            "thesis":            {"S": THESIS},
            "source_article":    {"S": "OilPrice.com March 11 2026"},
            "cash_reserve_held": {"N": str(CASH_RESERVE)},
        },
    )
    print(f"  Logged to DynamoDB: trade_id={trade_id}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    client = PublicClient()

    # ------------------------------------------------------------------
    # Step 1 — Buying power check
    # ------------------------------------------------------------------
    print("\nStep 1 — Checking buying power...")
    bal_data = client.get_account_balance()
    buying_power = float(bal_data.get("buying_power") or bal_data.get("cash_balance") or 0)
    print(f"  Buying power: ${buying_power:,.2f}")
    if buying_power < TOTAL_BUDGET:
        print(f"  ERROR: Insufficient buying power (need ${TOTAL_BUDGET:.2f}, have ${buying_power:.2f})")
        sys.exit(1)
    print(f"  OK — sufficient buying power for ${TOTAL_BUDGET:.2f} trade")

    # ------------------------------------------------------------------
    # Step 2 — Current prices
    # ------------------------------------------------------------------
    print("\nStep 2 — Fetching current prices...")
    quotes_resp = client.get_quotes(["XLE", "OXY"])
    prices: dict[str, float] = {}
    for q in quotes_resp.get("quotes", []):
        sym = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
        val = float(q.get("last") or q.get("ask") or q.get("bid") or 0)
        if sym and val:
            prices[sym] = val
        print(f"  {sym}: last={q.get('last')}  bid={q.get('bid')}  ask={q.get('ask')}")

    xle_price = prices.get("XLE")
    oxy_price  = prices.get("OXY")
    if not xle_price or not oxy_price:
        print("  ERROR: Could not fetch prices for XLE and/or OXY")
        sys.exit(1)

    xle_shares_approx = XLE_BUDGET / xle_price

    # ------------------------------------------------------------------
    # Step 3 — OXY option expiry + chain
    # ------------------------------------------------------------------
    print("\nStep 3 — Selecting OXY option expiry and strike...")
    oxy_exps   = client.get_option_expirations("OXY")
    oxy_expiry = _pick_expiry(oxy_exps, OXY_DTE_MIN, OXY_DTE_MAX)
    if not oxy_expiry:
        print(f"  ERROR: No OXY expiry in {OXY_DTE_MIN}-{OXY_DTE_MAX} DTE window")
        print(f"  Available: {oxy_exps[:10]}")
        sys.exit(1)

    oxy_dte   = (date.fromisoformat(oxy_expiry) - date.today()).days
    oxy_chain = client.get_option_chain("OXY", oxy_expiry, "CALL")
    oxy_contract = _first_otm_call(oxy_chain, oxy_price)
    if not oxy_contract:
        print(f"  ERROR: No OTM call found for OXY above ${oxy_price:.2f}")
        sys.exit(1)

    oxy_strike = float(oxy_contract["strikePrice"])
    oxy_ask    = float(oxy_contract.get("ask") or oxy_contract.get("last") or 0)
    oxy_cost   = oxy_ask * 100  # 1 contract

    print(f"  OXY expiry: {oxy_expiry} ({oxy_dte} DTE)")
    print(f"  OXY ${oxy_strike:.0f}C  ask=${oxy_ask:.2f}  "
          f"vol={oxy_contract.get('volume', '?')}  oi={oxy_contract.get('openInterest', '?')}")
    print(f"  Cost: ~${oxy_cost:.2f} (1 contract)")

    if oxy_ask == 0:
        print("  WARNING: OXY ask is $0 — market may be closed/illiquid. Run during market hours.")

    total_deploy = XLE_BUDGET + oxy_cost

    # ------------------------------------------------------------------
    # Step 4 — Confirmation pause
    # ------------------------------------------------------------------
    print()
    print("═" * 52)
    print("  HORMUZ TRADE SUMMARY (HYBRID)")
    print("═" * 52)
    print(f"  XLE Stock:  ${XLE_BUDGET:.2f} (~{xle_shares_approx:.2f} shares @ ${xle_price:.2f})")
    print(f"              Market order, fractional, no expiry")
    print()
    print(f"  OXY Call:   ${oxy_strike:.0f}C expiring {oxy_expiry} ({oxy_dte} DTE)")
    print(f"              1 contract, limit @ ${oxy_ask:.2f} | Cost: ~${oxy_cost:.2f}")
    print()
    print(f"  Total deployment: ~${total_deploy:.2f}")
    print(f"  Cash reserve:      ${CASH_RESERVE:.2f} (NOT deploying)")
    print(f"  XLE max loss:      ${XLE_BUDGET:.2f} (stock goes to $0 — extremely unlikely)")
    print(f"  OXY max loss:      ${oxy_cost:.2f} (call expires worthless)")
    print("═" * 52)
    print()
    response = input("  Type 'CONFIRM' to execute or 'ABORT' to cancel: ").strip()
    if response != "CONFIRM":
        print("Aborted — no orders placed.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Step 5 — Execute
    # ------------------------------------------------------------------
    print("\nStep 5 — Placing orders...")
    positions = {}

    # XLE stock — fractional market order by dollar amount
    print(f"  Pre-flighting XLE stock (${XLE_BUDGET:.2f})...")
    try:
        pf = client.preflight_order("XLE", "BUY", amount=f"{XLE_BUDGET:.2f}")
        print(f"  Preflight: {pf}")
    except Exception as exc:
        print(f"  Preflight warning (non-fatal): {exc}")

    print(f"  Placing XLE stock buy (${XLE_BUDGET:.2f})...")
    xle_result   = client.place_order("XLE", "BUY", amount=f"{XLE_BUDGET:.2f}")
    xle_order_id = xle_result.get("orderId", "?")
    print(f"  XLE order ID: {xle_order_id}")
    positions["xle_stock"] = {
        "orderId":  xle_order_id,
        "symbol":   "XLE",
        "amount":   XLE_BUDGET,
        "price":    xle_price,
        "strategy": "stock_buy",
    }

    # OXY call — 1 contract limit at ask
    oxy_symbol = oxy_contract.get("optionSymbol", "")
    print(f"\n  Pre-flighting OXY call ({oxy_symbol})...")
    try:
        pf2 = client.preflight_options_order(oxy_symbol, "BUY", "1", "LIMIT", f"{oxy_ask:.2f}")
        print(f"  Preflight: {pf2}")
    except Exception as exc:
        print(f"  Preflight warning (non-fatal): {exc}")

    print(f"  Placing OXY call (1 contract @ ${oxy_ask:.2f})...")
    oxy_result   = client.place_options_order(oxy_symbol, "BUY", "1", "LIMIT", f"{oxy_ask:.2f}")
    oxy_order_id = oxy_result.get("orderId", "?")
    print(f"  OXY order ID: {oxy_order_id}")
    positions["oxy_call"] = {
        "orderId":      oxy_order_id,
        "symbol":       "OXY",
        "optionSymbol": oxy_symbol,
        "strike":       oxy_strike,
        "expiry":       oxy_expiry,
        "contracts":    1,
        "cost":         round(oxy_cost, 2),
        "strategy":     "long_call",
    }

    positions["cash_reserve"] = {
        "amount":   CASH_RESERVE,
        "note":     "Hormuz reserve — deploy on strait reopening headline pullback only",
        "deployed": False,
    }

    # ------------------------------------------------------------------
    # Step 6 — DynamoDB log
    # ------------------------------------------------------------------
    print("\nStep 6 — Logging to DynamoDB...")
    trade_id = str(uuid.uuid4())
    try:
        _log_to_dynamodb(trade_id, positions, total_deploy)
    except Exception as exc:
        print(f"  WARNING: DynamoDB log failed: {exc}")

    # ------------------------------------------------------------------
    # Step 7 — SNS alert
    # ------------------------------------------------------------------
    print("\nStep 7 — Sending SNS alert...")
    msg = (
        f"HORMUZ TRADE EXECUTED\n"
        f"XLE stock ${XLE_BUDGET:.2f} (~{xle_shares_approx:.2f} shares) — order {xle_order_id}\n"
        f"OXY ${oxy_strike:.0f}C x1 @ ${oxy_ask:.2f} — ${oxy_cost:.2f} — order {oxy_order_id}\n"
        f"Total deployed: ${total_deploy:.2f} | Reserve held: ${CASH_RESERVE:.2f}\n"
        f"Check Public.com for fill confirmations"
    )
    subj = f"[TraderBot] Hormuz trade — XLE stock + OXY call — ${total_deploy:.0f}"
    try:
        _publish_sns(msg, subj)
        print("  SNS alert sent")
    except Exception as exc:
        print(f"  WARNING: SNS failed: {exc}")

    print()
    print("=" * 52)
    print("  DONE — check Public.com for fill confirmations")
    print(f"  trade_id: {trade_id}")
    print("=" * 52)


if __name__ == "__main__":
    main()
