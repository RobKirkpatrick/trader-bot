"""
Options position reviewer — fetches all open options from Public.com,
gets current quotes/greeks, then asks Claude Sonnet for hold/close
recommendations on each position.

Run: source .venv/bin/activate && python3 options_review.py
"""

import json
import re
import sys
from datetime import datetime, date, timezone

import anthropic
from dotenv import load_dotenv

load_dotenv()

from broker.public_client import PublicClient
from config.settings import settings


# ── OSI symbol parser ────────────────────────────────────────────────────────

def parse_osi(osi: str) -> dict | None:
    """
    Parse an OCC/OSI option symbol into its components.
    Format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE_8DIGITS}
    e.g.  RIVN260320C00015000  →  RIVN, 2026-03-20, CALL, $15.00
    """
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', osi)
    if not m:
        return None
    underlying, date_str, cp, strike_raw = m.groups()
    try:
        expiry = datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying": underlying,
        "expiry":     expiry,
        "type":       "CALL" if cp == "C" else "PUT",
        "strike":     int(strike_raw) / 1000.0,
    }


# ── Fetch current option market price from the chain ─────────────────────────

def get_option_market_price(client: PublicClient, info: dict, osi: str) -> dict:
    """
    Look up the live bid/ask for a held option by pulling its expiry's chain.
    Returns {"bid": float, "ask": float, "mid": float} or zeros on failure.
    """
    try:
        chain = client.get_option_chain(
            symbol=info["underlying"],
            expiration=info["expiry"].strftime("%Y-%m-%d"),
            option_type=info["type"],
        )
        for c in chain:
            if c.get("optionSymbol", "").upper() == osi.upper():
                bid = float(c.get("bid") or 0)
                ask = float(c.get("ask") or 0)
                return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    except Exception as e:
        print(f"  [warn] chain lookup failed for {osi}: {e}")
    return {"bid": 0.0, "ask": 0.0, "mid": 0.0}


# ── Claude recommendation ─────────────────────────────────────────────────────

_SYSTEM = (
    "You are a conservative options trading advisor. "
    "You give clear, direct hold/close recommendations on existing options positions "
    "based on current market data, time-to-expiry, and unrealized P&L. "
    "You understand theta decay, intrinsic vs extrinsic value, and the risk of "
    "holding deep-ITM contracts vs. capturing gains now. "
    "Respond ONLY in valid JSON — no prose outside the JSON."
)

_USER_TEMPLATE = """\
Analyze this open options position and recommend: CLOSE (sell to close now), HOLD, or PARTIAL_CLOSE.

Position:
{position_json}

Respond in this exact JSON:
{{
  "recommendation": "CLOSE" | "HOLD" | "PARTIAL_CLOSE",
  "urgency": "immediate" | "today" | "monitor",
  "reasoning": "2–3 sentences explaining why",
  "key_risk": "biggest risk of NOT following recommendation",
  "ideal_exit_price": null or float
}}"""


def ask_claude(position_context: dict) -> dict:
    ai = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        msg = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    position_json=json.dumps(position_context, indent=2, default=str)
                )
            }],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        return {"recommendation": "ERROR", "reasoning": str(e), "urgency": "—", "key_risk": "—"}


# ── Close a position ─────────────────────────────────────────────────────────

def close_position(client: PublicClient, osi: str, qty: float) -> None:
    try:
        result = client.place_options_order(
            option_symbol=osi,
            side="SELL",
            quantity=str(int(qty)),
            order_type="MARKET",
        )
        print(f"  ✓ Close order submitted: orderId={result.get('orderId')}")
    except Exception as e:
        print(f"  ✗ Close order FAILED: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\nOptions Position Reviewer")
    print("=" * 60)

    client = PublicClient()
    today = date.today()

    # 1. Fetch all positions
    positions = client.get_positions()
    options_positions = []
    for p in positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        # Options symbols contain digits (OSI format)
        if re.search(r'\d', sym):
            options_positions.append((sym, p))

    if not options_positions:
        print("No open options positions found.")
        return

    print(f"Found {len(options_positions)} options position(s). Fetching data...\n")

    recommendations = []

    for osi, pos in options_positions:
        info = parse_osi(osi)
        if not info:
            print(f"  [skip] Could not parse OSI symbol: {osi}")
            continue

        qty = float(pos.get("quantity") or pos.get("shares") or 1)

        # Cost basis
        cost_basis = pos.get("costBasis")
        avg_cost = (
            float(cost_basis.get("unitCost")) if isinstance(cost_basis, dict)
            else float(cost_basis or 0)
        )
        total_cost = avg_cost * qty * 100  # options are priced per share, 100 shares/contract

        # DTE
        dte = (info["expiry"] - today).days

        # Current market price (from chain)
        print(f"[{osi}] fetching market price...")
        market = get_option_market_price(client, info, osi)
        current_mid = market["mid"]
        current_value = current_mid * qty * 100

        # Unrealized P&L
        unrealized_pnl = current_value - total_cost
        pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0

        # Greeks
        print(f"[{osi}] fetching greeks...")
        greeks = client.get_option_greeks(osi)

        # ITM check
        itm = (
            (info["type"] == "CALL" and current_mid > info["strike"]) or
            (info["type"] == "PUT"  and current_mid < info["strike"])
        )

        # Build context for Claude
        context = {
            "symbol":         osi,
            "underlying":     info["underlying"],
            "option_type":    info["type"],
            "strike":         info["strike"],
            "expiry":         info["expiry"].isoformat(),
            "dte":            dte,
            "contracts":      int(qty),
            "avg_cost_per_contract": avg_cost * 100,
            "total_cost_basis":      round(total_cost, 2),
            "current_bid":           market["bid"],
            "current_ask":           market["ask"],
            "current_mid":           round(current_mid, 4),
            "current_value":         round(current_value, 2),
            "unrealized_pnl":        round(unrealized_pnl, 2),
            "unrealized_pnl_pct":    round(pnl_pct, 1),
            "in_the_money":          itm,
            "greeks": {
                "delta":  greeks.get("delta", 0),
                "theta":  greeks.get("theta", 0),
                "vega":   greeks.get("vega", 0),
                "iv":     greeks.get("iv", 0),
            },
            "context_note": (
                "This is a real money position. Account is <$25k (PDT protected). "
                "Weigh theta decay vs. remaining intrinsic value. "
                "We cannot exercise — must sell to close to realize gains."
            ),
        }

        # Print position summary
        itm_label = "ITM" if itm else "OTM"
        pnl_sign = "+" if unrealized_pnl >= 0 else ""
        print(f"\n{'─' * 60}")
        print(f"  {osi}")
        print(f"  {info['type']} ${info['strike']:.2f}  exp {info['expiry']}  ({dte} DTE)  {itm_label}")
        print(f"  Contracts:  {int(qty)}")
        print(f"  Avg cost:   ${avg_cost * 100:.2f}/contract  (total ${total_cost:.2f})")
        if market["bid"] > 0:
            print(f"  Market:     bid ${market['bid']:.2f}  ask ${market['ask']:.2f}  mid ${current_mid:.4f}")
            print(f"  Value now:  ${current_value:.2f}  ({pnl_sign}{unrealized_pnl:.2f} / {pnl_sign}{pnl_pct:.1f}%)")
        else:
            print(f"  Market:     No current quote (market may be closed or illiquid)")
        if greeks:
            print(f"  Greeks:     δ={greeks.get('delta', 0):.3f}  θ={greeks.get('theta', 0):.4f}  IV={greeks.get('iv', 0):.1%}")

        # Ask Claude
        print(f"\n  Asking Claude for recommendation...")
        rec = ask_claude(context)

        rec_label = rec.get("recommendation", "?")
        urgency   = rec.get("urgency", "—")
        reasoning = rec.get("reasoning", "")
        key_risk  = rec.get("key_risk", "")
        exit_px   = rec.get("ideal_exit_price")

        print(f"\n  RECOMMENDATION: {rec_label}  (urgency: {urgency})")
        print(f"  Reasoning:  {reasoning}")
        print(f"  Key risk:   {key_risk}")
        if exit_px:
            print(f"  Ideal exit: ${exit_px:.2f}/contract")

        recommendations.append({
            "osi":      osi,
            "qty":      qty,
            "rec":      rec_label,
            "urgency":  urgency,
            "pnl":      unrealized_pnl,
            "pnl_pct":  pnl_pct,
            "context":  context,
            "claude":   rec,
        })

    # ── Summary & action prompt ───────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("SUMMARY")
    print(f"{'═' * 60}")
    close_candidates = []
    for r in recommendations:
        sign = "+" if r["pnl"] >= 0 else ""
        print(f"  {r['rec']:15s}  {r['osi']}  {sign}{r['pnl']:.2f} ({sign}{r['pnl_pct']:.1f}%)  [{r['urgency']}]")
        if r["rec"] in ("CLOSE", "PARTIAL_CLOSE"):
            close_candidates.append(r)

    if not close_candidates:
        print("\nNo close actions recommended. Done.")
        return

    print(f"\n{len(close_candidates)} position(s) flagged for closing.")
    print("\nEnter the number(s) to close now, 'all', or press Enter to skip:")
    for i, r in enumerate(close_candidates, 1):
        print(f"  [{i}] {r['osi']}  ({r['rec']})")

    try:
        choice = input("\nChoice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nSkipped.")
        return

    if not choice:
        print("No action taken.")
        return

    to_close = []
    if choice == "all":
        to_close = close_candidates
    else:
        for part in choice.replace(",", " ").split():
            try:
                idx = int(part) - 1
                if 0 <= idx < len(close_candidates):
                    to_close.append(close_candidates[idx])
            except ValueError:
                pass

    if not to_close:
        print("No valid selection. No action taken.")
        return

    print(f"\nClosing {len(to_close)} position(s)...")
    for r in to_close:
        print(f"\n  Closing {r['osi']} ×{int(r['qty'])} contracts...")
        close_position(client, r["osi"], r["qty"])

    print("\nDone.")


if __name__ == "__main__":
    main()
