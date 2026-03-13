"""
Hormuz Position Monitor — Manual daily check.
==============================================
Reads the trade record from DynamoDB, fetches current prices from
Public.com, and prints an unrealized P&L summary with an exit
recommendation.

Does NOT auto-close. Manual decision only.

Run: source .venv/bin/activate && python scripts/hormuz_monitor.py
     (optional: pass trade_id as argument)
"""

import json
import os
import sys
from datetime import date, datetime, timezone

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.public_client import PublicClient
from config.settings import settings

_LOG_TABLE = "trading-bot-logs"


def _dynamodb():
    return boto3.client("dynamodb", region_name=settings.AWS_REGION)


def _load_trade(trade_id: str | None) -> dict | None:
    db = _dynamodb()
    if trade_id:
        resp = db.get_item(TableName=_LOG_TABLE, Key={"trade_id": {"S": trade_id}})
        item = resp.get("Item")
        if not item:
            print(f"No trade found with trade_id={trade_id}")
        return item

    resp = db.scan(
        TableName=_LOG_TABLE,
        FilterExpression="#s = :s",
        ExpressionAttributeNames={"#s": "strategy"},
        ExpressionAttributeValues={":s": {"S": "hormuz_strait_closure"}},
    )
    items = resp.get("Items", [])
    if not items:
        print("No hormuz trade records found. Run scripts/hormuz_trade.py first.")
        return None
    items.sort(key=lambda i: i.get("timestamp", {}).get("S", ""), reverse=True)
    return items[0]


def _dte(expiry_str: str) -> int:
    return (date.fromisoformat(expiry_str) - date.today()).days


def _recommendation(gain_pct: float, dte: int | None = None) -> str:
    if dte is not None and dte <= 7:
        return "EXPIRY APPROACHING — decide now (close or let expire)"
    if gain_pct >= 0.40:
        return "Consider taking profit (unrealized gain >= 40%)"
    if gain_pct <= -0.50:
        return "Consider cutting loss (unrealized loss >= 50%)"
    return "Hold — no action triggered"


def main() -> None:
    trade_id_arg = sys.argv[1] if len(sys.argv) > 1 else None

    print("\nLoading trade record from DynamoDB...")
    item = _load_trade(trade_id_arg)
    if not item:
        sys.exit(1)

    trade_id   = item["trade_id"]["S"]
    timestamp  = item.get("timestamp", {}).get("S", "?")
    positions  = json.loads(item.get("positions", {}).get("S", "{}"))
    total_cost = float(item.get("total_deployed", {}).get("N", 0))

    print(f"  trade_id: {trade_id}")
    print(f"  opened:   {timestamp}")
    print(f"  deployed: ${total_cost:.2f}")

    client = PublicClient()
    now    = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")

    print()
    print("=" * 56)
    print(f"  HORMUZ MONITOR — {now}")
    print("=" * 56)

    total_current = 0.0
    total_basis   = 0.0

    # ------------------------------------------------------------------
    # XLE Stock
    # ------------------------------------------------------------------
    xle = positions.get("xle_stock", {})
    if xle:
        cost_basis  = float(xle.get("amount", 0))
        entry_price = float(xle.get("price", 0))
        quotes = client.get_quotes(["XLE"])
        current_price = 0.0
        for q in quotes.get("quotes", []):
            val = q.get("last") or q.get("bid")
            if val:
                current_price = float(val)

        if entry_price > 0 and current_price > 0:
            shares        = cost_basis / entry_price
            current_value = shares * current_price
            unrealized    = current_value - cost_basis
            gain_pct      = unrealized / cost_basis
        else:
            current_value = cost_basis
            unrealized    = 0.0
            gain_pct      = 0.0

        total_current += current_value
        total_basis   += cost_basis

        print()
        print(f"  XLE Stock (no expiry)")
        print(f"  Entry: ${entry_price:.2f}  Current: ${current_price:.2f}")
        print(f"  ~{cost_basis/entry_price:.2f} shares | Value: ${current_value:.2f}")
        print(f"  Unrealized: ${unrealized:+.2f}  ({gain_pct:+.1%})")
        print(f"  Rec: {_recommendation(gain_pct)}")

    # ------------------------------------------------------------------
    # OXY Call
    # ------------------------------------------------------------------
    oxy = positions.get("oxy_call", {})
    if oxy:
        oxy_symbol = oxy.get("optionSymbol", "")
        strike     = float(oxy.get("strike", 0))
        expiry     = oxy.get("expiry", "")
        cost_basis = float(oxy.get("cost", 0))
        dte        = _dte(expiry) if expiry else None

        # Fetch current option price from chain
        current_bid = 0.0
        if expiry:
            try:
                chain = client.get_option_chain("OXY", expiry, "CALL")
                for c in chain:
                    if abs(float(c.get("strikePrice", 0)) - strike) < 0.01:
                        current_bid = float(c.get("bid") or 0)
                        current_ask = float(c.get("ask") or 0)
                        break
            except Exception as exc:
                print(f"  Warning: could not fetch OXY chain: {exc}")

        current_value = current_bid * 100  # 1 contract
        unrealized    = current_value - cost_basis
        gain_pct      = unrealized / cost_basis if cost_basis > 0 else 0.0

        total_current += current_value
        total_basis   += cost_basis

        print()
        print(f"  OXY ${strike:.0f}C  (exp {expiry}, {dte} DTE)")
        print(f"  Bid: ${current_bid:.2f}  |  1 contract")
        print(f"  Current value: ${current_value:.2f}  (using bid)")
        print(f"  Cost basis:    ${cost_basis:.2f}")
        print(f"  Unrealized:    ${unrealized:+.2f}  ({gain_pct:+.1%})")
        print(f"  Rec: {_recommendation(gain_pct, dte)}")

    # ------------------------------------------------------------------
    # Cash reserve
    # ------------------------------------------------------------------
    reserve = positions.get("cash_reserve", {})
    if reserve:
        print()
        print(f"  Cash reserve: ${reserve.get('amount', 0):.2f}")
        print(f"  Note: {reserve.get('note', '')}")

    # ------------------------------------------------------------------
    # Total
    # ------------------------------------------------------------------
    total_unrealized = total_current - total_basis
    total_pct        = total_unrealized / total_basis if total_basis > 0 else 0.0

    print()
    print("-" * 56)
    print(f"  TOTAL  Cost: ${total_basis:.2f}  →  Current: ${total_current:.2f}")
    print(f"         Unrealized P&L: ${total_unrealized:+.2f}  ({total_pct:+.1%})")
    print("=" * 56)
    print()


if __name__ == "__main__":
    main()
