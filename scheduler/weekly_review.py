"""
Weekly performance review — runs Sunday at 6:00 PM ET.

Compares the bot's actual portfolio returns against a high-yield savings
account benchmark (5.6% APY by default), both for the past week and
cumulatively since tracking began.

Snapshot storage:
  The baseline and previous-week values are stored as extra keys inside
  the existing Secrets Manager secret (trading-bot/secrets):
    WEEKLY_BASELINE_DATE  — ISO date when tracking started (set on first run)
    WEEKLY_BASELINE_VALUE — portfolio value on that date
    WEEKLY_PREV_DATE      — ISO date of the last weekly snapshot
    WEEKLY_PREV_VALUE     — portfolio value at the last weekly snapshot
"""

import json
import logging
from datetime import datetime, timezone, timedelta

import boto3

from config.settings import settings

_CB_TABLE  = "carpet-bagger-watchlist"
_CB_REGION = "us-east-2"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Portfolio value
# ---------------------------------------------------------------------------

def _get_portfolio_value() -> tuple[float, float, list[dict]]:
    """
    Return (total_value, buying_power, positions).
    total_value = cash + sum of position market values.
    """
    from broker.public_client import PublicClient
    client = PublicClient()
    portfolio = client.get_portfolio()
    bp = portfolio.get("buyingPower", {})
    buying_power = float(bp.get("cashOnlyBuyingPower") or bp.get("buyingPower") or 0)
    positions = portfolio.get("positions", [])
    position_value = sum(float(p.get("currentValue") or 0) for p in positions)
    return buying_power + position_value, buying_power, positions


# ---------------------------------------------------------------------------
# Snapshot helpers (stored in Secrets Manager alongside other secrets)
# ---------------------------------------------------------------------------

def _load_secrets_dict() -> dict:
    secret_name = settings.AWS_SECRET_NAME
    region = settings.AWS_REGION
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_name)
    return json.loads(resp.get("SecretString") or "{}")


def _save_secrets_dict(secrets: dict) -> None:
    secret_name = settings.AWS_SECRET_NAME
    region = settings.AWS_REGION
    client = boto3.client("secretsmanager", region_name=region)
    client.put_secret_value(SecretId=secret_name, SecretString=json.dumps(secrets))


# ---------------------------------------------------------------------------
# Carpet Bagger weekly stats
# ---------------------------------------------------------------------------

def _get_carpet_bagger_weekly_stats() -> dict:
    """
    Scan carpet-bagger-watchlist for activity in the past 7 days.
    Returns a dict with scouted, bought, wins, losses, still_open, week_pnl.
    """
    try:
        db = boto3.client("dynamodb", region_name=_CB_REGION)
        resp = db.scan(TableName=_CB_TABLE)
        items = resp.get("Items", [])
    except Exception as exc:
        logger.warning("Could not load Carpet Bagger DynamoDB data: %s", exc)
        return {}

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    scouted    = 0
    bought     = 0
    wins       = 0
    losses     = 0
    still_open = 0
    week_pnl   = 0.0

    for item in items:
        last_updated  = item.get("last_updated",  {}).get("S", "")
        trigger_time  = item.get("trigger_time",  {}).get("S", "")
        status        = item.get("status",        {}).get("S", "")
        pnl           = float(item.get("pnl",     {}).get("N", "0"))

        # Count as scouted if added this week
        if last_updated >= week_ago and status == "watching":
            scouted += 1
        # Closed trades from this week
        if status == "closed" and trigger_time >= week_ago:
            bought += 1
            week_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1
        # Still holding
        if status == "bought":
            still_open += 1

    return {
        "scouted":    scouted,
        "bought":     bought,
        "wins":       wins,
        "losses":     losses,
        "still_open": still_open,
        "week_pnl":   round(week_pnl, 2),
    }


# ---------------------------------------------------------------------------
# HYSA benchmark
# ---------------------------------------------------------------------------

def _hysa_growth(principal: float, days: int, apy: float = None) -> float:
    """
    Compound growth of `principal` over `days` at `apy` (annual, decimal).
    Returns the dollar gain (not total value).
    """
    apy = apy if apy is not None else settings.HYSA_APY
    return principal * ((1 + apy) ** (days / 365) - 1)


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def _build_weekly_email(
    now_date: str,
    total_value: float,
    buying_power: float,
    positions: list[dict],
    prev_date: str,
    prev_value: float,
    baseline_date: str,
    baseline_value: float,
    cb_stats: dict | None = None,
) -> str:
    from datetime import date as date_type

    def parse_date(s: str) -> date_type:
        return datetime.strptime(s, "%Y-%m-%d").date()

    today = parse_date(now_date)
    prev  = parse_date(prev_date)
    base  = parse_date(baseline_date)

    days_this_week  = max((today - prev).days, 1)
    days_cumulative = max((today - base).days, 1)

    # Actual returns
    week_gain = total_value - prev_value
    week_pct  = week_gain / prev_value if prev_value > 0 else 0.0
    cum_gain  = total_value - baseline_value
    cum_pct   = cum_gain / baseline_value if baseline_value > 0 else 0.0

    # HYSA benchmark
    hysa_week_gain = _hysa_growth(prev_value, days_this_week)
    hysa_cum_gain  = _hysa_growth(baseline_value, days_cumulative)
    hysa_week_pct  = hysa_week_gain / prev_value if prev_value > 0 else 0.0
    hysa_cum_pct   = hysa_cum_gain / baseline_value if baseline_value > 0 else 0.0

    # Alpha
    week_alpha = week_gain - hysa_week_gain
    cum_alpha  = cum_gain  - hysa_cum_gain

    def fmt_gain(v: float) -> str:
        sign = "+" if v >= 0 else ""
        return f"{sign}${v:,.2f}"

    def fmt_pct(v: float) -> str:
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2%}"

    lines = [
        "TraderBot — Weekly Performance Review",
        datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC"),
        "─" * 46,
        f"Portfolio value:  ${total_value:>10,.2f}",
        f"Cash available:   ${buying_power:>10,.2f}",
        f"Invested:         ${total_value - buying_power:>10,.2f}",
        "",
        "━" * 46,
        f"THIS WEEK  ({prev_date} → {now_date}, {days_this_week}d)",
        "━" * 46,
        f"  Bot return:       {fmt_gain(week_gain):>10}  ({fmt_pct(week_pct)})",
        f"  HYSA @ {settings.HYSA_APY:.1%} APY:  {fmt_gain(hysa_week_gain):>10}  ({fmt_pct(hysa_week_pct)})",
        f"  Alpha:            {fmt_gain(week_alpha):>10}",
        "",
        "━" * 46,
        f"SINCE TRACKING STARTED  ({baseline_date}, {days_cumulative}d)",
        "━" * 46,
        f"  Bot return:       {fmt_gain(cum_gain):>10}  ({fmt_pct(cum_pct)})",
        f"  HYSA @ {settings.HYSA_APY:.1%} APY:  {fmt_gain(hysa_cum_gain):>10}  ({fmt_pct(hysa_cum_pct)})",
        f"  Alpha:            {fmt_gain(cum_alpha):>10}",
        f"  Starting value:   ${baseline_value:>10,.2f}",
        "",
        "━" * 46,
        "OPEN POSITIONS",
        "━" * 46,
    ]

    if positions:
        for p in positions:
            sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
            cur_val = float(p.get("currentValue") or 0)
            cost_basis = p.get("costBasis")
            if isinstance(cost_basis, dict):
                total_cost = float(cost_basis.get("totalCost") or 0)
                gain_val   = float(cost_basis.get("gainValue") or 0)
                gain_pct   = float(cost_basis.get("gainPercentage") or 0) / 100
            else:
                total_cost = 0.0
                gain_val   = 0.0
                gain_pct   = 0.0

            pnl_str  = f"{'+' if gain_val >= 0 else ''}${gain_val:.2f} ({'+' if gain_pct >= 0 else ''}{gain_pct:.1%})"
            lines.append(f"  {sym:<6} cost=${total_cost:.2f}  value=${cur_val:.2f}  P&L={pnl_str}")
    else:
        lines.append("  No open positions.")

    # Carpet Bagger section
    lines += ["", "━" * 46, "CARPET BAGGER  (Kalshi sports — this week)", "━" * 46]
    if cb_stats:
        pnl_str = f"{'+' if cb_stats['week_pnl'] >= 0 else ''}${cb_stats['week_pnl']:.2f}"
        record  = f"{cb_stats['wins']}W {cb_stats['losses']}L"
        lines += [
            f"  Scouted:     {cb_stats['scouted']} games",
            f"  Traded:      {cb_stats['bought']} ({record})",
            f"  Still open:  {cb_stats['still_open']}",
            f"  Week P&L:    {pnl_str}",
        ]
    else:
        lines.append("  No data yet (first week).")

    lines += [
        "",
        "─" * 46,
        f"Stock benchmark: High-Yield Savings @ {settings.HYSA_APY:.1%} APY",
        "Positive alpha = bot outperformed HYSA this week.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_weekly_review() -> dict:
    """
    Main entry point for the weekly performance review.
    Called by lambda_function.handler() when window == "weekly_review".
    """
    from scheduler.jobs import _publish_sns  # avoid circular import

    logger.info("=== Weekly performance review starting ===")

    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Current portfolio value
    try:
        total_value, buying_power, positions = _get_portfolio_value()
        logger.info("Portfolio value: $%.2f (cash $%.2f + positions $%.2f)",
                    total_value, buying_power, total_value - buying_power)
    except Exception as exc:
        logger.error("Failed to fetch portfolio: %s", exc)
        return {"window": "weekly_review", "error": str(exc)}

    # 2. Load snapshot from Secrets Manager
    try:
        secrets = _load_secrets_dict()
    except Exception as exc:
        logger.error("Failed to load secrets for snapshot: %s", exc)
        secrets = {}

    baseline_date  = secrets.get("WEEKLY_BASELINE_DATE", now_date)
    baseline_value = float(secrets.get("WEEKLY_BASELINE_VALUE", total_value))
    prev_date      = secrets.get("WEEKLY_PREV_DATE", now_date)
    prev_value     = float(secrets.get("WEEKLY_PREV_VALUE", total_value))

    # First-ever run: initialize baseline
    if "WEEKLY_BASELINE_DATE" not in secrets:
        logger.info("First weekly review run — setting baseline: $%.2f on %s", total_value, now_date)
        baseline_date  = now_date
        baseline_value = total_value
        prev_date      = now_date
        prev_value     = total_value

    # 3. Carpet Bagger stats
    cb_stats = _get_carpet_bagger_weekly_stats()
    logger.info("Carpet Bagger weekly stats: %s", cb_stats)

    # 4. Build email
    message = _build_weekly_email(
        now_date=now_date,
        total_value=total_value,
        buying_power=buying_power,
        positions=positions,
        prev_date=prev_date,
        prev_value=prev_value,
        baseline_date=baseline_date,
        baseline_value=baseline_value,
        cb_stats=cb_stats or None,
    )

    week_gain = total_value - prev_value
    cum_gain  = total_value - baseline_value
    cb_pnl_str = f" | CB P&L ${cb_stats['week_pnl']:+.2f}" if cb_stats else ""
    subject = (
        f"[TraderBot] Weekly review — "
        f"{'▲' if week_gain >= 0 else '▼'} ${abs(week_gain):.2f} this week | "
        f"${cum_gain:+.2f} total vs HYSA{cb_pnl_str}"
    )
    logger.info(message)

    # 5. Send email
    try:
        _publish_sns(message, subject=subject)
    except Exception as exc:
        logger.error("SNS publish failed for weekly review: %s", exc)

    # 6. Update snapshot in Secrets Manager (for next week's delta)
    try:
        secrets.update({
            "WEEKLY_BASELINE_DATE":  baseline_date,
            "WEEKLY_BASELINE_VALUE": str(baseline_value),
            "WEEKLY_PREV_DATE":      now_date,
            "WEEKLY_PREV_VALUE":     str(total_value),
        })
        _save_secrets_dict(secrets)
        logger.info("Weekly snapshot updated: $%.2f on %s", total_value, now_date)
    except Exception as exc:
        logger.warning("Failed to save weekly snapshot: %s", exc)

    return {
        "window":          "weekly_review",
        "portfolio_value": total_value,
        "week_gain":       round(week_gain, 2),
        "cum_gain":        round(cum_gain, 2),
    }
