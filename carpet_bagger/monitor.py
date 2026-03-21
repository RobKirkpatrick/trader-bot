"""
Carpet Bagger monitor — runs every 5 min, 11am–midnight ET.

For each watchlist record:
  - watching → check if in-game and prob hit a buy tier → BUY
  - bought   → check take-profit / stop-loss / settlement → SELL

Also provides summary() for the 11:59pm ET nightly digest.
"""

import logging
import os
import re as _re
from datetime import datetime, timezone, date as _date

import boto3

from carpet_bagger.kalshi_client import KalshiClient, parse_market_price
from carpet_bagger.models import WatchlistRecord
from carpet_bagger.strategy import (
    STOP_LOSS, TAKE_PROFIT, MAX_POSITIONS, MAX_POSITION_PCT, MAX_POSITION_DOLLARS,
    PRE_GAME_MIN, PRE_GAME_MAX,
    get_take_profit,
)

logger = logging.getLogger(__name__)

_MONTH_MAP = {m: i for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"], 1
)}

def _game_date_from_ticker(ticker: str) -> _date | None:
    """
    Extract the game date embedded in a Kalshi ticker.

    Kalshi encodes date as YYMONDD, e.g.:
      KXNBAGAME-26MAR12PHXIND-PHX    → 2026-03-12
      KXNCAABBGAME-26MAR101900TROY-… → 2026-03-10

    Returns None if no date can be parsed.
    """
    m = _re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})', ticker.upper())
    if not m:
        return None
    try:
        year  = 2000 + int(m.group(1))
        month = _MONTH_MAP[m.group(2)]
        day   = int(m.group(3))
        return _date(year, month, day)
    except (ValueError, KeyError):
        return None

_TABLE  = "carpet-bagger-watchlist"
_REGION = "us-east-2"

# Monitor only fires trades during 11am–midnight ET.
# The EventBridge schedule runs every 5 min all day; this guard prevents
# unnecessary API calls during overnight hours.
_MONITOR_START_ET = 10   # 10am — catches early afternoon games (conf tournaments, March Madness noon tip-offs)
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
    max_position_dollars: float = MAX_POSITION_DOLLARS,
) -> float:
    """
    Buy strategy: as soon as today's game is at 55–75%, deploy up to $5.
    Immediately place a resting sell limit at $0.97 after the buy fills.
    Returns dollars spent (0 if no buy placed).
    """
    if open_count >= MAX_POSITIONS:
        logger.debug("Max positions reached — skipping %s", record.market_ticker)
        return 0.0

    try:
        market = client.get_market(record.market_ticker)
        market_status = (market.get("status") or "").lower()
        if market_status in ("finalized", "settled", "resolved"):
            logger.info("Market %s already %s — marking closed (missed)", record.market_ticker, market_status)
            record.status = "closed"
            record.pnl = 0.0
            _update_record(record)
            return 0.0

        yes_ask = parse_market_price(market, "yes_ask")

        # Must be today's game
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        game_date = _game_date_from_ticker(record.market_ticker)
        if game_date is not None and game_date > today_et:
            logger.debug("Game %s is on %s — not today, holding", record.market_ticker, game_date)
            record.current_prob = yes_ask
            _update_record(record)
            return 0.0

        # Game must have already started — no pre-game buys, absorb the 2¢ in-game fee
        open_time_str = market.get("open_time", "")
        if open_time_str:
            try:
                open_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                if open_dt > datetime.now(timezone.utc):
                    logger.debug("Game %s hasn't started yet — skipping pre-game buy", record.market_ticker)
                    record.current_prob = yes_ask
                    _update_record(record)
                    return 0.0
            except ValueError:
                pass

        # Price must still be in the buy window (55–75%)
        if not (PRE_GAME_MIN <= yes_ask <= PRE_GAME_MAX):
            logger.debug("Game %s at %.0f%% — outside buy window [%.0f%%–%.0f%%]",
                         record.market_ticker, yes_ask * 100, PRE_GAME_MIN * 100, PRE_GAME_MAX * 100)
            record.current_prob = yes_ask
            _update_record(record)
            return 0.0

    except Exception as exc:
        logger.warning("Could not fetch market for %s: %s", record.market_ticker, exc)
        return 0.0

    # Cap spend at $5 or available float, whichever is smaller
    budget = min(available_float, max_position_dollars)
    if budget < yes_ask:
        logger.info("Insufficient float for %s (need $%.2f, have $%.2f)", record.market_ticker, yes_ask, budget)
        record.current_prob = yes_ask
        _update_record(record)
        return 0.0

    # Place buy
    try:
        result = client.place_buy(record.market_ticker, yes_ask, budget)
    except Exception as exc:
        logger.error("Buy order failed for %s: %s", record.market_ticker, exc)
        return 0.0

    order_data     = result.get("order", {}) if isinstance(result, dict) else {}
    contract_count = int(order_data.get("fill_count", 0)) or max(1, int(budget / yes_ask))
    actual_cost    = contract_count * yes_ask

    # Immediately place resting sell limit at $0.97
    take_profit = get_take_profit(record.sport)
    sell_order_id = ""
    try:
        sell_result   = client.place_sell(record.market_ticker, contract_count, yes_bid_dollars=take_profit)
        sell_order_id = (sell_result.get("order", {}) if isinstance(sell_result, dict) else {}).get("order_id", "")
        logger.info("Resting sell placed for %s: %d contracts @ $%.2f [order=%s]",
                    record.market_ticker, contract_count, take_profit, sell_order_id)
    except Exception as exc:
        logger.warning("Resting sell failed for %s: %s — monitor will retry", record.market_ticker, exc)

    record.status         = "bought"
    record.entry_price    = yes_ask
    record.position_size  = actual_cost
    record.contract_count = contract_count
    record.trigger_time   = datetime.now(timezone.utc).isoformat()
    record.sell_order_id  = sell_order_id
    record.current_prob   = yes_ask
    _update_record(record)

    logger.info("Bought %s: %d contracts @ $%.2f ($%.2f total) | resting sell @ $%.2f",
                record.market_ticker, contract_count, yes_ask, actual_cost, TAKE_PROFIT)
    return actual_cost


def _process_bought(record: WatchlistRecord, client: KalshiClient) -> None:
    """
    Manage a bought position.

    The resting $0.97 sell limit handles profit-taking automatically.
    This function only needs to:
      1. Detect settlement (game ended) and record P&L.
      2. Ensure a resting sell is in place (place one if missing after a failed attempt).
      3. Trigger stop-loss if price drops below $0.45 (market flipped — get out).
    """
    try:
        market = client.get_market(record.market_ticker)
    except Exception as exc:
        logger.warning("Could not fetch market %s: %s", record.market_ticker, exc)
        return

    market_status = (market.get("status") or "").lower()
    yes_ask = parse_market_price(market, "yes_ask")
    record.current_prob = yes_ask

    # Market settled — the resting sell either filled or auto-cancelled; record outcome
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

    # Ensure resting sell is in place — retry if the initial placement failed
    if not record.sell_order_id and record.contract_count > 0:
        try:
            sell_result   = client.place_sell(record.market_ticker, record.contract_count, yes_bid_dollars=get_take_profit(record.sport))
            sell_order_id = (sell_result.get("order", {}) if isinstance(sell_result, dict) else {}).get("order_id", "")
            record.sell_order_id = sell_order_id
            logger.info("Placed missing resting sell for %s [order=%s]", record.market_ticker, sell_order_id)
        except Exception as exc:
            logger.warning("Resting sell retry failed for %s: %s", record.market_ticker, exc)

    # Stop-loss: price fell below $0.45 — market has flipped, exit immediately
    if yes_ask < STOP_LOSS:
        _sell_position(record, client, yes_ask, reason="stop_loss")
        return

    # Holding — resting sell will auto-fill at $0.97 when the market gets there
    _update_record(record)


def _sell_position(record: WatchlistRecord, client: KalshiClient, current_ask: float, reason: str) -> None:
    """Cancel the resting sell order (if any), then market-sell and close the record."""
    # Cancel resting sell before placing a stop-loss sell to avoid double-sell
    if record.sell_order_id:
        try:
            client.cancel_order(record.sell_order_id)
            logger.info("Cancelled resting sell %s before stop-loss exit", record.sell_order_id)
        except Exception as exc:
            logger.warning("Could not cancel sell order %s: %s — proceeding with stop-loss anyway", record.sell_order_id, exc)

    try:
        client.place_sell(record.market_ticker, record.contract_count, yes_bid_dollars=current_ask)
    except Exception as exc:
        logger.error("Sell failed for %s: %s", record.market_ticker, exc)
        return

    pnl = (current_ask - record.entry_price) * record.contract_count
    record.status = "closed"
    record.pnl    = round(pnl, 2)
    _update_record(record)

    logger.info("STOP LOSS %s: P&L=$%.2f exit_prob=%.2f", record.market_ticker, pnl, current_ask)


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

        # Never restore a closed position — this causes a sell loop (take_profit fires,
        # marks closed, reconcile re-opens, take_profit fires again every 5 min).
        if record.status == "closed":
            logger.debug("Reconcile: %s is closed in DDB but Kalshi still shows %d contracts — ignoring (sell order may be pending)", ticker, contracts)
            continue

        # Trust DynamoDB contract_count for active bought positions — the Kalshi
        # position API can return unexpected values that corrupt our cost basis.
        if record.status == "bought":
            if record.contract_count == contracts:
                continue  # in sync
            else:
                logger.warning(
                    "Reconcile: %s DDB has %d contracts, Kalshi reports %d — trusting DynamoDB count",
                    ticker, record.contract_count, contracts,
                )
                continue

        # Only reconcile watching positions where a buy went through but DDB write failed
        if record.status == "watching":
            logger.info(
                "Reconcile: %s has %d contracts on Kalshi but DDB shows watching — restoring to bought",
                ticker, contracts,
            )
            record.status         = "bought"
            record.contract_count = contracts
            if record.entry_price == 0.0:
                try:
                    mkt = client.get_market(ticker)
                    record.entry_price = mkt.get("yes_ask", 0) / 100.0
                except Exception:
                    pass
            _update_record(record)


# ---------------------------------------------------------------------------
# Live in-game scanner
# ---------------------------------------------------------------------------

def _scan_live_games(
    client: KalshiClient,
    available_float: float,
    open_count: int,
    existing_tickers: set[str],
) -> tuple[float, int]:
    """
    Scan all active sport series for games currently in progress at 55–75%.
    Buys immediately and places a resting sell — no watchlist pre-population needed.

    This runs every monitor tick and catches games the 8am scout missed,
    mid-game momentum shifts, and any sport added after the scout ran.

    Returns (dollars_spent, contracts_bought).
    """
    from carpet_bagger.strategy import SPORT_SERIES, MAX_POSITION_DOLLARS

    spent    = 0.0
    bought   = 0
    now_utc  = datetime.now(timezone.utc)

    for series in SPORT_SERIES:
        if open_count >= MAX_POSITIONS:
            break
        try:
            markets = client.get_series_markets(series)
        except Exception as exc:
            logger.warning("Live scan: failed to fetch %s: %s", series, exc)
            continue

        for market in markets:
            if open_count >= MAX_POSITIONS:
                break

            ticker = market.get("ticker", "")
            if not ticker or ticker in existing_tickers:
                continue

            # Game must be in progress — open_time in the past
            open_time_str = market.get("open_time", "")
            if open_time_str:
                try:
                    open_dt = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                    if open_dt > now_utc:
                        continue  # not started yet
                except ValueError:
                    pass
            else:
                continue  # no open_time — can't confirm in progress

            # Probability filter: 55–75%
            yes_ask = parse_market_price(market, "yes_ask")
            if not (PRE_GAME_MIN <= yes_ask <= PRE_GAME_MAX):
                continue

            # Budget
            budget = min(available_float - spent, MAX_POSITION_DOLLARS)
            if budget < yes_ask:
                logger.debug("Live scan: insufficient float for %s ($%.2f needed, $%.2f available)",
                             ticker, yes_ask, budget)
                continue

            # Buy
            try:
                result = client.place_buy(ticker, yes_ask, budget)
            except Exception as exc:
                logger.error("Live scan buy failed for %s: %s", ticker, exc)
                continue

            order_data     = result.get("order", {}) if isinstance(result, dict) else {}
            contract_count = int(order_data.get("fill_count", 0)) or max(1, int(budget / yes_ask))
            actual_cost    = contract_count * yes_ask

            # Resting sell at sport-specific take-profit
            take_profit   = get_take_profit(series)
            sell_order_id = ""
            try:
                sell_result   = client.place_sell(ticker, contract_count, yes_bid_dollars=take_profit)
                sell_order_id = (sell_result.get("order", {}) if isinstance(sell_result, dict) else {}).get("order_id", "")
            except Exception as exc:
                logger.warning("Live scan resting sell failed for %s: %s", ticker, exc)

            teams      = market.get("subtitle") or market.get("title") or ticker
            close_time = market.get("close_time", "")

            record = WatchlistRecord(
                market_ticker  = ticker,
                sport          = series,
                teams          = teams,
                game_time      = close_time,
                pre_game_prob  = yes_ask,
                current_prob   = yes_ask,
                status         = "bought",
                position_size  = actual_cost,
                contract_count = contract_count,
                entry_price    = yes_ask,
                trigger_time   = now_utc.isoformat(),
                sell_order_id  = sell_order_id,
                last_updated   = now_utc.isoformat(),
            )
            try:
                _update_record(record)
                existing_tickers.add(ticker)
                spent      += actual_cost
                bought     += 1
                open_count += 1
                logger.info(
                    "Live buy: %s | %d contracts @ $%.2f ($%.2f) | resting sell @ $%.2f [%s]",
                    ticker, contract_count, yes_ask, actual_cost, take_profit, series,
                )
            except Exception as exc:
                logger.error("DynamoDB write failed for live buy %s: %s", ticker, exc)

    return spent, bought


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

    # Build set of active tickers so live scanner doesn't double-buy
    existing_tickers = {r.market_ticker for r in records}

    for record in records:
        if record.status == "watching":
            cost = _process_watching(record, client, available - spent, open_count)
            if cost > 0:
                spent      += cost
                open_count += 1
                buys       += 1

        elif record.status == "bought":
            _process_bought(record, client)
            if record.status == "closed":
                open_count = max(0, open_count - 1)
                sells += 1

    # Live in-game scanner — finds and buys in-progress games not already in watchlist
    live_spent, live_buys = 0.0, 0
    if available - spent >= 1.00 and open_count < MAX_POSITIONS:
        try:
            live_spent, live_buys = _scan_live_games(
                client, available - spent, open_count, existing_tickers
            )
            spent      += live_spent
            open_count += live_buys
            buys       += live_buys
        except Exception as exc:
            logger.warning("Live game scan failed (non-fatal): %s", exc)

    return {
        "window":          "carpet_bagger_monitor",
        "balance":         round(live_balance, 2),
        "available":       round(available - spent, 2),
        "buys":            buys,
        "sells":           sells,
        "open_positions":  open_count,
        "live_scan_buys":  live_buys,
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
# Baseball exit — close all MLB positions at entry + $0.02 (break-even + margin)
# ---------------------------------------------------------------------------

_BASEBALL_SERIES = {"KXMLBGAME", "KXMLBSTGAME", "KXNCAABBGAME", "KXNCAABASEGAME"}

def baseball_exit(cfg: dict | None = None) -> dict:
    """
    Place resting sell limits on all open baseball positions at entry_price + $0.02.
    Cancels any existing resting sell first.

    Trigger via Lambda: {"window": "carpet_bagger_baseball_exit"}
    """
    logger.info("=== Baseball exit: setting break-even sells ===")

    api_key = os.environ.get("KALSHI_API_KEY", "")
    rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
    if not api_key or not rsa_key:
        return {"error": "missing credentials"}

    client  = KalshiClient(api_key, rsa_key)
    records = _load_active_records()
    baseball = [r for r in records if r.sport in _BASEBALL_SERIES and r.status == "bought"]

    if not baseball:
        logger.info("Baseball exit: no open baseball positions found")
        return {"exited": 0}

    exited = 0
    for record in baseball:
        exit_price = round(min(record.entry_price + 0.02, 0.99), 2)
        exit_price = max(exit_price, 0.03)  # Kalshi floor

        # Cancel existing resting sell
        if record.sell_order_id:
            try:
                client.cancel_order(record.sell_order_id)
                logger.info("Cancelled old sell order %s for %s", record.sell_order_id, record.market_ticker)
            except Exception as exc:
                logger.warning("Could not cancel sell order %s: %s", record.sell_order_id, exc)

        # Place new resting sell at entry + $0.02
        try:
            sell_result   = client.place_sell(record.market_ticker, record.contract_count, yes_bid_dollars=exit_price)
            sell_order_id = (sell_result.get("order", {}) if isinstance(sell_result, dict) else {}).get("order_id", "")
            record.sell_order_id = sell_order_id
            _update_record(record)
            exited += 1
            logger.info(
                "Baseball exit: %s | %d contracts | resting sell @ $%.2f (entry was $%.2f)",
                record.market_ticker, record.contract_count, exit_price, record.entry_price,
            )
        except Exception as exc:
            logger.error("Baseball exit sell failed for %s: %s", record.market_ticker, exc)

    return {"exited": exited, "tickers": [r.market_ticker for r in baseball]}


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
