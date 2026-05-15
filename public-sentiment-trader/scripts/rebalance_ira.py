"""
IRA Rebalancing Script — Public.com Roth IRA (5OD29589)

Steps:
  1. Read current holdings from the IRA account
  2. Calculate trades needed to reach target allocation
  3. Print proposed trades and wait for confirmation
  4. Execute sells first (generate cash), then buys
  5. Log every fill

Target allocation:
  25% cash / money market  (SPAXX or equivalent — held as cash on Public)
  15% energy ETF           (XLE preferred, VDE fallback)
  10% TIPS ETF             (SCHP preferred, VTIP fallback)
  10% broad commodities    (PDBC preferred, DJP fallback)
  40% broad equity index   (whatever index funds currently held — reduce to 40%)

Safety constraints (IRA rules):
  - No options, no margin, no shorting
  - No day-trading
  - Sells execute before buys
  - Limit orders preferred; market only for residual fractional cleanup
  - Script is entirely standalone — never touches the sentiment bot account (5OP20116)
"""

import logging
import os
import sys
import time
import uuid

# Add project root to path so broker/ and config/ are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()

from broker.public_client import PublicClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — IRA account is HARDCODED, never reads PUBLIC_ACCOUNT_ID
# ---------------------------------------------------------------------------

IRA_ACCOUNT_ID = "5OD29589"   # Roth IRA — DO NOT CHANGE
BASE_URL = "https://api.public.com"

# Target allocation weights (must sum to 1.0)
TARGET = {
    "cash":       0.25,   # Held as cash on Public (no ticker needed)
    "energy":     0.15,   # XLE preferred
    "tips":       0.10,   # SCHP preferred
    "commodities":0.10,   # PDBC preferred
    "equity":     0.40,   # Broad index — reduce existing holdings to this bucket
}

# Preferred tickers per bucket (first available / already held wins)
ENERGY_TICKERS      = ["XLE", "VDE"]
TIPS_TICKERS        = ["SCHP", "VTIP"]
COMMODITY_TICKERS   = ["PDBC", "DJP", "COPX", "SCCO"]  # COPX + SCCO kept as copper thesis

# Change 1: IBIT is kept — macro thesis (dollar debasement) is bullish for Bitcoin
KEEP_TICKERS = {"IBIT"}

# Change 2: Override commodities buy — split between COPX and PDBC
COMMODITIES_BUY_OVERRIDE = [
    {"symbol": "COPX", "dollars": 1500.00, "reason": "Copper miners — structural demand thesis"},
    {"symbol": "PDBC", "dollars": 1564.00, "reason": "Broad commodity futures — oil/gold/ag/metals, tight oil shock alignment"},
]

# Tickers we treat as "broad equity index" (extend if needed)
EQUITY_INDEX_TICKERS = {
    "SPY", "VOO", "IVV", "VTI", "ITOT", "SCHB", "SCHA", "SCHX",
    "QQQ", "VUG", "VTV", "VIG", "VXUS", "VEA", "VWO",
}

# Minimum trade size — skip if dollar amount is too small to bother
MIN_TRADE_USD = 5.00

# Limit order slippage buffer (bid + buffer for buys; ask - buffer for sells)
LIMIT_BUFFER_PCT = 0.001   # 0.1% — keeps fills tight on liquid ETFs


# ---------------------------------------------------------------------------
# IRA-aware PublicClient subclass
# ---------------------------------------------------------------------------

class IRAClient(PublicClient):
    """
    PublicClient locked to the IRA account ID.
    Overrides get_account_id() so every API call targets 5OD29589.
    """

    def get_account_id(self) -> str:
        return IRA_ACCOUNT_ID


# ---------------------------------------------------------------------------
# Portfolio reader
# ---------------------------------------------------------------------------

def get_ira_snapshot(client: IRAClient) -> tuple[float, list[dict]]:
    """
    Return (cash_balance, positions) for the IRA.
    positions: list of dicts with keys: symbol, quantity, market_value, cost_basis
    """
    import requests
    headers = client._headers()

    url = f"{BASE_URL}/userapigateway/trading/{IRA_ACCOUNT_ID}/portfolio/v2"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    portfolio = resp.json()

    bp = portfolio.get("buyingPower", {})
    cash = float(bp.get("cashOnlyBuyingPower") or bp.get("buyingPower") or 0)

    raw_positions = portfolio.get("positions", [])
    positions = []
    for p in raw_positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        if not sym:
            continue
        qty = float(p.get("quantity") or 0)
        mv_raw = p.get("marketValue") or p.get("currentValue") or p.get("totalValue") or "0"
        mv = float(mv_raw) if isinstance(mv_raw, (int, float)) else float(mv_raw or 0)

        cb_raw = p.get("costBasis")
        if isinstance(cb_raw, dict):
            cost = float(cb_raw.get("totalCost") or cb_raw.get("unitCost") or 0)
        else:
            cost = float(cb_raw or 0)

        positions.append({
            "symbol":       sym,
            "quantity":     qty,
            "market_value": mv,
            "cost_basis":   cost,
        })

    return cash, positions


def get_live_prices(client: IRAClient, symbols: list[str]) -> dict[str, float]:
    """Return {symbol: last_price} for a list of symbols."""
    if not symbols:
        return {}
    try:
        data = client.get_quotes(symbols)
        prices = {}
        for q in data.get("quotes", []):
            sym = q.get("instrument", {}).get("symbol") or q.get("symbol", "")
            price = float(q.get("last") or q.get("lastPrice") or q.get("mid") or 0)
            if sym and price > 0:
                prices[sym.upper()] = price
        return prices
    except Exception as e:
        logger.warning("Price fetch failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Bucket classifier
# ---------------------------------------------------------------------------

def classify_positions(positions: list[dict]) -> dict[str, list[dict]]:
    """
    Sort positions into buckets: energy, tips, commodities, equity, other.
    """
    buckets: dict[str, list[dict]] = {
        "energy": [], "tips": [], "commodities": [], "equity": [], "other": []
    }
    for p in positions:
        sym = p["symbol"]
        if sym in ENERGY_TICKERS:
            buckets["energy"].append(p)
        elif sym in TIPS_TICKERS:
            buckets["tips"].append(p)
        elif sym in COMMODITY_TICKERS:
            buckets["commodities"].append(p)
        elif sym in EQUITY_INDEX_TICKERS:
            buckets["equity"].append(p)
        else:
            buckets["other"].append(p)
    return buckets


# ---------------------------------------------------------------------------
# Rebalance calculator
# ---------------------------------------------------------------------------

def compute_trades(
    cash: float,
    positions: list[dict],
    prices: dict[str, float],
) -> tuple[float, list[dict], list[dict]]:
    """
    Calculate sells and buys needed to hit target allocation.

    Returns:
        total_value  — total IRA value (cash + positions)
        sells        — list of {symbol, quantity, dollars, reason}
        buys         — list of {symbol, dollars, reason}
    """
    pos_value = sum(p["market_value"] for p in positions)
    # If market_value is missing, fall back to quantity × live price
    if pos_value == 0:
        for p in positions:
            p["market_value"] = p["quantity"] * prices.get(p["symbol"], 0)
        pos_value = sum(p["market_value"] for p in positions)

    total_value = cash + pos_value

    buckets = classify_positions(positions)

    target_cash       = total_value * TARGET["cash"]
    target_energy     = total_value * TARGET["energy"]
    target_tips       = total_value * TARGET["tips"]
    target_commodities= total_value * TARGET["commodities"]
    target_equity     = total_value * TARGET["equity"]

    sells: list[dict] = []
    buys:  list[dict] = []

    # --- Equity bucket: reduce to 40% ---
    equity_value = sum(p["market_value"] for p in buckets["equity"])
    if equity_value > target_equity + MIN_TRADE_USD:
        trim_usd = equity_value - target_equity
        # Trim proportionally from each held equity position
        for p in buckets["equity"]:
            if equity_value <= 0:
                break
            frac = p["market_value"] / equity_value
            sell_usd = trim_usd * frac
            if sell_usd < MIN_TRADE_USD:
                continue
            price = prices.get(p["symbol"], 0)
            qty = round(sell_usd / price, 6) if price > 0 else 0
            if qty > 0:
                sells.append({
                    "symbol":   p["symbol"],
                    "quantity": qty,
                    "dollars":  sell_usd,
                    "reason":   f"Trim equity to {TARGET['equity']*100:.0f}% target",
                })
    elif equity_value < target_equity - MIN_TRADE_USD:
        # Need more equity — we'll handle this in the buy pass if cash allows
        buys.append({
            "symbol":  _pick_ticker(buckets["equity"], EQUITY_INDEX_TICKERS, "SPY"),
            "dollars": target_equity - equity_value,
            "reason":  f"Top up equity to {TARGET['equity']*100:.0f}% target",
        })

    # --- "Other" bucket: sell everything (not in any target bucket) ---
    for p in buckets["other"]:
        if p["symbol"] in KEEP_TICKERS:
            continue  # Change 1: explicitly kept (e.g. IBIT)
        if p["market_value"] < MIN_TRADE_USD:
            continue
        price = prices.get(p["symbol"], 0)
        qty = p["quantity"]
        sells.append({
            "symbol":   p["symbol"],
            "quantity": qty,
            "dollars":  p["market_value"],
            "reason":   "Not in target allocation — liquidate",
        })

    # --- Energy bucket ---
    energy_value = sum(p["market_value"] for p in buckets["energy"])
    _add_bucket_trades(
        "energy", energy_value, target_energy,
        buckets["energy"], ENERGY_TICKERS, prices, sells, buys,
    )

    # --- TIPS bucket ---
    tips_value = sum(p["market_value"] for p in buckets["tips"])
    _add_bucket_trades(
        "tips", tips_value, target_tips,
        buckets["tips"], TIPS_TICKERS, prices, sells, buys,
    )

    # --- Commodities bucket (Change 2: split buy override) ---
    comm_value = sum(p["market_value"] for p in buckets["commodities"])
    comm_diff = target_commodities - comm_value
    if comm_diff > MIN_TRADE_USD:
        # Use the explicit split instead of a single-ticker buy
        for leg in COMMODITIES_BUY_OVERRIDE:
            buys.append(leg.copy())
    elif comm_diff < -MIN_TRADE_USD:
        # Over-weight: trim proportionally as normal
        _add_bucket_trades(
            "commodities", comm_value, target_commodities,
            buckets["commodities"], COMMODITY_TICKERS, prices, sells, buys,
        )

    # --- Cash: no trade needed (selling above generates the cash) ---
    # We'll note the target cash amount in the summary

    return total_value, sells, buys


def _pick_ticker(held: list[dict], preferred_set: set | list, fallback: str) -> str:
    """Pick the preferred ticker: first held, then first in preferred list, else fallback."""
    held_syms = {p["symbol"] for p in held}
    if isinstance(preferred_set, set):
        preferred_list = sorted(preferred_set)
    else:
        preferred_list = list(preferred_set)

    for sym in held_syms:
        if sym in preferred_list or sym in preferred_set:
            return sym
    for sym in preferred_list:
        return sym
    return fallback


def _add_bucket_trades(
    name: str,
    current_value: float,
    target_value: float,
    held: list[dict],
    preferred: list[str],
    prices: dict[str, float],
    sells: list[dict],
    buys: list[dict],
) -> None:
    diff = target_value - current_value
    if diff > MIN_TRADE_USD:
        # Need to buy
        ticker = _pick_ticker(held, preferred, preferred[0])
        buys.append({
            "symbol":  ticker,
            "dollars": diff,
            "reason":  f"Build {name} to {TARGET.get(name, 0)*100:.0f}% target",
        })
    elif diff < -MIN_TRADE_USD:
        # Need to sell (trim)
        trim = abs(diff)
        for p in held:
            price = prices.get(p["symbol"], 0)
            sell_usd = min(trim, p["market_value"])
            qty = round(sell_usd / price, 6) if price > 0 else 0
            if qty > 0 and sell_usd >= MIN_TRADE_USD:
                sells.append({
                    "symbol":   p["symbol"],
                    "quantity": qty,
                    "dollars":  sell_usd,
                    "reason":   f"Trim {name} to {TARGET.get(name, 0)*100:.0f}% target",
                })
            trim -= sell_usd
            if trim < MIN_TRADE_USD:
                break


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_holdings(cash: float, positions: list[dict], total: float) -> None:
    print("\n" + "=" * 60)
    print(f"  IRA HOLDINGS  (Account: {IRA_ACCOUNT_ID})")
    print("=" * 60)
    print(f"  {'Symbol':<10} {'Market Value':>14} {'% of Portfolio':>15}")
    print(f"  {'-'*10} {'-'*14} {'-'*15}")
    print(f"  {'CASH':<10} ${cash:>13,.2f} {cash/total*100:>14.1f}%")
    for p in sorted(positions, key=lambda x: -x["market_value"]):
        pct = p["market_value"] / total * 100 if total else 0
        print(f"  {p['symbol']:<10} ${p['market_value']:>13,.2f} {pct:>14.1f}%")
    print(f"  {'─'*10} {'─'*14}")
    print(f"  {'TOTAL':<10} ${total:>13,.2f}")
    print()


def print_proposed_trades(
    total: float,
    sells: list[dict],
    buys: list[dict],
    cash: float,
    target_cash: float,
) -> None:
    print("=" * 60)
    print("  PROPOSED REBALANCING TRADES")
    print("=" * 60)

    if sells:
        print("\n  SELLS (execute first):")
        for s in sells:
            print(f"    SELL  {s['symbol']:<8}  ${s['dollars']:>10,.2f}  ({s['reason']})")
    else:
        print("\n  SELLS: none needed")

    if buys:
        print("\n  BUYS (after sells settle):")
        for b in buys:
            print(f"    BUY   {b['symbol']:<8}  ${b['dollars']:>10,.2f}  ({b['reason']})")
    else:
        print("\n  BUYS: none needed")

    sell_total = sum(s["dollars"] for s in sells)
    buy_total  = sum(b["dollars"] for b in buys)
    cash_after = cash + sell_total - buy_total

    print(f"\n  Cash now:        ${cash:>10,.2f}")
    print(f"  + Sell proceeds: ${sell_total:>10,.2f}")
    print(f"  - Buy cost:      ${buy_total:>10,.2f}")
    print(f"  = Cash after:    ${cash_after:>10,.2f}  (target: ${target_cash:,.2f})")
    print()


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

def get_fresh_price(client: IRAClient, symbol: str) -> float:
    """Fetch a real-time price immediately before placing an order."""
    try:
        data = client.get_quotes([symbol])
        for q in data.get("quotes", []):
            sym = q.get("instrument", {}).get("symbol") or q.get("symbol", "")
            if sym.upper() == symbol.upper():
                price = float(q.get("last") or q.get("lastPrice") or q.get("mid") or 0)
                if price > 0:
                    return price
    except Exception as e:
        logger.warning("Fresh price fetch failed for %s: %s", symbol, e)
    return 0.0


def place_market_order(
    client: IRAClient,
    symbol: str,
    side: str,
    dollars: float,
    reason: str,
) -> str | None:
    """
    Place a dollar-amount market order. Returns orderId on success, None on failure.
    Public.com requires market orders for fractional share quantities.
    IRA-safe: equities only, no options, no margin.
    """
    amount_str = f"{dollars:.2f}"
    logger.info("Placing %s %s $%s (%s)", side, symbol, amount_str, reason)
    try:
        result = client.place_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            amount=amount_str,
        )
        order_id = result.get("orderId", "")
        print(f"    ✓ {side} {symbol}  ${amount_str}  orderId={order_id}")
        return order_id
    except Exception as e:
        print(f"    ✗ {side} {symbol} FAILED: {e}")
        logger.error("Order failed: %s", e)
        return None


def wait_for_fills(client: IRAClient, order_ids: list[str], timeout_sec: int = 120) -> None:
    """Poll order statuses until all are filled or timeout."""
    import requests
    if not order_ids:
        return
    print(f"\n  Waiting for {len(order_ids)} order(s) to fill (up to {timeout_sec}s)...")
    deadline = time.time() + timeout_sec
    pending = set(order_ids)
    while pending and time.time() < deadline:
        time.sleep(5)
        still_pending = set()
        for oid in pending:
            try:
                status = client.get_order(oid)
                filled = float(status.get("filledQuantity") or status.get("filled_size") or 0)
                state  = status.get("status") or status.get("orderStatus") or ""
                if filled > 0 or state in ("FILLED", "COMPLETED", "CLOSED"):
                    print(f"    ✓ Order {oid} filled")
                else:
                    still_pending.add(oid)
            except Exception:
                still_pending.add(oid)
        pending = still_pending
    if pending:
        print(f"  ⚠ Orders still pending after {timeout_sec}s: {pending}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("  IRA REBALANCER — Public.com Roth IRA")
    print(f"  Account: {IRA_ACCOUNT_ID}  (hardcoded — never touches bot account)")
    print("=" * 60)

    client = IRAClient()

    # ── Step 1: Read holdings ─────────────────────────────────────────────
    print("\nStep 1: Reading IRA holdings...")
    cash, positions = get_ira_snapshot(client)

    all_syms = [p["symbol"] for p in positions]
    prices = get_live_prices(client, all_syms) if all_syms else {}

    # Fill in market values from live prices where missing
    for p in positions:
        if p["market_value"] == 0 and prices.get(p["symbol"], 0) > 0:
            p["market_value"] = p["quantity"] * prices[p["symbol"]]

    pos_value  = sum(p["market_value"] for p in positions)
    total_value = cash + pos_value

    if total_value == 0:
        print("  ⚠ Could not determine portfolio value. Check account ID and API credentials.")
        return

    print_holdings(cash, positions, total_value)

    # ── Step 2: Propose trades ────────────────────────────────────────────
    print("Step 2: Calculating rebalancing trades...")
    total_value, sells, buys = compute_trades(cash, positions, prices)
    target_cash = total_value * TARGET["cash"]
    print_proposed_trades(total_value, sells, buys, cash, target_cash)

    if not sells and not buys:
        print("  Portfolio is already at target allocation. No trades needed.")
        return

    # ── Step 3: Confirmation ──────────────────────────────────────────────
    print("Step 3: Waiting for confirmation.")
    print("  Review the trades above carefully.")
    print("  Type 'confirmed' to execute, or anything else to abort:")
    print()
    try:
        response = input("  > ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborted.")
        return

    if response != "confirmed":
        print("  Aborted — no trades placed.")
        return

    # ── Step 4: Execute sells first ───────────────────────────────────────
    sell_order_ids = []
    if sells:
        print("\nStep 4a: Executing sells...")
        for s in sells:
            oid = place_market_order(client, s["symbol"], "SELL", s["dollars"], s["reason"])
            if oid:
                sell_order_ids.append(oid)

        wait_for_fills(client, sell_order_ids)

    # ── Step 4b: Execute buys ─────────────────────────────────────────────
    if buys:
        print("\nStep 4b: Executing buys...")
        buy_order_ids = []
        for b in buys:
            oid = place_market_order(client, b["symbol"], "BUY", b["dollars"], b["reason"])
            if oid:
                buy_order_ids.append(oid)

        wait_for_fills(client, buy_order_ids)

    # ── Step 5: Confirm bot account unaffected ────────────────────────────
    print("\nStep 5: Confirming sentiment bot account is unaffected...")
    bot_client = PublicClient()
    bot_account_id = bot_client.get_account_id()
    print(f"  Sentiment bot account ID: {bot_account_id}")
    if bot_account_id == IRA_ACCOUNT_ID:
        print("  ⚠ WARNING: bot resolved to IRA account — investigate immediately!")
    else:
        print(f"  ✓ Bot account ({bot_account_id}) is separate from IRA ({IRA_ACCOUNT_ID})")
        try:
            bal = bot_client.get_account_balance()
            print(f"  ✓ Bot portfolio value: ${bal['portfolio_value']:,.2f}  cash: ${bal['cash_balance']:,.2f}")
        except Exception as e:
            print(f"  ⚠ Could not fetch bot balance: {e}")

    print("\n  Rebalancing complete.\n")


if __name__ == "__main__":
    main()
