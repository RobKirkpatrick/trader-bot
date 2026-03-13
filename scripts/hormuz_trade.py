"""
Hormuz Strait Closure — One-Time Macro Trade
=============================================
Standalone script. Not integrated with the automated bot.

Position structure ($750 total, defined risk):
  1. XLE Bull Call Spread — $400 net debit, 4 contracts
  2. OXY Single Call      — $200, 1-2 contracts
  3. $150 cash reserve    — held for ceasefire dip entry

Run: source .venv/bin/activate && python scripts/hormuz_trade.py
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone, date, timedelta

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.public_client import PublicClient
from config.settings import settings

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOTAL_BUDGET       = 750.00
XLE_BUDGET         = 400.00
OXY_BUDGET         = 200.00
CASH_RESERVE       = 150.00
XLE_CONTRACTS      = 4
XLE_SPREAD_WIDTH   = 10.0        # sell strike = buy strike + $10
OXY_MAX_CONTRACTS  = 2
XLE_DTE_MIN        = 45
XLE_DTE_MAX        = 60
OXY_DTE_MIN        = 30
OXY_DTE_MAX        = 45

THESIS = (
    "Strait of Hormuz closure (Feb 28, 2026). ~90% tanker traffic reduction, "
    "15M barrel/day shortfall. Iran actively mining — physical reopening takes "
    "weeks beyond any ceasefire. High IV environment. Defined-risk spread on XLE, "
    "direct exposure via OXY call. Source: OilPrice.com, March 11 2026."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_expiry(expirations: list[str], dte_min: int, dte_max: int) -> str | None:
    today = date.today()
    candidates = []
    for exp_str in expirations:
        exp = date.fromisoformat(exp_str)
        dte = (exp - today).days
        if dte_min <= dte <= dte_max:
            candidates.append((dte, exp_str))
    if not candidates:
        return None
    # Prefer closest to the midpoint of the range
    mid = (dte_min + dte_max) / 2
    candidates.sort(key=lambda x: abs(x[0] - mid))
    return candidates[0][1]


def _first_otm_call(chain: list[dict], current_price: float) -> dict | None:
    """Return the lowest strike strictly above current_price."""
    otm = [c for c in chain if float(c["strikePrice"]) > current_price]
    if not otm:
        return None
    otm.sort(key=lambda c: float(c["strikePrice"]))
    return otm[0]


def _find_strike(chain: list[dict], target_strike: float) -> dict | None:
    """Return the contract with strike closest to target_strike."""
    if not chain:
        return None
    return min(chain, key=lambda c: abs(float(c["strikePrice"]) - target_strike))


def _net_debit_estimate(buy_ask: float, sell_bid: float, contracts: int) -> float:
    """Estimated net debit for a spread (ask of buy - bid of sell) × 100 × contracts."""
    return (buy_ask - sell_bid) * 100 * contracts


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
    db = _dynamodb()
    db.put_item(
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
    print(f"Logged to DynamoDB: trade_id={trade_id}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    client = PublicClient()

    # ------------------------------------------------------------------
    # Step 1 — Buying power check
    # ------------------------------------------------------------------
    print("\nStep 1 — Checking buying power...")
    balance, _ = client.get_account_balance()
    buying_power = float(balance)
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
        val = q.get("last") or q.get("ask") or q.get("bid")
        if sym and val:
            prices[sym] = float(val)
        bid = q.get("bid")
        ask = q.get("ask")
        last = q.get("last")
        print(f"  {sym}: last={last}  bid={bid}  ask={ask}")

    xle_price = prices.get("XLE")
    oxy_price = prices.get("OXY")
    if not xle_price or not oxy_price:
        print("  ERROR: Could not fetch prices for XLE and/or OXY")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 3 — Option expirations
    # ------------------------------------------------------------------
    print("\nStep 3 — Selecting option expirations...")
    xle_exps = client.get_option_expirations("XLE")
    oxy_exps = client.get_option_expirations("OXY")

    xle_expiry = _pick_expiry(xle_exps, XLE_DTE_MIN, XLE_DTE_MAX)
    oxy_expiry = _pick_expiry(oxy_exps, OXY_DTE_MIN, OXY_DTE_MAX)

    if not xle_expiry:
        print(f"  ERROR: No XLE expiry found in {XLE_DTE_MIN}-{XLE_DTE_MAX} DTE window")
        print(f"  Available: {xle_exps[:10]}")
        sys.exit(1)
    if not oxy_expiry:
        print(f"  ERROR: No OXY expiry found in {OXY_DTE_MIN}-{OXY_DTE_MAX} DTE window")
        print(f"  Available: {oxy_exps[:10]}")
        sys.exit(1)

    xle_dte = (date.fromisoformat(xle_expiry) - date.today()).days
    oxy_dte = (date.fromisoformat(oxy_expiry) - date.today()).days
    print(f"  XLE expiry: {xle_expiry} ({xle_dte} DTE)")
    print(f"  OXY expiry: {oxy_expiry} ({oxy_dte} DTE)")

    # ------------------------------------------------------------------
    # Step 4 — Option chains + strike selection
    # ------------------------------------------------------------------
    print("\nStep 4 — Selecting strikes...")

    xle_chain = client.get_option_chain("XLE", xle_expiry, "CALL")
    oxy_chain = client.get_option_chain("OXY", oxy_expiry, "CALL")

    # XLE: first OTM call (buy leg) + buy_strike + $10 (sell leg)
    xle_buy_contract = _first_otm_call(xle_chain, xle_price)
    if not xle_buy_contract:
        print(f"  ERROR: No OTM call found for XLE above ${xle_price:.2f}")
        sys.exit(1)
    xle_buy_strike  = float(xle_buy_contract["strikePrice"])
    xle_sell_strike = xle_buy_strike + XLE_SPREAD_WIDTH
    xle_sell_contract = _find_strike(xle_chain, xle_sell_strike)
    if not xle_sell_contract:
        print(f"  ERROR: No XLE call found at or near ${xle_sell_strike:.2f}")
        sys.exit(1)
    xle_sell_strike = float(xle_sell_contract["strikePrice"])

    xle_buy_ask  = float(xle_buy_contract.get("ask") or 0)
    xle_sell_bid = float(xle_sell_contract.get("bid") or 0)
    xle_net_debit_per = xle_buy_ask - xle_sell_bid
    xle_total_debit   = _net_debit_estimate(xle_buy_ask, xle_sell_bid, XLE_CONTRACTS)

    print(f"\n  XLE Bull Call Spread:")
    print(f"    Buy  ${xle_buy_strike:.0f}C  ask=${xle_buy_ask:.2f}  "
          f"vol={xle_buy_contract.get('volume', '?')}  oi={xle_buy_contract.get('openInterest', '?')}")
    print(f"    Sell ${xle_sell_strike:.0f}C  bid={xle_sell_bid:.2f}  "
          f"vol={xle_sell_contract.get('volume', '?')}  oi={xle_sell_contract.get('openInterest', '?')}")
    print(f"    Net debit: ~${xle_net_debit_per:.2f}/contract | ${xle_total_debit:.2f} total ({XLE_CONTRACTS} contracts)")
    print(f"    Max gain:  ${(XLE_SPREAD_WIDTH - xle_net_debit_per) * 100 * XLE_CONTRACTS:.2f}  "
          f"(spread width ${XLE_SPREAD_WIDTH:.0f} − debit)")

    if xle_total_debit > XLE_BUDGET:
        print(f"  WARNING: Estimated XLE debit ${xle_total_debit:.2f} exceeds budget ${XLE_BUDGET:.2f}")

    # OXY: first OTM call
    oxy_contract = _first_otm_call(oxy_chain, oxy_price)
    if not oxy_contract:
        print(f"  ERROR: No OTM call found for OXY above ${oxy_price:.2f}")
        sys.exit(1)
    oxy_strike  = float(oxy_contract["strikePrice"])
    oxy_ask     = float(oxy_contract.get("ask") or 0)
    oxy_qty     = min(OXY_MAX_CONTRACTS, max(1, int(OXY_BUDGET / (oxy_ask * 100))))
    oxy_cost    = oxy_ask * 100 * oxy_qty

    print(f"\n  OXY Call:")
    print(f"    Buy  ${oxy_strike:.0f}C  ask=${oxy_ask:.2f}  "
          f"vol={oxy_contract.get('volume', '?')}  oi={oxy_contract.get('openInterest', '?')}")
    print(f"    Qty: {oxy_qty} contract(s) | Cost: ~${oxy_cost:.2f}")

    if oxy_cost > OXY_BUDGET:
        print(f"  WARNING: OXY cost ${oxy_cost:.2f} exceeds budget ${OXY_BUDGET:.2f}")

    total_deploy = xle_total_debit + oxy_cost

    # ------------------------------------------------------------------
    # Step 5 — Confirmation pause
    # ------------------------------------------------------------------
    print()
    print("═" * 52)
    print("  HORMUZ TRADE SUMMARY")
    print("═" * 52)
    print(f"  XLE Spread: Buy ${xle_buy_strike:.0f}C / Sell ${xle_sell_strike:.0f}C")
    print(f"  Expiry: {xle_expiry} ({xle_dte} DTE) | {XLE_CONTRACTS} contracts")
    print(f"  Net debit: ~${xle_total_debit:.2f}")
    print()
    print(f"  OXY Call: ${oxy_strike:.0f}C")
    print(f"  Expiry: {oxy_expiry} ({oxy_dte} DTE) | {oxy_qty} contract(s)")
    print(f"  Cost: ~${oxy_cost:.2f}")
    print()
    print(f"  Total deployment: ~${total_deploy:.2f}")
    print(f"  Cash reserve:      ${CASH_RESERVE:.2f} (NOT deploying)")
    print(f"  Max loss:          ~${total_deploy:.2f} (both positions expire worthless)")
    print(f"  XLE max gain:      ${(XLE_SPREAD_WIDTH - xle_net_debit_per) * 100 * XLE_CONTRACTS:.2f}")
    print("═" * 52)
    print()
    response = input("  Type 'CONFIRM' to execute or 'ABORT' to cancel: ").strip()
    if response != "CONFIRM":
        print("Aborted — no orders placed.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Step 6 — Execute
    # ------------------------------------------------------------------
    print("\nStep 6 — Placing orders...")
    positions = {}

    # XLE spread — multi-leg
    xle_buy_leg = client.make_option_leg(
        base_symbol="XLE", option_type="CALL",
        strike=str(int(xle_buy_strike)), expiration=xle_expiry,
        side="BUY", open_close="OPEN",
    )
    xle_sell_leg = client.make_option_leg(
        base_symbol="XLE", option_type="CALL",
        strike=str(int(xle_sell_strike)), expiration=xle_expiry,
        side="SELL", open_close="OPEN",
    )
    xle_limit = f"{xle_net_debit_per:.2f}"

    print(f"  Pre-flighting XLE spread (limit=${xle_limit})...")
    try:
        pf = client.preflight_multi_leg([xle_buy_leg, xle_sell_leg], str(XLE_CONTRACTS), limit_price=xle_limit)
        print(f"  Preflight: {pf}")
    except Exception as exc:
        print(f"  Preflight warning (non-fatal): {exc}")

    print(f"  Placing XLE bull call spread ({XLE_CONTRACTS} contracts)...")
    xle_result = client.place_multi_leg(
        legs=[xle_buy_leg, xle_sell_leg],
        quantity=str(XLE_CONTRACTS),
        limit_price=xle_limit,
    )
    xle_order_id = xle_result.get("orderId", "?")
    print(f"  XLE spread order ID: {xle_order_id}")
    positions["xle_spread"] = {
        "orderId":    xle_order_id,
        "buy_strike": xle_buy_strike,
        "sell_strike": xle_sell_strike,
        "expiry":     xle_expiry,
        "contracts":  XLE_CONTRACTS,
        "net_debit":  round(xle_total_debit, 2),
        "symbol":     "XLE",
        "strategy":   "bull_call_spread",
    }

    # OXY call — single leg
    oxy_symbol = oxy_contract.get("optionSymbol", "")
    print(f"\n  Pre-flighting OXY call ({oxy_symbol})...")
    try:
        pf2 = client.preflight_options_order(oxy_symbol, "BUY", str(oxy_qty), "LIMIT", f"{oxy_ask:.2f}")
        print(f"  Preflight: {pf2}")
    except Exception as exc:
        print(f"  Preflight warning (non-fatal): {exc}")

    print(f"  Placing OXY call ({oxy_qty} contract(s))...")
    oxy_result = client.place_options_order(
        oxy_symbol, "BUY", str(oxy_qty), "LIMIT", f"{oxy_ask:.2f}",
    )
    oxy_order_id = oxy_result.get("orderId", "?")
    print(f"  OXY call order ID: {oxy_order_id}")
    positions["oxy_call"] = {
        "orderId":   oxy_order_id,
        "symbol":    "OXY",
        "strike":    oxy_strike,
        "expiry":    oxy_expiry,
        "contracts": oxy_qty,
        "cost":      round(oxy_cost, 2),
        "strategy":  "long_call",
    }

    positions["cash_reserve"] = {
        "amount":  CASH_RESERVE,
        "note":    "Hormuz reserve — deploy on strait reopening headline pullback only",
        "deployed": False,
    }

    # ------------------------------------------------------------------
    # Step 7 — DynamoDB log
    # ------------------------------------------------------------------
    print("\nStep 7 — Logging to DynamoDB...")
    trade_id = str(uuid.uuid4())
    try:
        _log_to_dynamodb(trade_id, positions, total_deploy)
    except Exception as exc:
        print(f"  WARNING: DynamoDB log failed: {exc}")

    # ------------------------------------------------------------------
    # Step 8 — SNS alert
    # ------------------------------------------------------------------
    print("\nStep 8 — Sending SNS alert...")
    msg = (
        f"HORMUZ TRADE EXECUTED\n"
        f"XLE ${xle_buy_strike:.0f}/{xle_sell_strike:.0f}C spread x{XLE_CONTRACTS} — ${xle_total_debit:.2f}\n"
        f"OXY ${oxy_strike:.0f}C x{oxy_qty} — ${oxy_cost:.2f}\n"
        f"Total deployed: ${total_deploy:.2f} | Reserve held: ${CASH_RESERVE:.2f}\n"
        f"Max loss: ${total_deploy:.2f} | Check Public.com for fills\n"
        f"XLE order: {xle_order_id}\n"
        f"OXY order: {oxy_order_id}"
    )
    subj = f"[TraderBot] Hormuz trade executed — ${total_deploy:.0f} deployed"
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
