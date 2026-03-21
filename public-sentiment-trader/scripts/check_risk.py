#!/usr/bin/env python3
"""
Check current risk posture: VIX regime, daily loss, PDT status, position concentration.

Usage:
    python public-sentiment-trader/scripts/check_risk.py

Environment variables required:
    PUBLIC_API_SECRET, PUBLIC_ACCOUNT_ID
    POLYGON_API_KEY  (VIX level)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from broker.public_client import PublicClient
from config.settings import settings


def _vix_level() -> float:
    """Fetch current VIX from Polygon. Returns 0.0 on failure."""
    import requests
    key = settings.POLYGON_API_KEY
    if not key:
        return 0.0
    try:
        resp = requests.get(
            "https://api.polygon.io/v2/last/trade/I:VIX",
            params={"apiKey": key}, timeout=5,
        )
        if resp.ok:
            return float(resp.json().get("results", {}).get("p", 0))
    except Exception:
        pass
    return 0.0


def main() -> None:
    client = PublicClient()
    bal    = client.get_account_balance()
    cash   = float(bal.get("cash_balance") or bal.get("buying_power") or 0)
    equity = float(bal.get("equity") or cash)

    positions = client.get_positions()
    n_pos = len(positions)

    daily_loss_limit  = equity * settings.DAILY_LOSS_LIMIT_PCT
    max_position_usd  = equity * settings.MAX_POSITION_PCT
    risk_tolerance    = settings.RISK_TOLERANCE
    options_enabled   = settings.OPTIONS_CALLS_ENABLED

    vix = _vix_level()
    if vix >= 30:
        vix_regime = "EXTREME FEAR (VIX ≥ 30) — 40% size reduction, options blocked"
        vix_color  = "⚠"
    elif vix >= 20:
        vix_regime = f"ELEVATED FEAR (VIX {vix:.1f}) — 20% size reduction"
        vix_color  = "~"
    elif vix > 0:
        vix_regime = f"CALM (VIX {vix:.1f}) — full position sizing"
        vix_color  = "✓"
    else:
        vix_regime = "VIX unavailable (POLYGON_API_KEY not set)"
        vix_color  = "?"

    # PDT check — count today's sells from DynamoDB (simplified: show guidance)
    pdt_warning = equity < 25_000

    print(f"\n{'='*56}")
    print(f"  RISK DASHBOARD — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"{'='*56}")
    print(f"  Risk tolerance:     {risk_tolerance.upper()}")
    print(f"  Cash / equity:      ${cash:,.2f} / ${equity:,.2f}")
    print(f"  Open positions:     {n_pos}")
    print(f"  Daily loss limit:   ${daily_loss_limit:,.2f}  ({settings.DAILY_LOSS_LIMIT_PCT:.0%} of equity)")
    print(f"  Max position size:  ${max_position_usd:,.2f}  ({settings.MAX_POSITION_PCT:.0%} of equity)")
    print(f"  Options (calls):    {'ENABLED' if options_enabled else 'DISABLED'}")
    print()
    print(f"  {vix_color} VIX regime: {vix_regime}")
    print()
    if pdt_warning:
        print(f"  ⚠ PDT WARNING: Account equity ${equity:,.2f} < $25,000")
        print(f"    Selling a position bought same-day counts as a round trip.")
        print(f"    4+ round trips in 5 days triggers PDT flag (90-day freeze).")
        print(f"    Bot enforces this automatically — no same-day sells.")
    else:
        print(f"  ✓ PDT: Account > $25k — no round-trip restriction")

    print()
    print(f"  Settings (change via .env or Secrets Manager):")
    print(f"    RISK_TOLERANCE={risk_tolerance}")
    print(f"    OPTIONS_CALLS_ENABLED={str(options_enabled).lower()}")
    print(f"    CARPET_BAGGER_ENABLED={str(settings.CARPET_BAGGER_ENABLED).lower()}")
    print(f"    CARPET_BAGGER_MAX_POSITION={settings.CARPET_BAGGER_MAX_POSITION:.2f}")
    print(f"    TRADING_PAUSED=false  # set to true for kill switch")
    print(f"{'='*56}\n")


if __name__ == "__main__":
    main()
