"""
CFTC Commitments of Traders (COT) signal.

Reads the "Traders in Financial Futures" report released every Friday ~3:30 PM ET.
Covers net positioning through the prior Tuesday. No API key required.

Used by scheduler/suggestions.py to give Claude institutional positioning context
for Friday evening → Monday trade suggestions.
"""

import logging

import requests

logger = logging.getLogger(__name__)

_COT_API = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_TIMEOUT  = 15

# (search_name, equity_signal_direction, display_label)
# equity_signal_direction: +1 = net lev-money longs is bullish for equities,
#                          -1 = net lev-money longs is bearish for equities
_TRACKED = [
    ("S&P 500 Consolidated",    +1.0, "S&P 500"),
    ("NASDAQ-100 Consolidated", +1.0, "Nasdaq-100"),
    ("RUSSELL E-MINI",          +1.0, "Russell 2000"),
    ("VIX FUTURES",             -1.0, "VIX"),
    ("UST 10Y NOTE",            -0.4, "10Y Treasury"),
]


def fetch_cot_signal() -> dict:
    """
    Fetch the latest CFTC COT data.

    Returns:
        {
          "summary":      str   — plain-text block for Claude's prompt
          "equity_tilt":  float — aggregate signal, -1.0 (bearish) to +1.0 (bullish)
          "report_date":  str   — date of the latest report (YYYY-MM-DD)
          "contracts":    dict  — per-contract detail
        }
    Returns {} on failure so the caller can skip gracefully.
    """
    try:
        return _fetch_and_parse()
    except Exception as exc:
        logger.warning("CFTC COT fetch failed: %s", exc)
        return {}


def _fetch_and_parse() -> dict:
    contracts_data: dict[str, dict] = {}
    report_date: str = ""

    for search_name, direction, label in _TRACKED:
        try:
            resp = requests.get(
                _COT_API,
                params={
                    "$where": f"contract_market_name='{search_name}'",
                    "$order": "report_date_as_yyyy_mm_dd DESC",
                    "$limit": "1",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:
            logger.debug("CFTC: request failed for %s: %s", search_name, exc)
            continue

        if not rows:
            logger.debug("CFTC: no data for %s", search_name)
            continue

        row = rows[0]
        if not report_date:
            report_date = (row.get("report_date_as_yyyy_mm_dd") or "")[:10]

        try:
            lev_long  = int(row.get("lev_money_positions_long",  0) or 0)
            lev_short = int(row.get("lev_money_positions_short", 0) or 0)
            am_long   = int(row.get("asset_mgr_positions_long",  0) or 0)
            am_short  = int(row.get("asset_mgr_positions_short", 0) or 0)
            lev_long_chg  = int(row.get("change_in_lev_money_long",  0) or 0)
            lev_short_chg = int(row.get("change_in_lev_money_short", 0) or 0)
        except (ValueError, TypeError):
            continue

        contracts_data[search_name] = {
            "label":       label,
            "direction":   direction,
            "lev_long":    lev_long,
            "lev_short":   lev_short,
            "net_lev":     lev_long - lev_short,
            "net_am":      am_long  - am_short,
            "wow_net_chg": lev_long_chg - lev_short_chg,
        }

    if not contracts_data:
        return {}

    equity_tilt = _compute_equity_tilt(contracts_data)
    summary     = _build_summary(contracts_data, equity_tilt, report_date)

    return {
        "summary":      summary,
        "equity_tilt":  equity_tilt,
        "report_date":  report_date,
        "contracts":    contracts_data,
    }


def _compute_equity_tilt(contracts: dict) -> float:
    """Aggregate net leveraged-money positioning into a single equity tilt score."""
    signals: list[float] = []
    for c in contracts.values():
        total = c["lev_long"] + c["lev_short"]
        if total == 0:
            continue
        net_ratio = c["net_lev"] / total  # range -1 to +1
        signals.append(net_ratio * c["direction"])
    return round(sum(signals) / len(signals), 3) if signals else 0.0


def _build_summary(contracts: dict, equity_tilt: float, report_date: str) -> str:
    if equity_tilt > 0.1:
        tilt_label = "bullish"
    elif equity_tilt < -0.1:
        tilt_label = "bearish"
    else:
        tilt_label = "neutral"

    lines = [
        f"CFTC Commitments of Traders — week ending {report_date}",
        f"Aggregate institutional equity tilt: {equity_tilt:+.2f} ({tilt_label})",
        "",
        "Leveraged money (hedge fund) net positioning + week-over-week change:",
    ]

    for c in contracts.values():
        wow   = c["wow_net_chg"]
        trend = "increasing" if wow > 500 else "decreasing" if wow < -500 else "flat"
        sign  = "+" if c["net_lev"] >= 0 else ""
        lines.append(
            f"  {c['label']:<22}: net {sign}{c['net_lev']:,}  WoW {'+' if wow >= 0 else ''}{wow:,} ({trend})"
        )

    lines += [
        "",
        "Interpretation: large WoW shifts or extreme crowding (>80% one-sided) are most",
        "actionable. COT data lags ~4 days — use as structural bias, not a day-trade signal.",
    ]

    return "\n".join(lines)
