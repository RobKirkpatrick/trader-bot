import os
import sys
import argparse
from datetime import datetime
from dateutil import parser as dateparser
import requests
import pandas as pd
import matplotlib.pyplot as plt

API_BASE = "https://api.venmo.com/v1"

def get_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "PostmanRuntime/7.51.0",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache"
    }

def fetch_me(token):
    r = requests.get(f"{API_BASE}/me", headers=get_headers(token))
    if r.status_code != 200:
        print(f"Error: /me endpoint returned {r.status_code}", file=sys.stderr)
        return {}
    try:
        data = r.json()
        me_data = data.get("data", {})
        # username is nested in user object
        if isinstance(me_data, dict) and "user" in me_data:
            return me_data.get("user", {})
        return me_data
    except Exception as e:
        print(f"Error: Could not parse /me response: {e}", file=sys.stderr)
        return {}

def fetch_payments(token, actor_username=None, target_username=None, limit=50):
    url = f"{API_BASE}/payments"
    params = {"limit": limit}
    if actor_username:
        params["actor_username"] = actor_username
    if target_username:
        params["target_username"] = target_username
    while url:
        r = requests.get(url, headers=get_headers(token), params=params if url.endswith("/payments") else {})
        if r.status_code != 200:
            print(f"Error: {r.status_code} - {r.text}", file=sys.stderr)
        r.raise_for_status()
        body = r.json()
        for item in body.get("data", []):
            yield item
        # pagination: try common keys
        pagination = body.get("pagination", {}) or {}
        url = pagination.get("next") or body.get("next") or None
        params = None  # subsequent pages already include query in URL

def parse_date(item):
    for key in ("date", "created_time", "created_at", "datetime", "time"):
        val = item.get(key)
        if val:
            return dateparser.parse(val)
    # fallback: try 'date_created' etc.
    for v in item.values():
        if isinstance(v, str):
            try:
                return dateparser.parse(v)
            except Exception:
                pass

def extract_username(obj):
    """Extract username from a user object"""
    if not obj:
        return None
    if isinstance(obj, dict):
        # Check for nested user object first
        if "user" in obj and isinstance(obj["user"], dict):
            return obj["user"].get("username")
        # Then check direct username
        return obj.get("username")
    return None

def counterparty_username(payment, my_username):
    actor = payment.get("actor") or {}
    target = payment.get("target") or {}

    actor_un = extract_username(actor)
    target_un = extract_username(target)

    # If we're the actor, return the target
    if actor_un and actor_un == my_username:
        return target_un or actor_un
    # Otherwise return the actor
    return actor_un or target_un or "unknown"

def get_display_name(obj):
    """Extract display name from a user object"""
    if not obj:
        return None
    if isinstance(obj, dict):
        # Check for nested user object first
        if "user" in obj and isinstance(obj["user"], dict):
            return obj["user"].get("display_name")
        # Then check direct display_name
        return obj.get("display_name")
    return None

def counterparty_display_name(payment, my_username):
    actor = payment.get("actor") or {}
    target = payment.get("target") or {}

    actor_un = extract_username(actor)
    target_un = extract_username(target)

    # If we're the actor, return target's display name
    if actor_un and actor_un == my_username:
        return get_display_name(target)
    # Otherwise return actor's display name
    return get_display_name(actor)

def amount_value(payment):
    # expects numeric in 'amount' or nested
    a = payment.get("amount")
    try:
        return float(a)
    except Exception:
        return 0.0

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", help="start date YYYY-MM-DD", required=False)
    p.add_argument("--end", help="end date YYYY-MM-DD", required=False)
    p.add_argument("--actor_username", help="filter by actor username", required=False)
    p.add_argument("--target_username", help="filter by target username", required=False)
    p.add_argument("--limit", type=int, default=50, help="page size")
    p.add_argument("--output", help="export results to CSV file", required=False)
    p.add_argument("--verbose", action="store_true", help="include memos in output")
    p.add_argument(
        "--export-all",
        action="store_true",
        help="Export every transaction (raw rows) including memo, actor, and target details."
    )
    args = p.parse_args()

    # Try to read token from secrets folder first, then fall back to environment variable
    token = None
    secrets_file = os.path.join(os.path.dirname(__file__), "secrets", "token.txt")
    if os.path.exists(secrets_file):
        with open(secrets_file, "r") as f:
            token = f.read().strip()
    if not token:
        token = os.getenv("VENMO_TOKEN")
    if not token:
        print("Token not found in secrets/token.txt or VENMO_TOKEN environment variable", file=sys.stderr)
        sys.exit(1)

    # Get authenticated user's username from /me endpoint
    me = fetch_me(token)
    my_username = me.get("username")
    if not my_username:
        print("Error: Could not get username from /me endpoint. /me endpoint may be inaccessible.", file=sys.stderr)
        sys.exit(1)

    print(f"Authenticated as: {my_username}", file=sys.stderr)

    start_dt = dateparser.parse(args.start).replace(tzinfo=None) if args.start else None
    end_dt = dateparser.parse(args.end).replace(tzinfo=None) if args.end else None

    # Fetch payments with optional filters
    rows = []
    for pmt in fetch_payments(token, actor_username=args.actor_username, target_username=args.target_username, limit=args.limit):
        dt = parse_date(pmt)
        if dt is None:
            continue
        dt_naive = dt.replace(tzinfo=None)

        if start_dt and dt_naive < start_dt:
            continue
        if end_dt and dt_naive > end_dt:
            continue

        actor_obj = pmt.get("actor") or {}
        target_obj = pmt.get("target") or {}

        actor_un = extract_username(actor_obj)
        target_un = extract_username(target_obj)

        # Client-side filtering for actor/target usernames
        if args.actor_username and actor_un != args.actor_username:
            continue
        if args.target_username and target_un != args.target_username:
            continue

        # Skip self-payments (same user as both actor and target)
        if actor_un and target_un and actor_un == target_un:
            continue

        counterparty = counterparty_username(pmt, my_username)
        display_name = counterparty_display_name(pmt, my_username)

        # Determine direction: are we the actor (sender) or target (recipient)?
        is_outgoing = actor_un == my_username
        direction = "Sent" if is_outgoing else "Received"

        # Skip if counterparty is yourself (edge case where API returns self-payment)
        if counterparty == my_username:
            continue

        # Skip transactions with excluded keywords in memo
        memo = pmt.get("note") or ""
        excluded_keywords = ["tip", "gas", "supplies"]
        if any(keyword.lower() in memo.lower() for keyword in excluded_keywords):
            continue

        actor_name = get_display_name(actor_obj) or actor_un
        target_name = get_display_name(target_obj) or target_un

        rows.append({
            "date": dt_naive,
            "direction": direction,
            "amount": amount_value(pmt),
            "memo": memo,

            # useful derived fields
            "counterparty": counterparty,
            "counterparty_display_name": display_name or counterparty,

            # explicit actor/target fields
            "actor_username": actor_un,
            "actor_display_name": actor_name,
            "target_username": target_un,
            "target_display_name": target_name,
        })

    if not rows:
        print("No payments in range.")
        return

    df = pd.DataFrame(rows)

    # If exporting all transactions, bypass grouping and export raw rows
    if args.export_all:
        export_df = df.sort_values("date", ascending=True)

        if args.output:
            export_df.to_csv(args.output, index=False)
            print(f"\nExported ALL transactions to {args.output}", file=sys.stderr)
        else:
            print("\n" + export_df.to_string(index=False))
        return

    # Otherwise: Group by counterparty and direction, get totals and display names
    grouped = df.groupby(["counterparty", "direction"], dropna=False).agg({
        "amount": "sum",
        "counterparty_display_name": "first",
        "memo": lambda x: " | ".join([m for m in x if m])  # concatenate memos
    }).reset_index()

    # Sort: direction first (Received before Sent), then by amount descending
    direction_order = {"Received": 0, "Sent": 1}
    grouped["direction_sort"] = grouped["direction"].map(direction_order)
    grouped = grouped.sort_values(["direction_sort", "amount"], ascending=[True, False]).drop("direction_sort", axis=1)

    # Display table
    if args.verbose:
        table_df = pd.DataFrame({
            "Username": grouped["counterparty"],
            "Display Name": grouped["counterparty_display_name"],
            "Direction": grouped["direction"],
            "Total": grouped["amount"],
            "Memo": grouped["memo"]
        })
    else:
        table_df = pd.DataFrame({
            "Username": grouped["counterparty"],
            "Display Name": grouped["counterparty_display_name"],
            "Direction": grouped["direction"],
            "Total": grouped["amount"]
        })

    # Export to CSV if requested
    if args.output:
        table_df.to_csv(args.output, index=False)
        print(f"\nExported to {args.output}", file=sys.stderr)

    print("\n" + table_df.to_string(index=False))

if __name__ == "__main__":
    main()
