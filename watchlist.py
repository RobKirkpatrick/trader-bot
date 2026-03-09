#!/usr/bin/env python3
"""
Carpet Bagger watchlist viewer.

Uses the AWS CLI (no boto3 credential setup needed).

Usage:
    python3 watchlist.py           # show watching + bought
    python3 watchlist.py --all     # include today's closed records too
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

_TABLE  = "carpet-bagger-watchlist"
_REGION = "us-east-2"

_SPORT_LABELS = {
    "KXNBAGAMES":   "NBA",
    "KXNBAGAME":    "NBA",
    "KXNHLGAME":    "NHL",
    "KXNCAABGAME":  "NCAAB-M",
    "KXNCAABBGAME": "NCAAB-M",
    "KXNCAAWBGAME": "NCAAW",
    "KXMLBGAME":    "MLB",
    "KXMLSGAME":    "MLS",
}

STATUS_COLOR = {
    "watching": "\033[33m",   # yellow
    "bought":   "\033[32m",   # green
    "closed":   "\033[90m",   # gray
}
RESET = "\033[0m"
BOLD  = "\033[1m"


def _label(sport: str) -> str:
    return _SPORT_LABELS.get(sport, sport.replace("KX", "").replace("GAME", "")[:8])


def _scan(filter_expr: str, attr_names: dict, attr_values: dict) -> list[dict]:
    cmd = [
        "aws", "dynamodb", "scan",
        "--table-name", _TABLE,
        "--region", _REGION,
        "--filter-expression", filter_expr,
        "--expression-attribute-names", json.dumps(attr_names),
        "--expression-attribute-values", json.dumps(attr_values),
        "--output", "json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error querying DynamoDB:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout).get("Items", [])


def main():
    show_all = "--all" in sys.argv
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if show_all:
        items = _scan(
            "(#s IN (:w, :b)) OR (#s = :c AND begins_with(last_updated, :today))",
            {"#s": "status"},
            {":w": {"S":"watching"}, ":b": {"S":"bought"}, ":c": {"S":"closed"}, ":today": {"S":today}},
        )
    else:
        items = _scan(
            "#s IN (:w, :b)",
            {"#s": "status"},
            {":w": {"S":"watching"}, ":b": {"S":"bought"}},
        )

    if not items:
        print("No active records.")
        return

    def sort_key(i):
        s = i.get("status", {}).get("S", "")
        return (0 if s == "bought" else 1 if s == "watching" else 2,
                i.get("sport", {}).get("S", ""))

    items.sort(key=sort_key)

    watching = sum(1 for i in items if i.get("status", {}).get("S") == "watching")
    bought   = sum(1 for i in items if i.get("status", {}).get("S") == "bought")

    print(f"\n{BOLD}Carpet Bagger Watchlist — {today}{RESET}")
    print(f"  {BOLD}{bought}{RESET} bought  |  {BOLD}{watching}{RESET} watching\n")
    print(f"{'Sport':<10} {'Status':<10} {'Pre%':>5} {'Now%':>5} {'Entry':>6} {'Cts':>4}  {'Pick':<6}  Matchup")
    print("─" * 90)

    for i in items:
        status      = i.get("status",        {}).get("S", "")
        sport       = i.get("sport",         {}).get("S", "")
        teams       = i.get("teams",         {}).get("S", "")
        ticker      = i.get("market_ticker", {}).get("S", "")
        pre_prob    = float(i.get("pre_game_prob", {}).get("N", 0))
        cur_prob    = float(i.get("current_prob",  {}).get("N", 0))
        entry_price = float(i.get("entry_price",   {}).get("N", 0))
        contracts   = int(float(i.get("contract_count", {}).get("N", 0)))
        pnl         = float(i.get("pnl", {}).get("N", 0))

        # Extract the picked team abbreviation from the ticker suffix (last segment after final '-')
        pick = ticker.rsplit("-", 1)[-1] if "-" in ticker else "?"

        color     = STATUS_COLOR.get(status, "")
        label     = _label(sport)
        entry_str = f"{entry_price:.0%}" if entry_price else "  —  "
        cts_str   = str(contracts) if contracts else " —"
        pnl_str   = f"  P&L ${pnl:+.2f}" if status == "closed" and pnl else ""

        # Clean up the matchup string
        matchup = teams.replace(" Winner?", "").replace(" winner?", "").strip()

        print(f"{color}{label:<10} {status:<10} {pre_prob:>4.0%} {cur_prob:>5.0%} "
              f"{entry_str:>6} {cts_str:>4}  {BOLD}{pick:<6}{RESET}{color}  {matchup[:40]}{pnl_str}{RESET}")

    print()


if __name__ == "__main__":
    main()
