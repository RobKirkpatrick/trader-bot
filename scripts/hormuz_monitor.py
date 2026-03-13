"""
Hormuz Position Monitor — Manual daily check.
==============================================
Reads the trade record from DynamoDB, fetches current option prices
from Public.com, and prints an unrealized P&L summary with an
exit recommendation.

Does NOT auto-close positions. Manual decision only.

Run: source .venv/bin/activate && python scripts/hormuz_monitor.py
     (pass trade_id as arg to override: python scripts/hormuz_monitor.py <trade_id>)
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
    """Load the most recent hormuz trade record from DynamoDB."""
    db = _dynamodb()

    if trade_id:
        resp = db.get_item(TableName=_LOG_TABLE, Key={"trade_id": {"S": trade_id}})
        item = resp.get("Item")
        if not item:
            print(f"No trade found with trade_id={trade_id}")
            return None
        return item

    # Scan for most recent hormuz trade
    resp = db.scan(
        TableName=_LOG_TABLE,
        FilterExpression="#s = :s",
        ExpressionAttributeNames={"#s": "strategy"},
        ExpressionAttributeValues={":s": {"S": "hormuz_strait_closure"}},
    )
    items = resp.get("Items", [])
    if not items:
        print("No hormuz trade records found in DynamoDB.")
        print("Run scripts/hormuz_trade.py first.")
        return None

    items.sort(key=lambda i: i.get("timestamp", {}).get("S", ""), reverse=True)
    return items[0]


def _get_option_price(client: PublicClient, option_symbol: str) -> tuple[float, float]:
    """Return (bid, ask) for an option symbol. Returns (0, 0) on failure."""
    try:
        resp = client.get_quotes([option_symbol])
        for q in resp.get("quotes", []):
            bid = q.get("bid")
            ask = q.get("ask")
            last = q.get("last")
            price = float(bid or ask or last or 0)
            bid_f = float(bid or last or 0)
            ask_f = float(ask or last or 0)
            return bid_f, ask_f
    except Exception as exc:
        print(f"  Warning: could not fetch quote for {option_symbol}: {exc}")
    return 0.0, 0.0


def _dte(expiry_str: str) -> int:
    return (date.fromisoformat(expiry_str) - date.today()).days


def _recommendation(gain_pct: float, dte: int) -> str:
    if dte <= 7:
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

    print(f"  trade_id:  {trade_id}")
    print(f"  opened:    {timestamp}")
    print(f"  deployed:  ${total_cost:.2f}")

    client = PublicClient()
    now    = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")

    print()
    print("=" * 56)
    print(f"  HORMUZ POSITION MONITOR — {now}")
    print("=" * 56)

    total_current_value = 0.0
    total_cost_basis    = 0.0

    # ------------------------------------------------------------------
    # XLE Bull Call Spread
    # ------------------------------------------------------------------
    xle = positions.get("xle_spread", {})
    if xle:
        buy_strike  = xle.get("buy_strike", 0)
        sell_strike = xle.get("sell_strike", 0)
        expiry      = xle.get("expiry", "")
        contracts   = xle.get("contracts", 0)
        cost_basis  = xle.get("net_debit", 0)
        dte         = _dte(expiry) if expiry else 0

        # Fetch buy leg and sell leg prices
        buy_chain  = client.get_option_chain("XLE", expiry, "CALL") if expiry else []
        sell_chain = buy_chain

        def _find_strike_price(chain, strike):
            for c in chain:
                if abs(float(c.get("strikePrice", 0)) - strike) < 0.01:
                    return float(c.get("bid") or c.get("ask") or 0), float(c.get("ask") or 0)
            return 0.0, 0.0

        buy_bid, buy_ask   = _find_strike_price(buy_chain, buy_strike)
        sell_bid, sell_ask = _find_strike_price(sell_chain, sell_strike)

        # Current spread value: bid of buy leg - ask of sell leg (conservative)
        spread_value   = max(0.0, buy_bid - sell_ask)
        current_value  = spread_value * 100 * contracts
        unrealized     = current_value - cost_basis
        gain_pct       = unrealized / cost_basis if cost_basis > 0 else 0.0
        max_gain       = (sell_strike - buy_strike - (cost_basis / contracts / 100)) * 100 * contracts

        total_current_value += current_value
        total_cost_basis    += cost_basis

        print()
        print(f"  XLE Bull Call Spread  (exp {expiry}, {dte} DTE)")
        print(f"  ${buy_strike:.0f}C bid={buy_bid:.2f}  |  ${sell_strike:.0f}C ask={sell_ask:.2f}")
        print(f"  Spread value:  ${spread_value:.2f}/contract  →  ${current_value:.2f} total")
        print(f"  Cost basis:    ${cost_basis:.2f}  ({contracts} contracts)")
        print(f"  Unrealized:    ${unrealized:+.2f}  ({gain_pct:+.1%})")
        print(f"  Max gain:      ${max_gain:.2f}")
        print(f"  Rec: {_recommendation(gain_pct, dte)}")

    # ------------------------------------------------------------------
    # OXY Call
    # ------------------------------------------------------------------
    oxy = positions.get("oxy_call", {})
    if oxy:
        strike    = oxy.get("strike", 0)
        expiry    = oxy.get("expiry", "")
        contracts = oxy.get("contracts", 0)
        cost_basis= oxy.get("cost", 0)
        dte       = _dte(expiry) if expiry else 0

        oxy_chain = client.get_option_chain("OXY", expiry, "CALL") if expiry else []
        oxy_bid, oxy_ask = 0.0, 0.0
        for c in oxy_chain:
            if abs(float(c.get("strikePrice", 0)) - strike) < 0.01:
                oxy_bid = float(c.get("bid") or 0)
                oxy_ask = float(c.get("ask") or 0)
                break

        current_value = oxy_bid * 100 * contracts
        unrealized    = current_value - cost_basis
        gain_pct      = unrealized / cost_basis if cost_basis > 0 else 0.0

        total_current_value += current_value
        total_cost_basis    += cost_basis

        print()
        print(f"  OXY ${strike:.0f}C  (exp {expiry}, {dte} DTE)")
        print(f"  Bid: ${oxy_bid:.2f}  Ask: ${oxy_ask:.2f}  x{contracts} contract(s)")
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
        print(f"  Cash reserve: ${reserve.get('amount', 0):.2f} — {reserve.get('note', '')}")

    # ------------------------------------------------------------------
    # Total
    # ------------------------------------------------------------------
    total_unrealized = total_current_value - total_cost_basis
    total_gain_pct   = total_unrealized / total_cost_basis if total_cost_basis > 0 else 0.0

    print()
    print("-" * 56)
    print(f"  TOTAL  Cost: ${total_cost_basis:.2f}  →  Current: ${total_current_value:.2f}")
    print(f"         Unrealized P&L: ${total_unrealized:+.2f}  ({total_gain_pct:+.1%})")
    print("=" * 56)
    print()


if __name__ == "__main__":
    main()
