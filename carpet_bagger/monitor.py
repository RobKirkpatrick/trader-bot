"""
Carpet Bagger monitor — runs every 5 min, 11am–midnight ET.

For each watchlist record:
  - watching → check if in-game and prob hit a buy tier → BUY
  - bought   → check take-profit / stop-loss / settlement → SELL

Also provides summary() for the 11:59pm ET nightly digest.
"""

import logging
import os
from datetime import datetime, timezone

import boto3

from carpet_bagger.kalshi_client import KalshiClient
from carpet_bagger.models import WatchlistRecord
from carpet_bagger.strategy import (
    STOP_LOSS, MAX_POSITIONS, MAX_POSITION_DOLLARS,
    get_tier_fraction, get_take_profit,
)

logger = logging.getLogger(__name__)

_TABLE  = "carpet-bagger-watchlist"
_REGION = "us-east-2"

# Monitor only fires trades during 11am–midnight ET.
# The EventBridge schedule runs every 5 min all day; this guard prevents
# unnecessary API calls during overnight hours.
_MONITOR_START_ET = 11   # 11am
_MONITOR_END_ET   = 24   # midnight (exclusive)


def _dynamodb():
    return boto3.client("dynamodb", region_name=_REGION)


def _publish_sns(message: str, subject: str) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.warning("SNS_TOPIC_ARN not set — skipping notification")
        return
    arn_parts = topic_arn.split(":")
    region    = arn_parts[3] if len(arn_parts) >= 4 else _REGION
    boto3.client("sns", region_name=region).publish(
        TopicArn=topic_arn, Subject=subject[:99], Message=message,
    )


def _et_hour() -> int:
    """Current hour in ET (approximation using UTC offset; DST-safe enough for guards)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).hour


def _load_active_records() -> list[WatchlistRecord]:
    """Load all watching or bought records from DynamoDB."""
    db = _dynamodb()
    resp = db.scan(
        TableName=_TABLE,
        FilterExpression="#s IN (:watching, :bought)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":watching": {"S": "watching"},
            ":bought":   {"S": "bought"},
        },
    )
    return [WatchlistRecord.from_dynamodb(item) for item in resp.get("Items", [])]


def _load_todays_closed() -> list[WatchlistRecord]:
    """Load records closed today (for nightly summary)."""
    db    = _dynamodb()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp  = db.scan(
        TableName=_TABLE,
        FilterExpression="#s = :closed AND begins_with(last_updated, :today)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":closed": {"S": "closed"},
            ":today":  {"S": today},
        },
    )
    return [WatchlistRecord.from_dynamodb(item) for item in resp.get("Items", [])]


def _update_record(record: WatchlistRecord) -> None:
    record.last_updated = datetime.now(timezone.utc).isoformat()
    _dynamodb().put_item(TableName=_TABLE, Item=record.to_dynamodb())


def _count_bought(records: list[WatchlistRecord]) -> int:
    return sum(1 for r in records if r.status == "bought")


def _process_watching(
    record: WatchlistRecord,
    client: KalshiClient,
    available_float: float,
    open_count: int,
) -> float:
    """
    Check if a watched market has reached a buy tier.
    Returns dollars spent (0 if no buy placed).
    """
    if open_count >= MAX_POSITIONS:
        logger.debug("Max positions reached — skipping %s", record.market_ticker)
        return 0.0

    # Skip if market already finalized
    try:
        market = client.get_market(record.market_ticker)
        market_status = (market.get("status") or "").lower()
        if market_status in ("finalized", "settled", "resolved"):
            logger.info("Market %s already %s — marking closed (missed)", record.market_ticker, market_status)
            record.status = "closed"
            record.pnl = 0.0
            _update_record(record)
            return 0.0
        yes_ask = market.get("yes_ask", 0) / 100.0

        # Only buy once the game has actually started
        open_time_str = market.get("open_time", "")
        if open_time_str:
            try:
                open_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                if open_dt > datetime.now(timezone.utc):
                    logger.debug("Game not yet started for %s (open_time=%s) — holding", record.market_ticker, open_time_str)
                    record.current_prob = yes_ask
                    _update_record(record)
                    return 0.0
            except ValueError:
                pass
    except Exception as exc:
        logger.warning("Could not fetch market for %s: %s", record.market_ticker, exc)
        return 0.0

    record.current_prob = yes_ask

    tier_fraction = get_tier_fraction(yes_ask)
    if tier_fraction == 0.0:
        _update_record(record)
        return 0.0   # not in a buy tier yet

    # In-game odds must exceed pre-game odds — team must be outperforming expectations
    if yes_ask < record.pre_game_prob:
        logger.debug(
            "In-game odds %.0f%% < pre-game %.0f%% for %s — waiting for outperformance",
            yes_ask * 100, record.pre_game_prob * 100, record.market_ticker,
        )
        _update_record(record)
        return 0.0

    tier_size = min(tier_fraction * available_float, MAX_POSITION_DOLLARS)
    if tier_size < yes_ask:
        logger.info("Insufficient float for %s (need $%.2f, have $%.2f)", record.market_ticker, tier_size, available_float)
        _update_record(record)
        return 0.0

    # Place buy
    try:
        result = client.place_buy(record.market_ticker, yes_ask, tier_size)
    except Exception as exc:
        logger.error("Buy order failed for %s: %s", record.market_ticker, exc)
        return 0.0

    # Use actual filled count from order response (not the requested count)
    order_data     = result.get("order", {}) if isinstance(result, dict) else {}
    contract_count = int(order_data.get("fill_count", 0)) or max(1, int(tier_size / yes_ask))
    actual_cost    = contract_count * yes_ask

    record.status         = "bought"
    record.entry_price    = yes_ask
    record.position_size  = actual_cost
    record.contract_count = contract_count
    record.trigger_time   = datetime.now(timezone.utc).isoformat()
    _update_record(record)

    logger.info("Bought %s: %d contracts @ %.2f ($%.2f)", record.market_ticker, contract_count, yes_ask, actual_cost)
    return actual_cost


def _process_bought(record: WatchlistRecord, client: KalshiClient) -> None:
    """Check take-profit, stop-loss, or settlement for a bought position."""
    # Check market status for settlement
    try:
        market = client.get_market(record.market_ticker)
    except Exception as exc:
        logger.warning("Could not fetch market %s: %s", record.market_ticker, exc)
        return

    market_status = (market.get("status") or "").lower()
    yes_ask_cents = market.get("yes_ask", 0)
    yes_ask       = yes_ask_cents / 100.0

    record.current_prob = yes_ask

    # Track peak probability for trailing stop
    if yes_ask > record.peak_prob:
        record.peak_prob = yes_ask

    # Market settled/finalized — record outcome
    if market_status in ("finalized", "settled", "resolved"):
        result = market.get("result", "")
        pnl = (1.0 - record.entry_price) * record.contract_count if result == "yes" else -record.entry_price * record.contract_count
        record.status = "closed"
        record.pnl    = round(pnl, 2)
        _update_record(record)
        outcome = "WON" if result == "yes" else "LOST"
        msg = (
            f"SETTLED {record.teams} — {outcome}\n"
            f"P&L: ${pnl:+.2f} | Result: {result} | {record.sport}"
        )
        _publish_sns(msg, f"[TraderBot] Carpet Bagger: Settled {record.teams} ({outcome})")
        return

    take_profit = get_take_profit(record.sport)

    # Take profit
    if yes_ask >= take_profit:
        _sell_position(record, client, yes_ask, reason="take_profit")
        return

    # Trailing stop: if up 40%+ from entry then retrace 15% from peak → sell
    if record.entry_price > 0 and record.peak_prob > 0:
        gain_from_entry = (record.peak_prob - record.entry_price) / record.entry_price
        if gain_from_entry >= 0.40 and yes_ask < record.peak_prob * 0.85:
            _sell_position(record, client, yes_ask, reason="trailing_stop")
            return

    # Stop loss: exit if odds drop below entry price (break-even protection)
    if record.entry_price > 0 and yes_ask < record.entry_price:
        _sell_position(record, client, yes_ask, reason="stop_loss")
        return

    # Still holding — update current prob
    _update_record(record)


def _sell_position(record: WatchlistRecord, client: KalshiClient, current_ask: float, reason: str) -> None:
    """Execute a market sell and close the record."""
    try:
        client.place_sell(record.market_ticker, record.contract_count, yes_bid_dollars=current_ask)
    except Exception as exc:
        logger.error("Sell failed for %s: %s", record.market_ticker, exc)
        return

    pnl = (current_ask - record.entry_price) * record.contract_count
    record.status = "closed"
    record.pnl    = round(pnl, 2)
    _update_record(record)

    label = {"take_profit": "SOLD", "trailing_stop": "TRAILING STOP"}.get(reason, "STOP LOSS")
    logger.info("%s %s: P&L=$%.2f exit_prob=%.2f", label, record.market_ticker, pnl, current_ask)


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------

def _reconcile_positions(client: KalshiClient) -> None:
    """
    Sync Kalshi open positions back to DynamoDB.

    If Kalshi has a position for a ticker but DynamoDB shows it as closed or
    missing (can happen when a buy fires on the same tick as a cutoff), restore
    the record to status=bought so the monitor keeps managing it.
    """
    kalshi_positions = {
        p["ticker"]: abs(int(p.get("position", 0)))
        for p in client.get_positions()
        if abs(int(p.get("position", 0))) > 0
    }
    if not kalshi_positions:
        return

    # Never reconcile excluded series (golf, racing) — we intentionally ignore those positions
    _EXCLUDED_PREFIXES = ("KXPGA", "KXLPGA", "KXNASCAR", "KXF1")

    db = _dynamodb()
    for ticker, contracts in kalshi_positions.items():
        if any(ticker.startswith(p) for p in _EXCLUDED_PREFIXES):
            logger.debug("Reconcile: skipping excluded series ticker %s", ticker)
            continue
        resp = db.get_item(TableName=_TABLE, Key={"market_ticker": {"S": ticker}})
        item = resp.get("Item")
        if not item:
            logger.warning("Reconcile: %s has %d contracts on Kalshi but no DDB record — skipping", ticker, contracts)
            continue
        record = WatchlistRecord.from_dynamodb(item)
        if record.status == "bought" and record.contract_count == contracts:
            continue  # already in sync
        if record.status in ("closed", "watching") or record.contract_count != contracts:
            logger.info(
                "Reconcile: fixing %s — DDB status=%s contracts=%d → bought contracts=%d",
                ticker, record.status, record.contract_count, contracts,
            )
            record.status         = "bought"
            record.contract_count = contracts
            # Preserve entry_price if already set; otherwise approximate from current ask
            if record.entry_price == 0.0:
                try:
                    mkt = client.get_market(ticker)
                    record.entry_price = mkt.get("yes_ask", 0) / 100.0
                except Exception:
                    pass
            _update_record(record)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def run(cfg: dict | None = None) -> dict:
    """Monitor entry point — called every 5 min by EventBridge."""
    logger.info("=== Carpet Bagger monitor tick ===")

    # Time gate: only trade during 11am–midnight ET
    et_hour = _et_hour()
    if not (_MONITOR_START_ET <= et_hour < _MONITOR_END_ET):
        logger.debug("Outside trading window (ET hour=%d) — monitor skipping", et_hour)
        return {"skipped": "outside_window"}

    api_key = os.environ.get("KALSHI_API_KEY", "")
    rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
    if not api_key or not rsa_key:
        logger.error("Kalshi credentials missing")
        return {"error": "missing credentials"}

    client = KalshiClient(api_key, rsa_key)

    # Exchange check
    if not client.is_trading_active():
        logger.info("Exchange not active — monitor skipping")
        return {"skipped": "exchange_inactive"}

    # Account state
    try:
        live_balance   = client.get_balance()
        total_deployed = client.get_total_deployed()
        available      = max(0.0, live_balance - total_deployed)
        logger.info("Balance: $%.2f | Deployed: $%.2f | Available: $%.2f",
                    live_balance, total_deployed, available)
    except Exception as exc:
        logger.error("Failed to fetch Kalshi balance: %s", exc)
        return {"error": str(exc)}

    # Reconcile: detect Kalshi positions that DynamoDB lost track of
    try:
        _reconcile_positions(client)
    except Exception as exc:
        logger.warning("Position reconciliation failed (non-fatal): %s", exc)

    # Process records
    records    = _load_active_records()
    open_count = _count_bought(records)
    spent      = 0.0
    buys       = 0
    sells      = 0

    for record in records:
        if record.status == "watching":
            cost = _process_watching(record, client, available - spent, open_count)
            if cost > 0:
                spent      += cost
                open_count += 1
                buys       += 1

        elif record.status == "bought":
            before_status = record.status
            _process_bought(record, client)
            if record.status == "closed":
                open_count = max(0, open_count - 1)
                sells += 1

    return {
        "window":        "carpet_bagger_monitor",
        "balance":       round(live_balance, 2),
        "available":     round(available - spent, 2),
        "buys":          buys,
        "sells":         sells,
        "open_positions":open_count,
    }


# ---------------------------------------------------------------------------
# Manual force-sell (invoked via {"window": "carpet_bagger_force_sell", "ticker": "..."})
# ---------------------------------------------------------------------------

def force_sell(cfg: dict | None = None) -> dict:
    """
    Immediately sell a specific position by ticker.
    Event: {"window": "carpet_bagger_force_sell", "ticker": "KXPGAH2H-..."}

    Optional overrides (for out-of-sync DynamoDB records):
      "contracts": int    — override contract count from DynamoDB
      "entry_price": float — override entry price for P&L calculation
    """
    cfg = cfg or {}
    ticker = cfg.get("ticker", "")
    if not ticker:
        logger.error("force_sell: no ticker provided")
        return {"error": "ticker required"}

    logger.info("=== Force sell: %s ===", ticker)

    api_key = os.environ.get("KALSHI_API_KEY", "")
    rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
    if not api_key or not rsa_key:
        return {"error": "missing credentials"}

    client = KalshiClient(api_key, rsa_key)

    # Find the record in DynamoDB (may be stale/closed if DDB lost sync with Kalshi)
    db   = _dynamodb()
    resp = db.get_item(TableName=_TABLE, Key={"market_ticker": {"S": ticker}})
    item = resp.get("Item")
    if not item:
        logger.error("force_sell: no DynamoDB record for %s", ticker)
        return {"error": f"no record for {ticker}"}

    record = WatchlistRecord.from_dynamodb(item)

    # Apply overrides — needed when DDB lost track of the position
    contracts_override = cfg.get("contracts")
    if contracts_override is not None:
        record.contract_count = int(contracts_override)
        record.status = "bought"  # treat as bought so we can sell
    if cfg.get("entry_price") is not None:
        record.entry_price = float(cfg["entry_price"])

    if record.status not in ("watching", "bought"):
        logger.info("force_sell: %s is %s with 0 contracts — nothing to sell", ticker, record.status)
        return {"skipped": f"status={record.status}"}

    # Get current market price
    try:
        market  = client.get_market(ticker)
        yes_bid = max(market.get("yes_bid", 1) / 100.0, 0.01)
    except Exception as exc:
        logger.error("force_sell: could not fetch market %s: %s", ticker, exc)
        return {"error": str(exc)}

    if record.contract_count <= 0:
        record.status = "closed"
        record.pnl    = 0.0
        _update_record(record)
        logger.info("force_sell: %s has 0 contracts — marked closed", ticker)
        return {"closed": ticker, "contracts": 0}

    _sell_position(record, client, yes_bid, reason="force_sell")
    logger.info("force_sell complete: %s | contracts=%d | price=%.2f", ticker, record.contract_count, yes_bid)
    return {"sold": ticker, "contracts": record.contract_count, "price": yes_bid}


# ---------------------------------------------------------------------------
# Nightly summary
# ---------------------------------------------------------------------------

def _week_thursday() -> datetime:
    """Return midnight ET on the most recent Thursday (or today if today is Thursday)."""
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    now = datetime.now(ZoneInfo("America/New_York"))
    days_since_thursday = (now.weekday() - 3) % 7
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_thursday)


def _settlements_pnl(client: KalshiClient, limit: int = 100) -> tuple[float, list[dict]]:
    """
    Compute net P&L from the Kalshi settlements API since the most recent Thursday.

    Returns (total_net, [{"ticker", "result", "net", "payout", "cost", "settled_time"}])
    Includes ALL positions — bot and manual.
    """
    from zoneinfo import ZoneInfo
    cutoff = _week_thursday()

    try:
        raw = client._request("GET", f"/portfolio/settlements?limit={limit}")
        settlements = raw.get("settlements", [])
    except Exception as exc:
        logger.warning("Could not fetch settlements: %s", exc)
        return 0.0, []

    results = []
    total   = 0.0

    for s in settlements:
        settled_dt = datetime.fromisoformat(
            s["settled_time"].replace("Z", "+00:00")
        ).astimezone(ZoneInfo("America/New_York"))
        if settled_dt < cutoff:
            continue

        value      = s["value"]           # 100 if YES wins, 0 if NO wins
        yes_payout = s["yes_count"] * (value / 100.0)
        no_payout  = s["no_count"]  * ((100 - value) / 100.0)
        cost       = (s["yes_total_cost"] + s["no_total_cost"]) / 100.0
        fee        = float(s.get("fee_cost", 0))
        net        = yes_payout + no_payout - cost - fee

        total += net
        results.append({
            "ticker": s["ticker"],
            "result": s["market_result"],
            "net":    round(net, 2),
            "payout": round(yes_payout + no_payout, 2),
            "cost":   round(cost, 2),
        })

    return round(total, 2), sorted(results, key=lambda x: x["net"], reverse=True)


def summary(cfg: dict | None = None) -> dict:
    """11:59pm ET nightly digest."""
    logger.info("=== Carpet Bagger nightly summary ===")

    api_key = os.environ.get("KALSHI_API_KEY", "")
    rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")

    live_balance = 0.0
    total_pnl    = 0.0
    settled      = []

    if api_key and rsa_key:
        try:
            client       = KalshiClient(api_key, rsa_key)
            live_balance = client.get_balance()
            total_pnl, settled = _settlements_pnl(client)
        except Exception as exc:
            logger.warning("Could not fetch Kalshi data: %s", exc)

    records    = _load_active_records()
    still_open = [r for r in records if r.status == "bought"]
    wins       = sum(1 for s in settled if s["net"] > 0)
    losses     = sum(1 for s in settled if s["net"] < 0)

    from zoneinfo import ZoneInfo
    ET      = ZoneInfo("America/New_York")
    now_str = datetime.now(ET).strftime("%a %b %d, %Y  %I:%M %p ET")
    thu_str = _week_thursday().strftime("%b %d")

    lines = [
        f"Carpet Bagger Weekly Summary — {now_str}",
        "─" * 48,
        f"Live Balance:    ${live_balance:.2f}",
        f"Week P&L ({thu_str}+): ${total_pnl:+.2f}   ({wins}W / {losses}L)",
        "─" * 48,
    ]

    if settled:
        lines.append(f"SETTLED SINCE {thu_str.upper()}:")
        for s in sorted(settled, key=lambda x: x["net"], reverse=True):
            icon = "✓" if s["net"] > 0 else ("✗" if s["net"] < 0 else "~")
            lines.append(
                f"  {icon} ${s['net']:>+7.2f}  payout ${s['payout']:.2f} / cost ${s['cost']:.2f}  [{s['result'].upper()}]  {s['ticker']}"
            )
        lines.append("")

    if still_open:
        lines.append("STILL OPEN:")
        for r in still_open:
            unrealized = (r.current_prob - r.entry_price) * r.contract_count
            lines.append(
                f"  ? {unrealized:>+8.2f} unrlzd  {r.contract_count}c @ {r.entry_price:.2f}  {r.teams}"
            )
        lines.append("")

    lines += [
        "─" * 48,
        f"Float available: ${live_balance:.2f}",
    ]

    msg     = "\n".join(lines)
    subject = f"[TraderBot] Carpet Bagger — Balance ${live_balance:.2f} | Week {total_pnl:+.2f} | {wins}W {losses}L"

    try:
        _publish_sns(msg, subject)
    except Exception as exc:
        logger.warning("SNS failed for nightly summary: %s", exc)

    return {
        "window":  "carpet_bagger_summary",
        "pnl":     total_pnl,
        "wins":    wins,
        "losses":  losses,
        "balance": round(live_balance, 2),
    }
