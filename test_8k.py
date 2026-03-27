#!/usr/bin/env python3
"""
test_8k.py — Standalone 8-K strategy tester (no broker, no DynamoDB).

Tests three things:
  1. Live EDGAR scan  — hits SEC EDGAR for today's 8-K filings on watchlist tickers
  2. Item parsing     — fetches + parses a real (known) filing to check regex matching
  3. Scoring logic    — runs synthetic item combos through score_filing() to verify output

Usage:
  python3 test_8k.py
"""

import logging
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from sentiment.edgar_monitor import (
    _load_cik_map,
    get_todays_filings,
    get_filing_text,
    parse_filing_items,
    score_filing,
    build_signal,
    scan_watchlist,
    _ITEM_SCORES,
)
from config.settings import settings

BOLD  = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"

WATCHLIST = [t for t in settings.WATCHLIST if t not in settings.BLACKLIST]

# ─────────────────────────────────────────────
# 1. Live EDGAR scan for today
# ─────────────────────────────────────────────
print(f"\n{BOLD}=== 1. Live EDGAR scan for {date.today().isoformat()} ==={RESET}")
print(f"  Checking {len(WATCHLIST)} tickers: {', '.join(WATCHLIST[:10])}{'...' if len(WATCHLIST) > 10 else ''}")

print("\n  [a] CIK resolution...")
cik_map = _load_cik_map(WATCHLIST)
resolved = len(cik_map)
missed = [t for t in WATCHLIST if t not in cik_map]
print(f"      Resolved: {resolved}/{len(WATCHLIST)}")
if missed:
    print(f"      {YELLOW}No CIK found for: {missed}{RESET}  (ETFs / indexes often missing from EDGAR)")

print("\n  [b] Today's 8-K filings...")
filings = get_todays_filings(WATCHLIST)
if not filings:
    print(f"      {YELLOW}No 8-K filings found for watchlist tickers today.{RESET}")
    print("      This is normal — 8-Ks are event-driven, not daily.")
else:
    print(f"      {GREEN}Found {len(filings)} filing(s):{RESET}")
    for f in filings:
        print(f"        {BOLD}{f['ticker']}{RESET}  accession={f['accession_number']}  url={f['filing_url']}")

print("\n  [c] Full scan_watchlist() (parse + score)...")
signals = scan_watchlist(WATCHLIST)
if not signals:
    print(f"      {YELLOW}No scored signals produced today.{RESET}")
else:
    for ticker, sig in signals.items():
        color = GREEN if sig["direction"] == "bullish" else RED
        print(f"      {color}{BOLD}{ticker}{RESET}  catalyst={sig['catalyst']}  "
              f"score={sig['score']:.1f}  items={sig['items']}  "
              f"direction={sig['direction']}  priority={sig['priority']}")

# ─────────────────────────────────────────────
# 2. Known-good filing parse test
#    Dynamically find the most recent Apple 8-K from EDGAR submissions
#    and parse it to confirm item detection works end-to-end.
# ─────────────────────────────────────────────
print(f"\n{BOLD}=== 2. Filing parse smoke test (real SEC document) ==={RESET}")
print("  Looking up Apple's most recent 8-K via EDGAR submissions API...")
try:
    from sentiment.edgar_monitor import _fetch_json, _SUBMISSIONS, _ITEM_PATTERNS
    AAPL_CIK = "0000320193"
    data    = _fetch_json(_SUBMISSIONS.format(cik=AAPL_CIK))
    recent  = data.get("filings", {}).get("recent", {})
    forms   = recent.get("form", [])
    dates   = recent.get("filingDate", [])
    accnos  = recent.get("accessionNumber", [])
    pdocs   = recent.get("primaryDocument", [])

    smoke_filing = None
    for form, filed, accno, doc in zip(forms, dates, accnos, pdocs):
        if form == "8-K":
            cik_short  = str(int(AAPL_CIK))
            acc_nodash = accno.replace("-", "")
            smoke_filing = {
                "ticker": "AAPL",
                "filed":  filed,
                "accno":  accno,
                "url":    f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc_nodash}/{doc}",
            }
            break

    if smoke_filing:
        print(f"  Found: AAPL 8-K filed {smoke_filing['filed']}  ({smoke_filing['accno']})")
        print(f"  URL: {smoke_filing['url']}")
        text = get_filing_text(smoke_filing["url"])
        if text:
            print(f"  Fetched {len(text)} chars of cleaned text")
            print(f"  Preview: {text[:300]!r}")
            items = [item for item, pat in _ITEM_PATTERNS.items() if pat.search(text)]
            items.sort()
            if items:
                print(f"  {GREEN}High-impact items detected: {items}{RESET}")
            else:
                print(f"  {YELLOW}No high-impact items in this filing (normal — most 8-Ks aren't high-impact).{RESET}")
                # Show any item references found in the text
                import re
                all_items = re.findall(r'\bItem\s+\d+\.\d+', text, re.IGNORECASE)
                if all_items:
                    print(f"  Items present in doc: {list(dict.fromkeys(all_items))[:10]}")
        else:
            print(f"  {RED}Empty text — fetch failed.{RESET}")
    else:
        print(f"  {YELLOW}No 8-K found in Apple's recent filings.{RESET}")
except Exception as exc:
    print(f"  {RED}Smoke test error: {exc}{RESET}")

# ─────────────────────────────────────────────
# 3. Scoring logic unit tests
# ─────────────────────────────────────────────
print(f"\n{BOLD}=== 3. Scoring logic (synthetic item lists) ==={RESET}")

test_cases = [
    (["2.01", "5.01", "5.02"], "completed_acquisition", "bullish", 1.0),
    (["2.01", "5.02"],         "acquisition_leadership","bullish", 0.9),
    (["2.01"],                 "acquisition",           "bullish", 0.8),
    (["1.05"],                 "cybersecurity",         "bearish", 0.8),
    (["3.01"],                 "delisting_risk",        "bearish", 0.9),
    (["2.02"],                 "earnings",              "bullish", 0.5),
    (["5.02"],                 "executive_change",      "bullish", 0.7),
    ([],                       "unknown",               "bullish", 0.0),
]

all_pass = True
for items, exp_catalyst, exp_dir, exp_score in test_cases:
    result = score_filing(items)
    ok = (
        result["score"]     == exp_score and
        result["catalyst"]  == exp_catalyst and
        result["direction"] == exp_dir
    )
    status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    if not ok:
        all_pass = False
    items_str = str(items) if items else "[]"
    print(f"  [{status}] items={items_str:<30}  "
          f"score={result['score']:.1f} (exp {exp_score:.1f})  "
          f"catalyst={result['catalyst']} (exp {exp_catalyst})  "
          f"dir={result['direction']} (exp {exp_dir})")

print()
if all_pass:
    print(f"  {GREEN}{BOLD}All scoring tests passed.{RESET}")
else:
    print(f"  {RED}{BOLD}Some scoring tests FAILED — scoring logic has a bug.{RESET}")

print()
