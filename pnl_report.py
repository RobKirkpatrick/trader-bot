#!/usr/bin/env python3
"""
TraderBot P&L Report
====================
Pulls live data from Public.com, Kalshi, and DynamoDB to produce a dated CSV.

Run:
    source .venv/bin/activate && python3 pnl_report.py

Output:
    pnl_report_YYYY-MM-DD.csv  — full breakdown (open in Excel / Numbers)
    Summary printed to console
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# USER INPUTS  ← edit these before running
# ---------------------------------------------------------------------------
TRACKING_START_DATE    = "YYYY-MM-DD"   # set to the date you first funded the accounts
PUBLIC_INITIAL_DEPOSIT = 0.00           # set to your total Public.com deposits (cash moved in, not current value)
AWS_MONTHLY_COST       = 5.00           # estimated monthly AWS charges (Lambda, CW, SM, etc.) — adjust to your usage
CLAUDE_MONTHLY_COST    = 20.00          # Claude Pro subscription + API usage — adjust to your plan
OTHER_MONTHLY_COST     = 0.00           # any other recurring costs (data feeds, broker fees, etc.)

# Kalshi deposit/withdrawal ledger — add a row each time you move money in or out
# Format: ("YYYY-MM-DD", amount)  positive = deposit, negative = withdrawal
# Example:
#   ("2026-01-01", +100.00),   # initial deposit
#   ("2026-01-15", -50.00),    # withdrew winnings
KALSHI_TRANSACTIONS = [
    # ("YYYY-MM-DD", +0.00),   # add your transactions here
]
# Net cash into Kalshi (deposits minus withdrawals) — used for P&L calculation
# Negative = you've pulled out more than you put in (a good thing)
KALSHI_NET_CASH_IN = sum(amt for _, amt in KALSHI_TRANSACTIONS)
KALSHI_INITIAL_DEPOSIT = sum(amt for _, amt in KALSHI_TRANSACTIONS if amt > 0)
# ---------------------------------------------------------------------------

# Bootstrap .env + settings before importing project modules
from dotenv import load_dotenv
load_dotenv()

import boto3
from config.settings import settings
from broker.public_client import PublicClient


def _load_secrets_from_sm() -> dict:
    """Pull trading-bot/secrets from Secrets Manager to get Kalshi keys."""
    try:
        import json as _json
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        resp = sm.get_secret_value(SecretId=settings.AWS_SECRET_NAME)
        return _json.loads(resp.get("SecretString") or "{}")
    except Exception as exc:
        print(f"  WARNING: Secrets Manager unavailable: {exc}")
        return {}


# Inject Kalshi keys from Secrets Manager into env if not already present
if not os.getenv("KALSHI_API_KEY"):
    _sm_secrets = _load_secrets_from_sm()
    for _k in ("KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY"):
        if _k in _sm_secrets and not os.getenv(_k):
            os.environ[_k] = _sm_secrets[_k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days_running() -> int:
    start = datetime.strptime(TRACKING_START_DATE, "%Y-%m-%d").date()
    return max((date.today() - start).days, 1)


def _prorated_cost(monthly: float, days: int) -> float:
    """Prorate a monthly cost to the number of days running."""
    return monthly * days / 30.44


def _dynamodb_scan_all(table_name: str, region: str = "us-east-2") -> list[dict]:
    """Paginated full scan of a DynamoDB table."""
    db = boto3.client("dynamodb", region_name=region)
    items = []
    kwargs: dict = {"TableName": table_name}
    while True:
        resp = db.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _ddb_str(item: dict, key: str) -> str:
    return item.get(key, {}).get("S", "")


def _ddb_float(item: dict, key: str) -> float:
    raw = item.get(key, {}).get("N", "0")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def _ddb_bool(item: dict, key: str) -> bool:
    return bool(item.get(key, {}).get("BOOL", False))


# ---------------------------------------------------------------------------
# Public.com — current state
# ---------------------------------------------------------------------------

def fetch_public_state() -> dict:
    print("Fetching Public.com account state...", flush=True)
    client = PublicClient()
    portfolio = client.get_portfolio()
    bp = portfolio.get("buyingPower", {})
    cash_balance = float(bp.get("cashOnlyBuyingPower") or bp.get("buyingPower") or 0)
    buying_power = float(bp.get("buyingPower") or bp.get("cashOnlyBuyingPower") or 0)

    raw_positions = portfolio.get("positions", [])
    open_positions = []
    position_value = 0.0
    for p in raw_positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        cur_val = float(p.get("currentValue") or 0)
        position_value += cur_val
        cb = p.get("costBasis", {})
        if isinstance(cb, dict):
            total_cost = float(cb.get("totalCost") or 0)
            gain_val   = float(cb.get("gainValue") or 0)
            gain_pct   = float(cb.get("gainPercentage") or 0)
        else:
            total_cost = cur_val
            gain_val   = 0.0
            gain_pct   = 0.0
        open_positions.append({
            "symbol":         sym,
            "cost_basis":     total_cost,
            "cur_value":      cur_val,
            "unrealized_pnl": gain_val,
            "unrealized_pct": gain_pct,
        })

    # Total = cash on hand + current market value of all open positions
    portfolio_value  = cash_balance + position_value
    unrealized_total = sum(p["unrealized_pnl"] for p in open_positions)
    open_cost_total  = sum(p["cost_basis"]     for p in open_positions)

    # Try to fetch order history (status varies by broker — try common values)
    orders = []
    for status in ("filled", "closed", "FILLED", "CLOSED"):
        try:
            raw = client.get_orders(status=status)
            if raw:
                orders = raw
                break
        except Exception:
            continue

    return {
        "cash_balance":     cash_balance,
        "buying_power":     buying_power,
        "portfolio_value":  portfolio_value,
        "open_positions":   open_positions,
        "open_cost_total":  open_cost_total,
        "unrealized_total": unrealized_total,
        "orders":           orders,
    }


# ---------------------------------------------------------------------------
# Public.com — trade history from DynamoDB
# ---------------------------------------------------------------------------

def fetch_public_trades() -> list[dict]:
    print("Fetching Public.com trade log from DynamoDB...", flush=True)
    try:
        items = _dynamodb_scan_all("trading-bot-logs")
    except Exception as exc:
        print(f"  WARNING: DynamoDB scan failed: {exc}")
        return []

    trades = []
    for item in items:
        if _ddb_str(item, "type") != "agent_decision":
            continue
        action = _ddb_str(item, "action_taken")
        if "order" not in action.lower() and "placed" not in action.lower():
            continue
        if "error" in action.lower():
            continue
        trades.append({
            "timestamp":     _ddb_str(item, "timestamp"),
            "symbol":        _ddb_str(item, "symbol"),
            "confidence":    _ddb_str(item, "confidence"),
            "position_size": _ddb_float(item, "position_size"),
            "action":        action,
            "order_result":  _ddb_str(item, "order_result"),
            "cash_before":   _ddb_float(item, "cash_balance"),
        })

    trades.sort(key=lambda t: t["timestamp"])
    print(f"  Found {len(trades)} Public.com trades in DynamoDB")
    return trades


# ---------------------------------------------------------------------------
# Kalshi — current state + trade history
# ---------------------------------------------------------------------------

def fetch_kalshi_state() -> dict:
    print("Fetching Kalshi account state...", flush=True)
    try:
        from carpet_bagger.kalshi_client import KalshiClient
        kalshi_key = os.getenv("KALSHI_API_KEY") or getattr(settings, "KALSHI_API_KEY", "")
        kalshi_pem = os.getenv("KALSHI_RSA_PRIVATE_KEY") or getattr(settings, "KALSHI_RSA_PRIVATE_KEY", "")
        if not kalshi_key or not kalshi_pem:
            raise ValueError("KALSHI_API_KEY / KALSHI_RSA_PRIVATE_KEY not in environment")
        client = KalshiClient(api_key=kalshi_key, rsa_private_key_pem=kalshi_pem)
        bal_data  = client._request("GET", "/portfolio/balance")
        balance   = bal_data.get("balance", 0) / 100.0  # available cash in dollars
        positions = client.get_positions()
        open_pos  = []
        open_exposure = 0.0
        for p in positions:
            exp = abs(p.get("market_exposure", 0)) / 100.0
            open_exposure += exp
            open_pos.append({
                "ticker":    p.get("ticker", ""),
                "contracts": abs(int(p.get("position", 0))),
                "exposure":  exp,
            })
        # total = cash on hand + at-risk capital in open positions
        total_value = balance + open_exposure
        print(f"  Kalshi: cash=${balance:.2f}  open_exposure=${open_exposure:.2f}  total=${total_value:.2f}")
        return {"balance": balance, "portfolio_value": open_exposure, "total_value": total_value, "open_positions": open_pos}
    except Exception as exc:
        print(f"  WARNING: Kalshi API unavailable: {exc}")
        return {"balance": 0.0, "portfolio_value": 0.0, "total_value": 0.0, "open_positions": []}


def fetch_kalshi_trades() -> list[dict]:
    print("Fetching Kalshi trade history from DynamoDB...", flush=True)
    try:
        items = _dynamodb_scan_all("carpet-bagger-watchlist")
    except Exception as exc:
        print(f"  WARNING: Kalshi DynamoDB scan failed: {exc}")
        return []

    trades = []
    for item in items:
        status = _ddb_str(item, "status")
        if status != "closed":
            continue
        pnl = _ddb_float(item, "pnl")
        if pnl == 0.0:   # skip pushes (watched but never bought, or $0 settlement)
            continue
        trades.append({
            "market_ticker":   _ddb_str(item, "market_ticker"),
            "sport":           _ddb_str(item, "sport"),
            "teams":           _ddb_str(item, "teams"),
            "trigger_time":    _ddb_str(item, "trigger_time"),
            "last_updated":    _ddb_str(item, "last_updated"),
            "entry_price":     _ddb_float(item, "entry_price"),
            "position_size":   _ddb_float(item, "position_size"),
            "contract_count":  int(_ddb_float(item, "contract_count")),
            "pnl":             pnl,
            "result":          "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "PUSH"),
        })

    trades.sort(key=lambda t: t["trigger_time"])
    print(f"  Found {len(trades)} closed Kalshi trades")
    return trades


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_csv(
    path: str,
    public: dict,
    public_trades: list[dict],
    kalshi: dict,
    kalshi_trades: list[dict],
    days: int,
) -> None:
    rows = []

    def blank():
        rows.append([])

    def header(text):
        rows.append([f"=== {text} ==="])

    def kv(label, value):
        rows.append([label, value])

    # ── Section 1: Summary ──────────────────────────────────────────────────
    kalshi_total_deposited  = sum(amt for _, amt in KALSHI_TRANSACTIONS if amt > 0)
    kalshi_total_withdrawn  = abs(sum(amt for _, amt in KALSHI_TRANSACTIONS if amt < 0))

    public_cur    = public["portfolio_value"]
    kalshi_cur    = kalshi["total_value"]
    total_cur     = public_cur + kalshi_cur

    kalshi_unrealized = sum(p["exposure"] for p in kalshi["open_positions"])

    # P&L split: realized (closed positions) vs unrealized (still open)
    public_gross_pnl  = public_cur - PUBLIC_INITIAL_DEPOSIT
    public_unrealized = public.get("unrealized_total", 0.0)
    public_realized   = public_gross_pnl - public_unrealized
    kalshi_gross_pnl  = kalshi_cur + kalshi_total_withdrawn - kalshi_total_deposited
    realized_gross_pnl = public_realized + kalshi_gross_pnl

    total_deposited = PUBLIC_INITIAL_DEPOSIT + kalshi_total_deposited

    aws_cost     = _prorated_cost(AWS_MONTHLY_COST,    days)
    claude_cost  = _prorated_cost(CLAUDE_MONTHLY_COST, days)
    other_cost   = _prorated_cost(OTHER_MONTHLY_COST,  days)
    total_cost   = aws_cost + claude_cost + other_cost
    net_pnl      = realized_gross_pnl - total_cost
    net_pct      = net_pnl / total_deposited * 100 if total_deposited else 0

    # HYSA benchmark on total deposited capital
    hysa_gain = total_deposited * ((1 + settings.HYSA_APY) ** (days / 365) - 1)
    alpha     = net_pnl - hysa_gain

    header("SUMMARY")
    kv("Report date", date.today().isoformat())
    kv("Tracking start", TRACKING_START_DATE)
    kv("Days running", days)
    blank()
    kv("Public.com deposited", f"${PUBLIC_INITIAL_DEPOSIT:.2f}")
    kv("Kalshi deposited", f"${kalshi_total_deposited:.2f}")
    kv("Kalshi withdrawn", f"${kalshi_total_withdrawn:.2f}")
    kv("Total capital deployed", f"${total_deposited:.2f}")
    blank()
    kv("Public.com current value", f"${public_cur:.2f}")
    kv("Kalshi current value", f"${kalshi_cur:.2f}")
    kv("Total current value", f"${public_cur + kalshi_cur:.2f}")
    blank()
    kv("── Realized P&L (closed positions)", "")
    kv("  Public realized", f"${public_realized:+.2f}")
    kv("  Kalshi realized (incl. withdrawals)", f"${kalshi_gross_pnl:+.2f}")
    kv("  └─ Kalshi cash balance", f"${kalshi['balance']:.2f}")
    kv("  └─ Kalshi withdrawn", f"${kalshi_total_withdrawn:.2f}")
    kv("  Total realized P&L", f"${realized_gross_pnl:+.2f}")
    blank()
    kv("── Unrealized P&L (open positions, still moving)", "")
    kv("  Public unrealized", f"${public_unrealized:+.2f}  ({len(public['open_positions'])} positions)")
    kv("  Kalshi unrealized", f"${kalshi_unrealized:+.2f}  ({len(kalshi['open_positions'])} positions)")
    blank()
    kv("AWS compute (prorated)", f"-${aws_cost:.2f}")
    kv("Claude subscription (prorated)", f"-${claude_cost:.2f}")
    kv("Other costs (prorated)", f"-${other_cost:.2f}")
    kv("Total costs", f"-${total_cost:.2f}")
    blank()
    kv("NET P&L on realized (after costs)", f"${net_pnl:+.2f}  ({net_pct:+.2f}%)")
    kv(f"HYSA benchmark ({settings.HYSA_APY:.1%} APY, {days}d)", f"${hysa_gain:.2f}")
    kv("Alpha vs HYSA", f"${alpha:+.2f}")
    blank()

    # ── Section 2: Public.com balance ───────────────────────────────────────
    header("PUBLIC.COM — CURRENT ACCOUNT")
    kv("Cash / buying power", f"${public['cash_balance']:.2f}")
    kv("Portfolio value", f"${public['portfolio_value']:.2f}")
    blank()

    # ── Section 3: Public.com open positions ────────────────────────────────
    header("PUBLIC.COM — OPEN POSITIONS")
    rows.append(["Symbol", "Cost Basis", "Current Value", "Unrealized P&L", "Unrealized %"])
    for p in public["open_positions"]:
        rows.append([
            p["symbol"],
            f"${p['cost_basis']:.2f}",
            f"${p['cur_value']:.2f}",
            f"${p['unrealized_pnl']:+.2f}",
            f"{p['unrealized_pct']:+.2f}%",
        ])
    if not public["open_positions"]:
        rows.append(["No open positions."])
    blank()

    # ── Section 4: Public.com trade history ─────────────────────────────────
    header("PUBLIC.COM — TRADE HISTORY (from DynamoDB)")
    rows.append(["Timestamp (UTC)", "Symbol", "Size ($)", "Confidence", "Action", "Cash Before"])
    for t in public_trades:
        rows.append([
            t["timestamp"][:19],
            t["symbol"],
            f"${t['position_size']:.2f}",
            t["confidence"],
            t["action"],
            f"${t['cash_before']:.2f}",
        ])
    if not public_trades:
        rows.append(["No trade history found."])
    blank()

    # ── Section 5: Kalshi balance ────────────────────────────────────────────
    header("KALSHI — CURRENT ACCOUNT")
    kv("Cash balance", f"${kalshi['balance']:.2f}")
    kv("Open position value", f"${kalshi['portfolio_value']:.2f}")
    kv("Open position exposure", f"${kalshi_unrealized:.2f}")
    kv("Effective total value", f"${kalshi_cur:.2f}")
    blank()

    # ── Section 6: Kalshi open positions ────────────────────────────────────
    header("KALSHI — OPEN POSITIONS")
    rows.append(["Ticker", "Contracts", "Exposure ($)"])
    for p in kalshi["open_positions"]:
        rows.append([p["ticker"], p["contracts"], f"${p['exposure']:.2f}"])
    if not kalshi["open_positions"]:
        rows.append(["No open positions."])
    blank()

    # ── Section 7: Kalshi trade history ─────────────────────────────────────
    header("KALSHI — CLOSED TRADE HISTORY (from DynamoDB)")
    rows.append([
        "Trigger Time", "Sport", "Teams", "Entry Price",
        "Size ($)", "Contracts", "P&L ($)", "Result",
    ])
    for t in kalshi_trades:
        rows.append([
            t["trigger_time"][:19],
            t["sport"],
            t["teams"],
            f"${t['entry_price']:.2f}",
            f"${t['position_size']:.2f}",
            t["contract_count"],
            f"${t['pnl']:+.2f}",
            t["result"],
        ])
    if not kalshi_trades:
        rows.append(["No closed trades found."])
    blank()

    # ── Section 8: Cost breakdown ────────────────────────────────────────────
    header("COST BREAKDOWN")
    rows.append(["Category", "Monthly Rate", f"Prorated ({days}d)", "Note"])
    rows.append(["AWS compute",      f"${AWS_MONTHLY_COST:.2f}",    f"${aws_cost:.2f}",    "Lambda, CW, SM, EventBridge"])
    rows.append(["Claude Pro",       f"${CLAUDE_MONTHLY_COST:.2f}", f"${claude_cost:.2f}", "Claude subscription"])
    rows.append(["Other",            f"${OTHER_MONTHLY_COST:.2f}",  f"${other_cost:.2f}",  ""])
    rows.append(["TOTAL",            "",                             f"${total_cost:.2f}",  ""])

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(public: dict, kalshi: dict, kalshi_trades: list[dict], public_trades: list[dict], days: int) -> None:
    kalshi_total_deposited = sum(amt for _, amt in KALSHI_TRANSACTIONS if amt > 0)
    kalshi_total_withdrawn = abs(sum(amt for _, amt in KALSHI_TRANSACTIONS if amt < 0))
    total_deposited = PUBLIC_INITIAL_DEPOSIT + kalshi_total_deposited

    public_cur  = public["portfolio_value"]
    kalshi_cur  = kalshi["total_value"]
    total_cur   = public_cur + kalshi_cur

    public_gross_pnl   = public_cur - PUBLIC_INITIAL_DEPOSIT
    public_unrealized  = public.get("unrealized_total", 0.0)
    public_realized    = public_gross_pnl - public_unrealized
    kalshi_gross_pnl   = kalshi_cur + kalshi_total_withdrawn - kalshi_total_deposited
    gross_pnl          = public_gross_pnl + kalshi_gross_pnl
    realized_gross_pnl = public_realized + kalshi_gross_pnl  # Kalshi all realized (settled contracts)

    total_cost  = sum([
        _prorated_cost(AWS_MONTHLY_COST, days),
        _prorated_cost(CLAUDE_MONTHLY_COST, days),
        _prorated_cost(OTHER_MONTHLY_COST, days),
    ])
    net_pnl     = realized_gross_pnl - total_cost
    net_pct     = net_pnl / total_deposited * 100 if total_deposited else 0
    hysa_gain   = total_deposited * ((1 + settings.HYSA_APY) ** (days / 365) - 1)
    alpha       = net_pnl - hysa_gain
    kalshi_wins   = sum(1 for t in kalshi_trades if t["pnl"] > 0)
    kalshi_losses = sum(1 for t in kalshi_trades if t["pnl"] < 0)

    W = 54
    print("=" * W)
    print("  TraderBot P&L Report — " + date.today().isoformat())
    print("=" * W)
    print(f"  Days running:           {days}d  (since {TRACKING_START_DATE})")
    print(f"  Total capital deployed: ${total_deposited:.2f}")
    print()
    print(f"  Public.com value:       ${public_cur:.2f}  (deposited ${PUBLIC_INITIAL_DEPOSIT:.2f})")
    print(f"  Kalshi total:           ${kalshi['total_value']:.2f}  (cash ${kalshi['balance']:.2f} + positions ${kalshi['portfolio_value']:.2f})")
    print(f"    deposited ${kalshi_total_deposited:.2f}, withdrew ${kalshi_total_withdrawn:.2f}")
    print(f"  Total current:          ${total_cur:.2f}")
    print()
    print(f"  ── Realized P&L (closed positions) ──")
    print(f"  Public realized:        ${public_realized:+.2f}")
    print(f"  Kalshi realized:        ${kalshi_gross_pnl:+.2f}  (incl. ${kalshi_total_withdrawn:.2f} withdrawn)")
    print(f"  Total realized:         ${realized_gross_pnl:+.2f}")
    print()
    print(f"  ── Unrealized (open positions, still moving) ──")
    print(f"  Public unrealized:      ${public_unrealized:+.2f}  ({len(public['open_positions'])} positions)")
    print()
    print(f"  Costs (prorated):      -${total_cost:.2f}")
    print(f"  Net P&L (realized):     ${net_pnl:+.2f}  ({net_pct:+.2f}% on capital)")
    print(f"  HYSA benchmark:         ${hysa_gain:.2f}")
    print(f"  Alpha vs HYSA:          ${alpha:+.2f}")
    print()
    print(f"  Public trades logged:   {len(public_trades)}")
    print(f"  Kalshi closed:          {len(kalshi_trades)}  ({kalshi_wins}W {kalshi_losses}L)")
    print(f"  Kalshi open pos:        {len(kalshi['open_positions'])}")
    print("=" * W)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print()
    print("TraderBot P&L Report")
    print(f"Tracking start: {TRACKING_START_DATE} | Today: {date.today().isoformat()}")
    print()

    days = _days_running()

    public        = fetch_public_state()
    public_trades = fetch_public_trades()
    kalshi        = fetch_kalshi_state()
    kalshi_trades = fetch_kalshi_trades()

    out_path = f"pnl_report_{date.today().isoformat()}.csv"
    print(f"\nWriting {out_path}...")
    write_csv(out_path, public, public_trades, kalshi, kalshi_trades, days)

    print()
    print_summary(public, kalshi, kalshi_trades, public_trades, days)
    print(f"\nFull report: {out_path}")
