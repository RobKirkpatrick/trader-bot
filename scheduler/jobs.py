"""
Scheduler jobs — scan windows:
  08:00 ET  Pre-market scan
  09:35 ET  Market-open scan
  12:00 ET  Midday scan
  15:45 ET  End-of-day stop-loss review

Each scan:
  1. Fetches live account state (buying power, open positions)
  2. Runs multi-source sentiment scan
  3. For each strong signal that passes risk checks:
       - Places the order automatically (no human confirmation)
       - Polls for fill status
  4. Emails a plain-English summary of what happened

Trade strategy:
  - Bullish (score ≥ 0.20):  buy stock at market
  - Strong bullish (≥ 0.35): try a single-leg call first; fall back to stock if unaffordable
  - Bearish (index ETFs only): bear put spread (defined-risk, no short-selling needed)
  - Duplicate guard: won't buy any ticker (or option on it) already held in open positions
"""

import logging
import time
import uuid
from datetime import datetime, timezone

import boto3
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import settings
from sentiment.scanner import SentimentScanner, TickerSentiment
from core.risk import RiskManager, TradeSignal
from broker.public_client import PublicClient

logger = logging.getLogger(__name__)

# DTE window and spread width for automated options plays
_OPTIONS_DTE_MIN      = 14   # at least 2 weeks out
_OPTIONS_DTE_MAX      = 45   # no more than ~6 weeks
_PUT_SPREAD_WIDTH_PCT = 0.02  # bear put spread width (e.g. $580/$568 on SPY)

# Bear put spreads restricted to liquid index ETFs — individual stocks have wide
# bid-ask spreads that destroy value and illiquid legs that are hard to close.
_INDEX_ETF_TICKERS = {"SPY", "QQQ", "IWM"}


def _base_tickers(positions: list[dict]) -> set[str]:
    """
    Extract base stock tickers from all positions, including options.
    Options symbols (e.g. AAPL240621C00185000) start with the underlying ticker.
    """
    import re
    result = set()
    for p in positions:
        sym = (
            p.get("instrument", {}).get("symbol")
            or p.get("symbol") or p.get("ticker", "")
        ).upper()
        if sym:
            m = re.match(r'^([A-Z]+)', sym)
            if m:
                result.add(m.group(1))
    return result


def _options_tickers(positions: list[dict]) -> set[str]:
    """
    Return base tickers that have an existing OPTIONS position (not plain stock).
    Options symbols contain digits (e.g. MRNA260320C00050000); plain stock symbols don't.
    """
    import re
    result = set()
    for p in positions:
        sym = (
            p.get("instrument", {}).get("symbol")
            or p.get("symbol") or p.get("ticker", "")
        ).upper()
        if sym and re.search(r'\d', sym):
            m = re.match(r'^([A-Z]+)', sym)
            if m:
                result.add(m.group(1))
    return result


# ---------------------------------------------------------------------------
# Intra-day position rotation
# ---------------------------------------------------------------------------

def _close_intraday(position: dict, client, reason: str) -> dict:
    """
    Close a single position mid-day.
    Uses place_options_order for options symbols (contain digits), place_order for stocks.
    """
    import re
    sym = (position.get("instrument", {}).get("symbol") or position.get("symbol") or "").upper()
    qty = _safe_float(position.get("quantity") or position.get("shares"))
    result = {
        "ticker":   sym,
        "signal":   "close",
        "score":    0.0,
        "action":   "skipped",
        "reason":   reason,
        "order_id": None,
        "status":   None,
        "amount":   None,
    }
    if not sym or qty is None or qty <= 0:
        result["reason"] = f"{reason} — skipped (qty={qty})"
        return result
    is_options = bool(re.search(r'\d', sym))
    try:
        if is_options:
            order = client.place_options_order(
                option_symbol=sym,
                side="SELL",
                quantity=str(int(qty)),
                order_type="MARKET",
            )
        else:
            order = client.place_order(
                symbol=sym,
                side="SELL",
                order_type="MARKET",
                quantity=str(qty),
            )
        result["order_id"] = order.get("orderId", "")
        result["action"]   = "closed"
        result["amount"]   = f"{qty:.4f} {'contracts' if is_options else 'shares'}"
        logger.info(
            "Intraday close: SELL %s ×%s | reason=%s | orderId=%s",
            sym, qty, reason, result["order_id"],
        )
    except Exception as exc:
        result["action"] = "error"
        result["reason"] = f"{reason} — close failed: {exc}"
        logger.error("Intraday close failed for %s: %s", sym, exc)
    return result


def _evaluate_intraday_rotation(
    positions: list[dict],
    score_map: dict[str, float],
    strong: list,
    client,
    account_equity: float = 0.0,
) -> list[dict]:
    """
    Return positions to close intra-day for two reasons:

    1. Signal reversal — holding long but score has dropped to bearish (≤ SENTIMENT_SELL_THRESHOLD).
       Avoids riding a position through a reversal until EOD.

    2. Rotation — score is weak (< 0.15) AND the position is at a profit AND there are strong
       new bullish signals (≥ SENTIMENT_OPTIONS_CALL_THRESHOLD) for tickers not currently held.
       Frees capital for better opportunities.

    Skips short legs (qty ≤ 0) to avoid touching spread short sides.
    Skips any position bought today (PDT protection — no same-day round trips).
    """
    import re
    to_close = []

    # PDT guard: never sell a position bought on the same calendar day (skipped if account ≥ $25k)
    today_buys = _get_today_buy_symbols(account_equity)
    if today_buys is None:
        logger.warning("PDT guard: skipping all intraday rotation (DynamoDB unavailable)")
        return []

    # Tickers with strong new signals not currently held
    held_base  = _base_tickers(positions)
    strong_new = {
        ts.ticker for ts in strong
        if ts.score >= settings.SENTIMENT_OPTIONS_CALL_THRESHOLD
        and ts.ticker not in held_base
    }

    # Fetch current prices for all held positions (needed for profit check)
    held_syms = [
        (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        for p in positions
    ]
    held_syms = [s for s in held_syms if s]
    price_map: dict[str, float] = {}
    if held_syms:
        try:
            resp = client.get_quotes(held_syms)
            for q in (resp.get("quotes", []) if isinstance(resp, dict) else resp):
                sym = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
                raw = q.get("last") or q.get("lastPrice") or q.get("price")
                if sym and raw:
                    try:
                        price_map[sym] = float(raw)
                    except (ValueError, TypeError):
                        pass
        except Exception as exc:
            logger.warning("Rotation price fetch failed: %s", exc)

    for p in positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        qty = _safe_float(p.get("quantity") or p.get("shares"))
        if not sym or qty is None or qty <= 0:
            continue  # skip short legs or missing data

        m = re.match(r'^([A-Z]+)', sym)
        if not m:
            continue
        base = m.group(1)

        score = score_map.get(base)
        if score is None:
            continue  # not in watchlist, skip

        # PDT: skip if this ticker was bought today
        if base in today_buys:
            logger.info("PDT guard: skipping intraday sell of %s — opened today", base)
            continue

        # 1. Signal reversal: long but signal has gone bearish
        if score <= settings.SENTIMENT_SELL_THRESHOLD:
            to_close.append({"position": p, "reason": f"Signal reversal: {base} score={score:+.3f}"})
            continue

        # 2. Rotation: weak signal + at profit + better opportunity available
        if score < 0.15 and strong_new:
            current_price = price_map.get(sym, 0.0)
            cost_basis    = p.get("costBasis")
            avg_price     = (
                _safe_float(cost_basis.get("unitCost")) if isinstance(cost_basis, dict)
                else _safe_float(cost_basis)
            ) or _safe_float(p.get("averagePrice")) or _safe_float(p.get("avgCostPerShare"))
            if avg_price > 0 and current_price > avg_price:
                to_close.append({
                    "position": p,
                    "reason": (
                        f"Rotation: {base} score={score:+.3f} weak, "
                        f"freeing capital for {', '.join(sorted(strong_new))}"
                    ),
                })

    return to_close


# ---------------------------------------------------------------------------
# Market hours check
# ---------------------------------------------------------------------------

def _market_is_open() -> bool:
    """
    True during regular US market hours: 9:30–16:00 ET, Mon–Fri.
    Uses UTC; ET = UTC-5 (EST) or UTC-4 (EDT).
    This is a simple check — does not account for market holidays.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()       # 0=Mon … 4=Fri
    if weekday >= 5:
        return False

    # Approximate ET offset — good enough for scheduling purposes
    # (EventBridge fires at 8am and 12pm ET which are always market-relevant windows)
    hour_utc = now_utc.hour
    minute_utc = now_utc.minute

    # 9:30 ET = 14:30 UTC (EST) or 13:30 UTC (EDT)
    # 16:00 ET = 21:00 UTC (EST) or 20:00 UTC (EDT)
    # Be conservative: 13:30–21:00 UTC covers both DST states
    total_minutes = hour_utc * 60 + minute_utc
    return 810 <= total_minutes <= 1260   # 13:30–21:00 UTC


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def _execute_bear_put_spread(
    ts: TickerSentiment,
    client: PublicClient,
    risk_manager: RiskManager,
    positions: list[dict],
    position_size_usd: float,
) -> dict:
    """
    Bear put spread on a broad-market index (SPY/QQQ) for a bearish signal.

    Strategy: BUY ATM put + SELL lower put (2% below) — same expiration.
    Defined max loss = net debit paid. Much cheaper than an outright put.
    """
    result = {
        "ticker":   ts.ticker,
        "signal":   ts.signal,
        "score":    ts.score,
        "action":   "skipped",
        "reason":   "",
        "order_id": None,
        "status":   None,
        "amount":   None,
    }

    try:
        # Get current price
        quotes = client.get_quotes([ts.ticker])
        quote_list = quotes.get("quotes", []) if isinstance(quotes, dict) else quotes
        current_price = 0.0
        for q in quote_list:
            sym = (
                q.get("instrument", {}).get("symbol")
                or q.get("symbol") or q.get("ticker") or ""
            ).upper()
            if sym == ts.ticker:
                for field in ("last", "lastPrice", "ask", "price"):
                    v = q.get(field)
                    if v:
                        current_price = float(v)
                        break

        if current_price <= 0:
            result["reason"] = f"Could not get current price for {ts.ticker}"
            return result

        # Pick expiration
        expirations = client.get_option_expirations(ts.ticker)
        if not expirations:
            result["reason"] = f"No option expirations available for {ts.ticker}"
            return result

        from datetime import date as date_type
        today = datetime.now(timezone.utc).date()
        target_dte = (_OPTIONS_DTE_MIN + _OPTIONS_DTE_MAX) / 2
        candidates = []
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if _OPTIONS_DTE_MIN <= dte <= _OPTIONS_DTE_MAX:
                    candidates.append((abs(dte - target_dte), exp_str))
            except ValueError:
                continue

        if not candidates:
            result["reason"] = f"No expiration in {_OPTIONS_DTE_MIN}-{_OPTIONS_DTE_MAX} DTE window"
            return result

        candidates.sort()
        expiration = candidates[0][1]

        # Build strikes: ATM put (long) and 2% OTM put (short)
        long_strike  = round(current_price * (1 - 0.005), 0)   # ~0.5% OTM long
        short_strike = round(current_price * (1 - _PUT_SPREAD_WIDTH_PCT - 0.005), 0)

        # Round to nearest dollar (most index options use $1 increments)
        long_strike_str  = f"{int(long_strike)}.00"
        short_strike_str = f"{int(short_strike)}.00"

        legs = [
            PublicClient.make_option_leg(
                base_symbol=ts.ticker, option_type="PUT",
                strike=long_strike_str, expiration=expiration,
                side="BUY", open_close="OPEN", ratio=1,
            ),
            PublicClient.make_option_leg(
                base_symbol=ts.ticker, option_type="PUT",
                strike=short_strike_str, expiration=expiration,
                side="SELL", open_close="OPEN", ratio=1,
            ),
        ]

        # Preflight to get estimated net debit
        try:
            pf = client.preflight_multi_leg(
                legs=legs, quantity="1",
                order_type="LIMIT", limit_price="1.00",
            )
            net_debit = float(pf.get("estimatedCost") or pf.get("buyingPowerRequirement") or 0)
            logger.info(
                "Bear put spread preflight — %s %s/%s exp=%s net_debit=~$%.2f",
                ts.ticker, long_strike_str, short_strike_str, expiration, net_debit,
            )
        except Exception as exc:
            logger.warning("Multi-leg preflight failed: %s — placing with limit $1.00", exc)
            net_debit = 1.0

        # Determine contracts: how many can we afford within position_size_usd?
        # Each contract controls 100 shares, so cost = net_debit * 100
        cost_per_contract = max(net_debit * 100, 1.0)
        contracts = max(1, int(position_size_usd / cost_per_contract))

        order = client.place_multi_leg(
            legs=legs, quantity=str(contracts),
            order_type="LIMIT", limit_price=f"{net_debit:.2f}",
        )
        order_id = order.get("orderId", "")
        result["action"]   = "order_placed"
        result["order_id"] = order_id
        result["amount"]   = f"${contracts} contract(s) @ ${net_debit:.2f} debit"
        logger.info("Bear put spread placed: %s", order_id)

        # Poll
        if order_id:
            for _ in range(5):
                time.sleep(3)
                try:
                    status = client.get_order(order_id)
                    state = (status.get("status") or status.get("orderStatus") or "").upper()
                    result["status"] = state
                    if state in ("FILLED", "CANCELLED", "REJECTED"):
                        break
                except Exception:
                    break

    except Exception as exc:
        result["action"] = "error"
        result["reason"] = str(exc)
        logger.error("Bear put spread failed for %s: %s", ts.ticker, exc)

    return result


def _execute_buy_call(
    ts: TickerSentiment,
    client: PublicClient,
    risk_manager: RiskManager,
    positions: list[dict],
    position_size_usd: float,
) -> dict:
    """
    Buy a single-leg call option for a very strong bullish signal.

    Finds the cheapest ATM-or-OTM call within the 14-45 DTE window that
    costs ≤ position_size_usd for 1 contract. Returns "skipped" if no
    affordable contract exists (caller falls back to buying stock).
    """
    result = {
        "ticker":   ts.ticker,
        "signal":   ts.signal,
        "score":    ts.score,
        "action":   "skipped",
        "reason":   "",
        "order_id": None,
        "status":   None,
        "amount":   None,
    }

    try:
        # Current price
        quotes = client.get_quotes([ts.ticker])
        quote_list = quotes.get("quotes", []) if isinstance(quotes, dict) else quotes
        current_price = 0.0
        for q in quote_list:
            sym = (
                q.get("instrument", {}).get("symbol")
                or q.get("symbol") or q.get("ticker") or ""
            ).upper()
            if sym == ts.ticker:
                for field in ("last", "lastPrice", "ask", "price"):
                    v = q.get(field)
                    if v:
                        current_price = float(v)
                        break

        if current_price <= 0:
            result["reason"] = f"Could not get current price for {ts.ticker}"
            return result

        # Pick expiration
        expirations = client.get_option_expirations(ts.ticker)
        if not expirations:
            result["reason"] = f"No option expirations available for {ts.ticker}"
            return result

        today = datetime.now(timezone.utc).date()
        target_dte = (_OPTIONS_DTE_MIN + _OPTIONS_DTE_MAX) / 2
        candidates = []
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if _OPTIONS_DTE_MIN <= dte <= _OPTIONS_DTE_MAX:
                    candidates.append((abs(dte - target_dte), exp_str))
            except ValueError:
                continue

        if not candidates:
            result["reason"] = f"No expiration in {_OPTIONS_DTE_MIN}-{_OPTIONS_DTE_MAX} DTE window"
            return result

        candidates.sort()
        expiration = candidates[0][1]

        # Get call chain; walk from ATM outward until we find an affordable contract
        chain = client.get_option_chain(ts.ticker, expiration, option_type="CALL")
        if not chain:
            result["reason"] = f"Empty call chain for {ts.ticker} exp={expiration}"
            return result

        # Sort ascending by strike distance from current price (ATM first)
        chain.sort(key=lambda c: abs(float(c.get("strikePrice", 0)) - current_price))

        chosen = None
        chosen_mid = 0.0
        for contract in chain[:10]:
            strike = float(contract.get("strikePrice", 0))
            if strike < current_price * 0.99:   # skip deep ITM (expensive)
                continue
            bid = float(contract.get("bid") or 0)
            ask = float(contract.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            if mid * 100 <= position_size_usd:   # 1 contract = 100 shares
                chosen = contract
                chosen_mid = mid
                break

        if not chosen:
            result["reason"] = (
                f"No affordable call for {ts.ticker} within ${position_size_usd:.0f} budget"
            )
            return result

        option_symbol = chosen.get("optionSymbol", "")
        strike_str    = chosen.get("strikePrice", "")

        order = client.place_options_order(
            option_symbol=option_symbol,
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            limit_price=f"{chosen_mid:.2f}",
        )
        order_id = order.get("orderId", "")
        result["action"]   = "order_placed"
        result["order_id"] = order_id
        result["amount"]   = f"1 call @ ${chosen_mid:.2f} (strike={strike_str} exp={expiration})"
        logger.info("Call option placed: %s %s", ts.ticker, result["amount"])

        # Poll for fill
        if order_id:
            for _ in range(5):
                time.sleep(3)
                try:
                    status = client.get_order(order_id)
                    state = (status.get("status") or status.get("orderStatus") or "").upper()
                    result["status"] = state
                    if state in ("FILLED", "CANCELLED", "REJECTED"):
                        break
                except Exception:
                    break

    except Exception as exc:
        result["action"] = "error"
        result["reason"] = str(exc)
        logger.error("Call buy failed for %s: %s", ts.ticker, exc)

    return result


def _execute_signal(
    ts: TickerSentiment,
    client: PublicClient,
    risk_manager: RiskManager,
    positions: list[dict],
    position_size_usd: float,
) -> dict:
    """
    Attempt to place an order for a single signal.

    Strategy:
      - Bearish (any ticker): bear put spread (defined-risk, no short-selling needed)
      - Very strong bullish (≥ SENTIMENT_OPTIONS_CALL_THRESHOLD): try a call first;
        fall back to buying stock if no affordable contract exists
      - Normal bullish: buy stock at market
    """
    result = {
        "ticker":    ts.ticker,
        "signal":    ts.signal,
        "score":     ts.score,
        "action":    "skipped",
        "reason":    "",
        "order_id":  None,
        "status":    None,
        "amount":    None,
    }

    # Bearish → bear put spread, but only on liquid index ETFs
    if ts.signal == "bearish":
        if ts.ticker not in _INDEX_ETF_TICKERS:
            result["reason"] = (
                f"Bearish on {ts.ticker} — put spreads restricted to index ETFs "
                f"({', '.join(sorted(_INDEX_ETF_TICKERS))})"
            )
            return result
        return _execute_bear_put_spread(ts, client, risk_manager, positions, position_size_usd)

    # Very strong bullish → try a call option first for leveraged upside;
    # fall back to stock if no affordable contract exists
    if ts.score >= settings.SENTIMENT_OPTIONS_CALL_THRESHOLD:
        call_result = _execute_buy_call(ts, client, risk_manager, positions, position_size_usd)
        if call_result["action"] == "order_placed":
            return call_result
        logger.info(
            "Call not placed for %s (%s) — buying stock instead",
            ts.ticker, call_result["reason"],
        )

    # Risk check
    signal = TradeSignal(
        ticker=ts.ticker,
        direction="buy",
        sentiment_score=ts.score,
        current_price=1.0,   # price check skipped for notional orders
    )
    assessment = risk_manager.evaluate(signal, positions)
    if not assessment.approved:
        result["reason"] = f"Risk rejected: {assessment.reason}"
        return result

    # Place stock market order
    amount_str = f"{position_size_usd:.2f}"
    try:
        order = client.place_order(
            symbol=ts.ticker,
            side="BUY",
            order_type="MARKET",
            amount=amount_str,
        )
    except Exception as exc:
        result["action"] = "error"
        result["reason"] = str(exc)
        logger.error("Order failed for %s: %s", ts.ticker, exc)
        return result

    order_id = order.get("orderId", "")
    result["action"]   = "order_placed"
    result["order_id"] = order_id
    result["amount"]   = amount_str
    logger.info("Order placed: BUY $%s %s | orderId=%s", amount_str, ts.ticker, order_id)

    # Poll for fill (up to 15s)
    if order_id:
        for _ in range(5):
            time.sleep(3)
            try:
                status = client.get_order(order_id)
                state = (status.get("status") or status.get("orderStatus") or "").upper()
                result["status"] = state
                if state in ("FILLED", "CANCELLED", "REJECTED"):
                    break
            except Exception:
                break

    return result


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def _build_alert_message(
    window: str,
    scan_results: list[TickerSentiment],
    trade_results: list[dict],
    positions_before: list[dict],
    buying_power: float,
    risk_manager: RiskManager,
    market_open: bool,
    macro_summary: str = "",
    edgar_stats: dict | None = None,
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")
    daily_limit = risk_manager.account_size * settings.DAILY_LOSS_LIMIT_PCT

    lines = [
        f"TraderBot — {window}",
        f"{now_str}",
        "─" * 42,
        f"Account:   ${buying_power:,.2f} buying power",
        f"Positions: {len(positions_before)} open",
        f"Risk cap:  ${risk_manager.daily_loss_remaining():,.2f} left today  "
        f"(max ${daily_limit:,.2f}/day)",
    ]

    if macro_summary:
        lines += ["", f"Market read: {macro_summary}"]

    if edgar_stats:
        lines.append(
            f"EDGAR 8-K: {edgar_stats.get('scanned', 0)} tickers checked, "
            f"{edgar_stats.get('high_impact', 0)} high-impact, "
            f"{edgar_stats.get('sent_to_claude', 0)} sent to Claude"
        )

    lines += ["─" * 42, ""]

    # ---- Trades placed ----
    closed  = [r for r in trade_results if r["action"] == "closed"]
    placed  = [r for r in trade_results if r["action"] == "order_placed"]
    skipped = [r for r in trade_results if r["action"] == "skipped"]
    errors  = [r for r in trade_results if r["action"] == "error"]

    if not market_open:
        lines.append("Market closed — scan ran but no orders placed.")
        lines.append("(Scheduled scans fire at 8am and 12pm ET when markets are open.)")
        lines.append("")

    if closed:
        lines.append(f"POSITIONS CLOSED (intraday): {len(closed)}")
        lines.append("")
        for r in closed:
            lines.append(f"  SELL {r['amount']} {r['ticker']}  — {r['reason']}")
            lines.append(f"  Order ID: {r['order_id']}")
            lines.append("")

    if placed:
        lines.append(f"ORDERS PLACED: {len(placed)}")
        lines.append("")
        for r in placed:
            status_str = f" → {r['status']}" if r["status"] else " → pending fill"
            lines.append(
                f"  BUY ${r['amount']} {r['ticker']}  "
                f"(score: {r['score']:+.3f}){status_str}"
            )
            lines.append(f"  Order ID: {r['order_id']}")
            lines.append("")

    elif market_open and not closed and trade_results:
        lines.append("No orders placed.")
        lines.append("")

    # ---- Signals detected (all) ----
    strong = [ts for ts in scan_results
              if ts.score >= settings.SENTIMENT_BUY_THRESHOLD
              or ts.score <= settings.SENTIMENT_SELL_THRESHOLD]

    if strong:
        lines.append("SIGNALS DETECTED:")
        for ts in strong:
            arrow = "▲" if ts.signal == "bullish" else "▼"
            earn  = " [EARNINGS]" if ts.earnings_imminent else ""
            lines.append(
                f"  {arrow} {ts.ticker}: {ts.signal.upper()} "
                f"score={ts.score:+.3f}  "
                f"[pr={ts.price_score:+.3f} fh={ts.finnhub_score:+.3f} "
                f"ma={ts.marketaux_score:+.3f} mc={ts.macro_score:+.3f} "
                f"poly={ts.polygon_score:+.3f} wsb={ts.wsb_score:+.3f}]{earn}"
            )
            # Find trade result for this ticker
            tr = next((r for r in trade_results if r["ticker"] == ts.ticker), None)
            if tr and tr["action"] != "order_placed":
                lines.append(f"    Not traded: {tr['reason']}")
        lines.append("")
    else:
        lines.append("No strong signals this scan.")
        lines.append(
            f"(Thresholds: bullish ≥ {settings.SENTIMENT_BUY_THRESHOLD}  "
            f"bearish ≤ {settings.SENTIMENT_SELL_THRESHOLD})"
        )
        lines.append("")
        # Show top 3 scores anyway so the email is informative
        top3 = sorted(scan_results, key=lambda t: abs(t.score), reverse=True)[:3]
        if top3:
            lines.append("Closest to threshold:")
            for ts in top3:
                lines.append(
                    f"  {ts.ticker}: {ts.score:+.3f}  "
                    f"[price={ts.price_score:+.3f} macro={ts.macro_score:+.3f}]"
                )
        lines.append("")

    if errors:
        lines.append("ORDER ERRORS:")
        for r in errors:
            lines.append(f"  {r['ticker']}: {r['reason']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# EOD: pull today's CloudWatch logs for Claude to summarise
# ---------------------------------------------------------------------------

def _fetch_todays_log_events() -> str:
    """
    Pull today's Lambda log events from CloudWatch Logs.

    Returns a plain-text string of the most relevant trading log lines
    (signals, orders, macro reads) for Claude to summarise.
    Falls back to an empty string silently if permissions are missing.
    """
    import os
    from datetime import date

    log_group = os.environ.get(
        "AWS_LAMBDA_LOG_GROUP_NAME", "/aws/lambda/trading-bot-sentiment"
    )
    today_prefix = date.today().strftime("%Y/%m/%d")

    keywords = [
        "window", "Buying power", "Open positions", "Strong signals",
        "Order placed", "Risk rejected", "Macro score", "Claude macro",
        "BUY", "SELL", "FILLED", "price signal", "scan starting",
        "signals_found", "orders_placed",
    ]

    try:
        logs_client = boto3.client("logs", region_name=settings.AWS_REGION)
        streams = logs_client.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=today_prefix,
            orderBy="LastEventTime",
            descending=True,
            limit=6,
        ).get("logStreams", [])
    except Exception as exc:
        logger.debug("CloudWatch log read skipped: %s", exc)
        return ""

    lines: list[str] = []
    for stream in streams:
        try:
            events = logs_client.get_log_events(
                logGroupName=log_group,
                logStreamName=stream["logStreamName"],
                startFromHead=True,
            ).get("events", [])
            for e in events:
                msg = e.get("message", "").strip()
                if any(k.lower() in msg.lower() for k in keywords):
                    # Strip Lambda timestamp/request-id prefix noise
                    clean = msg.split("\t")[-1] if "\t" in msg else msg
                    lines.append(clean)
        except Exception:
            continue

    return "\n".join(lines[:100])  # cap to keep Claude prompt lean


def _generate_eod_narrative(
    position_reviews: list[dict],
    buying_power: float,
    log_text: str,
) -> str:
    """
    Ask Claude Haiku to write a 3–5 sentence plain-English daily recap
    based on today's log activity and current portfolio state.
    Returns empty string on failure.
    """
    import anthropic

    positions_text = "\n".join(
        f"  {r['symbol']}: qty={r['qty']:.4f}  current=${r['current_price']:.2f}"
        + (f"  avg=${r['avg_price']:.2f}  P&L={r['pnl_pct']:+.1%}"
           if r["pnl_pct"] is not None else "  (no cost basis)")
        + (" [CLOSED TODAY]" if r["action"] == "closed" else "")
        for r in position_reviews
    ) or "  (none)"

    context_parts = [
        f"End-of-day cash: ${buying_power:,.2f}",
        f"\nCurrent open positions:\n{positions_text}",
    ]
    if log_text:
        context_parts.append(f"\nToday's trading log (key events):\n{log_text}")

    user_content = "\n".join(context_parts)

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=(
                "You are a trading assistant writing an end-of-day recap for a retail investor. "
                "Write 3–5 sentences in plain English covering: what the bot traded today and why "
                "(reference the macro theme and price signals), how the portfolio is performing, "
                "and any notable events (stop-losses, position limits hit, strong signals). "
                "Be specific with tickers and numbers. Tone: concise, professional. "
                "No bullet points — flowing prose only. No intro like 'Here is your recap'."
            ),
            messages=[{"role": "user", "content": user_content}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        logger.warning("EOD narrative generation failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# EOD email builder
# ---------------------------------------------------------------------------

def _build_eod_message(
    window: str,
    position_reviews: list[dict],
    buying_power: float,
    narrative: str = "",
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")
    lines = [
        f"TraderBot — {window}",
        f"{now_str}",
        "─" * 42,
        f"Cash available: ${buying_power:,.2f}",
        f"Open positions: {len(position_reviews)}",
        "",
    ]

    # AI-generated narrative at the top
    if narrative:
        lines += ["TODAY'S RECAP:", narrative, ""]

    if not position_reviews:
        lines.append("No open positions. Nothing to review.")
        lines.append("Market closes in ~15 minutes.")
        return "\n".join(lines)

    # Stop-loss closures
    closed = [r for r in position_reviews if r["action"] in ("closed", "close_failed")]
    if closed:
        lines.append("STOP-LOSS TRIGGERED:")
        for r in closed:
            pnl_str = f"{r['pnl_pct']:+.1%}" if r["pnl_pct"] is not None else "n/a"
            usd_str = f"  ${r['pnl_usd']:+.2f}" if r["pnl_usd"] is not None else ""
            status  = "SOLD" if r["action"] == "closed" else "FAILED"
            lines.append(f"  [{status}] {r['symbol']}: {pnl_str}{usd_str}")
            if r["order_id"]:
                lines.append(f"    Order ID: {r['order_id']}")
            if r["action"] == "close_failed":
                lines.append(f"    Error: {r['close_reason']}")
        lines.append("")

    # Full portfolio snapshot
    lines.append("PORTFOLIO SNAPSHOT:")
    total_pnl = 0.0
    for r in position_reviews:
        price_str = f"${r['current_price']:.2f}" if r["current_price"] else "n/a"
        avg_str   = f"${r['avg_price']:.2f}"      if r["avg_price"]     else "cost n/a"
        pnl_str   = f"{r['pnl_pct']:+.1%}"        if r["pnl_pct"] is not None else "P&L n/a"
        usd_str   = f"  (${r['pnl_usd']:+.2f})"   if r["pnl_usd"] is not None else ""
        flag      = " ← CLOSED" if r["action"] in ("closed", "close_failed") else ""
        lines.append(
            f"  {r['symbol']:<6} {price_str:>8}  avg {avg_str:>8}  {pnl_str}{usd_str}{flag}"
        )
        if r["pnl_usd"] is not None:
            total_pnl += r["pnl_usd"]

    lines += [
        "",
        f"Total unrealized P&L: ${total_pnl:+.2f}",
        "",
        "Market closes in ~15 minutes.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SNS publish
# ---------------------------------------------------------------------------

def _publish_sns(message: str, subject: str) -> None:
    arn_parts = settings.SNS_TOPIC_ARN.split(":")
    sns_region = arn_parts[3] if len(arn_parts) >= 4 else settings.AWS_REGION
    sns = boto3.client("sns", region_name=sns_region)
    sns.publish(TopicArn=settings.SNS_TOPIC_ARN, Subject=subject, Message=message)
    logger.info("SNS published: %s", subject)


# ---------------------------------------------------------------------------
# DynamoDB decision log
# ---------------------------------------------------------------------------

_LOG_TABLE = "trading-bot-logs"


def _dynamodb():
    return boto3.client("dynamodb", region_name=settings.AWS_REGION)


_PDT_EQUITY_THRESHOLD = 25_000.0   # FINRA PDT rule applies to accounts below this equity value


def _get_today_buy_symbols(account_equity: float = 0.0) -> set[str] | None:
    """
    Return base tickers where an order was placed today (UTC calendar day).
    Used to prevent same-day buy/sell round trips that trigger Pattern Day Trader rules.

    PDT rules only apply to accounts under $25,000. If account_equity >= $25,000,
    returns an empty set immediately (no restriction).

    Returns None on DynamoDB failure — callers should skip all sells when None,
    treating the failure as a conservative block rather than allowing potential PDT violations.
    """
    if account_equity >= _PDT_EQUITY_THRESHOLD:
        logger.info(
            "PDT guard: account equity $%.0f ≥ $%.0f threshold — PDT rules do not apply",
            account_equity, _PDT_EQUITY_THRESHOLD,
        )
        return set()   # empty set = no tickers to protect

    import re
    from datetime import date
    try:
        db    = _dynamodb()
        today = date.today().isoformat()   # "2026-03-06"
        resp  = db.scan(
            TableName=_LOG_TABLE,
            FilterExpression="begins_with(#ts, :today) AND action_taken = :action",
            ExpressionAttributeNames={"#ts": "timestamp"},
            ExpressionAttributeValues={
                ":today":  {"S": today},
                ":action": {"S": "order_placed"},
            },
            ProjectionExpression="symbol",
        )
        result: set[str] = set()
        for item in resp.get("Items", []):
            sym = item.get("symbol", {}).get("S", "").upper()
            if sym:
                m = re.match(r'^([A-Z]+)', sym)
                if m:
                    result.add(m.group(1))
        logger.info("PDT guard: %d symbol(s) bought today: %s", len(result), result or "none")
        return result
    except Exception as exc:
        logger.warning("PDT guard: DynamoDB query failed (%s) — blocking all intraday sells", exc)
        return None  # None = fail-safe: don't sell anything


def _log_decision(
    symbol: str,
    decision: dict,
    account_balance: dict,
    action_taken: str,
    order_result: dict | None = None,
    edgar_context: dict | None = None,
) -> None:
    """Write every Claude agent decision to DynamoDB trading-bot-logs."""
    try:
        db = _dynamodb()
        item = {
            "trade_id":       {"S": str(uuid.uuid4())},
            "timestamp":      {"S": datetime.now(timezone.utc).isoformat()},
            "type":           {"S": "agent_decision"},
            "symbol":         {"S": symbol},
            "execute":        {"BOOL": bool(decision.get("execute", False))},
            "confidence":     {"S": str(decision.get("confidence", ""))},
            "reason":         {"S": str(decision.get("reason", ""))},
            "contract":       {"S": str(decision.get("contract", {}))},
            "position_size":  {"N": str(decision.get("position_size_dollars", 0))},
            "cash_balance":   {"N": str(account_balance.get("cash_balance", 0))},   # Live from Public.com API — do not hardcode
            "buying_power":   {"N": str(account_balance.get("buying_power", 0))},   # Live from Public.com API — do not hardcode
            "action_taken":   {"S": action_taken},
        }
        if order_result:
            item["order_result"] = {"S": str(order_result)}
        if edgar_context:
            item["edgar_items"]         = {"S": ",".join(edgar_context.get("items", []))}
            item["edgar_score"]         = {"N": str(edgar_context.get("score", 0.0))}
            item["filing_url"]          = {"S": edgar_context.get("filing_url", "")}
            item["filing_text_snippet"] = {"S": edgar_context.get("filing_text", "")[:500]}
        db.put_item(TableName=_LOG_TABLE, Item=item)
    except Exception as exc:
        logger.warning("DynamoDB decision log failed: %s", exc)


# ---------------------------------------------------------------------------
# Debug SNS helper
# ---------------------------------------------------------------------------

def _publish_debug_sns(
    symbol: str,
    decision: dict,
    account_balance: dict,
    data_bundle: dict,
    edgar_context: dict | None = None,
) -> None:
    """Fire an SNS debug alert for every agent decision when TRADE_DEBUG=true."""
    if not settings.TRADE_DEBUG:
        return

    cash    = account_balance.get("cash_balance", 0.0)   # Live from Public.com API — do not hardcode
    bp      = account_balance.get("buying_power", 0.0)   # Live from Public.com API — do not hardcode
    budget  = cash * 0.05

    # Check for null/missing fields in the data bundle
    quote       = data_bundle.get("quote", {})
    contracts   = data_bundle.get("top_contracts", [])
    data_ok     = bool(quote.get("last")) and bool(contracts)

    msg = (
        f"TRADE DEBUG\n"
        f"Symbol:          {symbol}\n"
        f"Execute:         {decision.get('execute')}\n"
        f"Confidence:      {decision.get('confidence')}\n"
        f"Reason:          {decision.get('reason')}\n"
        f"Cash Balance:    ${cash:,.2f}\n"
        f"Buying Power:    ${bp:,.2f}\n"
        f"Position Budget: ${budget:,.2f}\n"
        f"Data complete:   {'yes' if data_ok else 'no — quote last={} contracts={}'.format(quote.get('last'), len(contracts))}\n"
        f"Contract:        {decision.get('contract')}\n"
        f"Pos size:        ${decision.get('position_size_dollars', 0):,.2f}"
    )
    if edgar_context:
        msg += (
            f"\nEDGAR 8-K:\n"
            f"  Items:    {edgar_context.get('items')}\n"
            f"  Score:    {edgar_context.get('score', 0):.1f}\n"
            f"  Catalyst: {edgar_context.get('catalyst')}\n"
            f"  Priority: {edgar_context.get('priority')}"
        )
    try:
        _publish_sns(msg, subject=f"[TraderBot DEBUG] {symbol} — execute={decision.get('execute')} ({decision.get('confidence')})")
    except Exception as exc:
        logger.warning("Debug SNS failed: %s", exc)


# ---------------------------------------------------------------------------
# Agent-driven execution
# ---------------------------------------------------------------------------

def _execute_with_agent(
    ts: TickerSentiment,
    client: PublicClient,
    risk_manager: RiskManager,
    positions: list[dict],
    account_balance: dict,
    edgar_context: dict | None = None,
) -> dict:
    """
    Agent-driven trade execution for a single signal.

    Flow:
      1. Fetch live quote + top 5 affordable option contracts from Public.com
      2. Assemble data bundle
      3. Ask Claude Sonnet for a structured trade decision
      4. Log decision to DynamoDB
      5. If TRADE_DEBUG: send SNS with full decision details
      6. Route:
           confidence=high|medium AND execute=true  → place order
           confidence=low AND execute=true          → SNS alert only, no order
           execute=false                            → log reason, skip
    """
    from data.public_options_provider import PublicOptionsProvider
    from core.agent import make_trade_decision, build_data_bundle

    result = {
        "ticker":   ts.ticker,
        "signal":   ts.signal,
        "score":    ts.score,
        "action":   "skipped",
        "reason":   "",
        "order_id": None,
        "status":   None,
        "amount":   None,
    }

    cash = account_balance.get("cash_balance", 0.0)   # Live from Public.com API — do not hardcode
    max_premium = cash * 0.05   # Live from Public.com API — do not hardcode

    # Gather live market data from Public.com
    provider = PublicOptionsProvider(client)
    quote = provider.get_quote(ts.ticker)
    side = "call" if ts.signal == "bullish" else "put"
    top_contracts = []
    try:
        top_contracts = provider.get_best_contracts(ts.ticker, side, max_premium)
    except Exception as exc:
        logger.warning("get_best_contracts failed for %s: %s", ts.ticker, exc)

    # Calculate portfolio exposure and daily P&L from open positions
    total_exposure = 0.0
    daily_pnl = 0.0
    for p in positions:
        exposure = p.get("marketValue") or p.get("market_exposure") or 0
        try:
            total_exposure += float(exposure)
        except (TypeError, ValueError):
            pass

    # Build the bundle and call the agent
    bundle = build_data_bundle(
        ts=ts,
        quote=quote,
        top_contracts=top_contracts,
        account_balance=account_balance,
        open_positions=positions,
        daily_pnl=daily_pnl,
        total_exposure=total_exposure,
        edgar_context=edgar_context,
    )

    decision = make_trade_decision(bundle)

    # Debug SNS — fires on every decision when TRADE_DEBUG=true
    _publish_debug_sns(ts.ticker, decision, account_balance, bundle, edgar_context=edgar_context)

    execute    = bool(decision.get("execute", False))
    confidence = str(decision.get("confidence", "low")).lower()
    reason     = decision.get("reason", "")

    # EDGAR priority: time-sensitive filing — lower confidence bar
    # (treat "low" as "medium" so we execute instead of SNS-only)
    if edgar_context and edgar_context.get("priority") and confidence == "low" and execute:
        logger.info(
            "EDGAR priority signal for %s — treating low confidence as medium (catalyst=%s)",
            ts.ticker, edgar_context.get("catalyst"),
        )
        confidence = "medium"
    contract   = decision.get("contract") or {}
    pos_size   = float(decision.get("position_size_dollars") or max_premium)

    # Route based on decision
    if not execute:
        result["reason"] = f"Agent: {reason}"
        _log_decision(ts.ticker, decision, account_balance, "skipped", edgar_context=edgar_context)
        return result

    if confidence == "low":
        # Low confidence + execute=true → alert only, no order
        alert_msg = (
            f"Agent wants to trade {ts.ticker} but confidence is LOW — waiting for approval.\n"
            f"Reason: {reason}\n"
            f"Contract: {contract}\n"
            f"Size: ${pos_size:.2f}"
        )
        try:
            _publish_sns(alert_msg, subject=f"[TraderBot] Approval needed: {ts.ticker}")
        except Exception as exc:
            logger.warning("Low-confidence SNS failed: %s", exc)
        result["reason"] = f"Low confidence — SNS sent: {reason}"
        result["action"] = "sns_sent"
        _log_decision(ts.ticker, decision, account_balance, "sns_sent", edgar_context=edgar_context)
        return result

    # High or medium confidence + execute=true → place order
    c_type   = (contract.get("type") or "stock").lower()
    c_symbol = (contract.get("symbol") or ts.ticker).strip()

    order    = None
    try:
        if c_type in ("call", "put") and c_symbol and c_symbol != ts.ticker:
            # Options order using agent's chosen contract
            limit_str = str(decision.get("limit_price") or 0) if decision.get("limit_price") else None
            order = client.place_options_order(
                option_symbol=c_symbol,
                side="BUY",
                quantity="1",
                order_type="LIMIT" if limit_str else "MARKET",
                limit_price=limit_str,
            )
        else:
            # Stock buy — use position size in dollars
            order = client.place_order(
                symbol=ts.ticker,
                side="BUY",
                order_type="MARKET",
                amount=f"{pos_size:.2f}",
            )

        order_id = order.get("orderId", "") if order else ""
        result["action"]   = "order_placed"
        result["order_id"] = order_id
        result["amount"]   = f"${pos_size:.2f} ({c_type})"
        logger.info(
            "Agent order placed: %s %s %s | orderId=%s",
            ts.ticker, c_type, c_symbol, order_id,
        )

        # Poll for fill (up to 15s)
        if order_id:
            for _ in range(5):
                time.sleep(3)
                try:
                    status = client.get_order(order_id)
                    state  = (status.get("status") or status.get("orderStatus") or "").upper()
                    result["status"] = state
                    if state in ("FILLED", "CANCELLED", "REJECTED"):
                        break
                except Exception:
                    break

        _log_decision(ts.ticker, decision, account_balance, "order_placed", order, edgar_context=edgar_context)

    except Exception as exc:
        result["action"] = "error"
        result["reason"] = str(exc)
        logger.error("Agent order failed for %s: %s", ts.ticker, exc)
        _log_decision(ts.ticker, decision, account_balance, "order_error", edgar_context=edgar_context)

    return result


# ---------------------------------------------------------------------------
# Core scan + trade loop
# ---------------------------------------------------------------------------

def run_pre_market_scan(
    scanner: SentimentScanner | None = None,
    client: PublicClient | None = None,
    risk_manager: RiskManager | None = None,
) -> dict:
    return _run_scan("Pre-Market (08:00 ET)", scanner, client, risk_manager)


def run_market_open_scan(
    scanner: SentimentScanner | None = None,
    client: PublicClient | None = None,
    risk_manager: RiskManager | None = None,
) -> dict:
    return _run_scan("Market Open (09:35 ET)", scanner, client, risk_manager)


def run_midday_scan(
    scanner: SentimentScanner | None = None,
    client: PublicClient | None = None,
    risk_manager: RiskManager | None = None,
) -> dict:
    return _run_scan("Midday (12:00 ET)", scanner, client, risk_manager)


# ---------------------------------------------------------------------------
# EDGAR 8-K dedup helpers
# ---------------------------------------------------------------------------

def _edgar_already_processed(accession_number: str) -> bool:
    """Return True if this EDGAR accession was already acted on today."""
    try:
        resp = _dynamodb().get_item(
            TableName=_LOG_TABLE,
            Key={"trade_id": {"S": f"edgar_{accession_number}"}},
        )
        return "Item" in resp
    except Exception:
        return False   # on DB error, allow processing (err on side of action)


def _mark_edgar_processed(accession_number: str) -> None:
    """Record this accession so the same filing isn't traded twice."""
    try:
        _dynamodb().put_item(
            TableName=_LOG_TABLE,
            Item={
                "trade_id":  {"S": f"edgar_{accession_number}"},
                "timestamp": {"S": datetime.now(timezone.utc).isoformat()},
                "type":      {"S": "edgar_processed"},
            },
        )
    except Exception as exc:
        logger.warning("EDGAR dedup mark failed: %s", exc)


# ---------------------------------------------------------------------------
# Standalone EDGAR scan — runs every 5 min, 8am–4pm ET
# ---------------------------------------------------------------------------

def run_edgar_scan() -> dict:
    """
    Dedicated EDGAR 8-K monitor (triggered every 5 min by EventBridge).

    Checks for new high-impact 8-K filings on watchlist tickers.
    For priority signals (score >= 0.8) not yet processed today:
      → Runs through Claude agent → places trade if confidence warrants.
    DynamoDB dedup prevents double-trading the same filing.
    """
    logger.info("=== EDGAR 8-K scan starting ===")

    from sentiment.edgar_monitor import scan_watchlist as _edgar_scan
    from sentiment.scanner import TickerSentiment as _TS

    client  = PublicClient()
    account_balance = {"cash_balance": 0.0, "buying_power": 0.0, "portfolio_value": 0.0}
    positions: list[dict] = []
    try:
        account_balance, positions = client.get_account_and_positions()
    except Exception as exc:
        logger.warning("EDGAR scan: account fetch failed: %s", exc)

    try:
        edgar_signals = _edgar_scan(settings.WATCHLIST)
    except Exception as exc:
        logger.error("EDGAR scan: edgar_monitor failed: %s", exc)
        return {"window": "edgar_scan", "filings_found": 0, "acted_on": 0, "error": str(exc)}

    if not edgar_signals:
        logger.info("EDGAR scan: no 8-K filings found for watchlist today")
        return {"window": "edgar_scan", "filings_found": 0, "acted_on": 0}

    high_impact = [s for s in edgar_signals.values() if s["priority"]]
    logger.info(
        "EDGAR scan: %d filings found, %d high-impact",
        len(edgar_signals), len(high_impact),
    )

    buying_power = account_balance.get("cash_balance", 0.0)
    risk_manager = RiskManager(account_size=buying_power)
    trade_results: list[dict] = []

    for sig in high_impact:
        ticker = sig["ticker"]
        accno  = sig["accession_number"]

        if _edgar_already_processed(accno):
            logger.info("EDGAR: %s (%s) already processed — skipping", ticker, accno)
            continue

        if not _market_is_open() or buying_power <= 0:
            # Market closed or no funds — log but don't trade
            if settings.TRADE_DEBUG:
                _publish_sns(
                    f"EDGAR 8-K signal: {ticker} | {sig['catalyst']} (score {sig['score']:.1f})\n"
                    f"Items: {sig['items']}\nMarket open: {_market_is_open()}",
                    subject=f"[TraderBot EDGAR] {ticker} — {sig['catalyst']} (market closed)",
                )
            _mark_edgar_processed(accno)
            continue

        # Build synthetic TickerSentiment and run through agent
        synthetic_score = 0.55 if sig["direction"] == "bullish" else -0.55
        ts = _TS(
            ticker=ticker,
            score=synthetic_score,
            price_score=0.0,
            macro_score=0.0,
            polygon_score=0.0,
            signal=sig["direction"],
        )

        tr = _execute_with_agent(ts, client, risk_manager, positions, account_balance, edgar_context=sig)
        trade_results.append(tr)
        _mark_edgar_processed(accno)

        if tr["action"] == "order_placed":
            try:
                positions = client.get_positions()
            except Exception:
                pass

    # EDGAR SNS summary
    if edgar_signals:
        lines = [
            "EDGAR 8-K Monitor",
            f"Filings found: {len(edgar_signals)}  |  High-impact: {len(high_impact)}",
            "",
        ]
        for ticker, sig in edgar_signals.items():
            icon = "!" if sig["priority"] else " "
            lines.append(
                f" {icon} {ticker}: {sig['catalyst']} (score {sig['score']:.1f}) "
                f"items={sig['items']} direction={sig['direction']}"
            )
        if trade_results:
            lines += ["", "EDGAR TRADES:"]
            for tr in trade_results:
                lines.append(f"  {tr['ticker']}: {tr['action']} — {tr.get('reason','')}")
        try:
            subject = (
                f"[TraderBot] EDGAR: {len(high_impact)} high-impact 8-K"
                + ("s" if len(high_impact) != 1 else "")
            )
            _publish_sns("\n".join(lines), subject=subject)
        except Exception as exc:
            logger.warning("EDGAR SNS failed: %s", exc)

    acted = sum(1 for tr in trade_results if tr["action"] == "order_placed")
    return {
        "window":        "edgar_scan",
        "filings_found": len(edgar_signals),
        "high_impact":   len(high_impact),
        "acted_on":      acted,
    }


def _safe_float(val) -> float:
    """Convert a value to float safely — handles str, int, float, dict, and None."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0
    return 0.0  # dicts, None, or anything else


def run_end_of_day_scan(
    client: PublicClient | None = None,
    risk_manager: RiskManager | None = None,
) -> dict:
    """
    End-of-day review at 15:45 ET (15 min before market close).

    1. Fetches all open positions and current prices.
    2. Auto-closes any position down more than STOP_LOSS_PCT (7%).
    3. Sends a portfolio P&L summary email regardless of whether anything was closed.
    """
    window = "End of Day (15:45 ET)"
    logger.info("=== %s review starting ===", window)

    client = client or PublicClient()

    buying_power   = 0.0   # Live from Public.com API — do not hardcode
    portfolio_value = 0.0
    try:
        account_balance, _ = client.get_account_and_positions()
        buying_power    = account_balance.get("cash_balance", 0.0)
        portfolio_value = account_balance.get("portfolio_value", buying_power)
        logger.info("Buying power: $%.2f | Portfolio: $%.2f", buying_power, portfolio_value)
    except Exception as exc:
        logger.warning("Could not fetch buying power: %s", exc)

    if risk_manager is None:
        risk_manager = RiskManager(account_size=buying_power)

    positions = []
    try:
        positions = client.get_positions()
        logger.info("Open positions: %d", len(positions))
    except Exception as exc:
        logger.warning("Could not fetch positions: %s", exc)

    if not positions:
        msg = _build_eod_message(window, [], buying_power)
        _publish_sns(msg, subject=f"[TraderBot] {window} — No open positions")
        return {"window": window, "positions": 0, "closed": 0}

    # Fetch current prices for all held tickers in one call
    held_symbols = []
    for p in positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        if sym:
            held_symbols.append(sym)

    price_map: dict[str, float] = {}
    try:
        resp = client.get_quotes(held_symbols)
        for q in resp.get("quotes", []):
            sym = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
            raw = q.get("last") or q.get("lastPrice") or q.get("price")
            if sym and raw:
                price_map[sym] = float(raw)
    except Exception as exc:
        logger.warning("EOD quotes failed: %s", exc)

    # Log first position structure once so we can confirm field names
    if positions:
        logger.info("EOD position sample keys: %s", list(positions[0].keys()))

    # PDT guard: don't stop-loss close positions opened today (would create a day-trade round trip)
    today_buys = _get_today_buy_symbols(portfolio_value) or set()   # empty set = no buys logged = safe to close all

    # Evaluate each position; auto-close if stop-loss hit
    position_reviews: list[dict] = []
    for p in positions:
        sym = (p.get("instrument", {}).get("symbol") or p.get("symbol") or "").upper()
        if not sym:
            continue

        qty           = _safe_float(p.get("quantity") or p.get("shares"))
        current_price = price_map.get(sym, 0.0)

        # Public.com may return avg cost under several field names, sometimes as nested dicts
        cost_basis = p.get("costBasis")
        cost_basis_unit = (
            _safe_float(cost_basis.get("unitCost"))
            if isinstance(cost_basis, dict) else _safe_float(cost_basis)
        )
        avg_price = (
            _safe_float(p.get("averagePrice"))
            or _safe_float(p.get("avgCostPerShare"))
            or cost_basis_unit
            or _safe_float(p.get("averageCost"))
        )

        pnl_pct = ((current_price - avg_price) / avg_price) if avg_price > 0 and current_price > 0 else None
        pnl_usd = ((current_price - avg_price) * qty)       if avg_price > 0 and current_price > 0 and qty > 0 else None

        review: dict = {
            "symbol":        sym,
            "qty":           qty,
            "current_price": current_price,
            "avg_price":     avg_price,
            "pnl_pct":       pnl_pct,
            "pnl_usd":       pnl_usd,
            "action":        "hold",
            "close_reason":  "",
            "order_id":      None,
        }

        # Auto-close if down more than the stop-loss threshold
        import re as _re
        _m = _re.match(r'^([A-Z]+)', sym)
        _base = _m.group(1) if _m else sym
        if _base in today_buys:
            review["close_reason"] = f"PDT guard: opened today — stop-loss deferred to tomorrow"
            position_reviews.append(review)
            logger.info("PDT guard: skipping stop-loss on %s — bought today", sym)
            continue

        if pnl_pct is not None and pnl_pct <= -settings.STOP_LOSS_PCT:
            review["close_reason"] = (
                f"Stop-loss hit ({pnl_pct:+.1%} ≤ -{settings.STOP_LOSS_PCT:.0%})"
            )
            if qty > 0:
                try:
                    order    = client.place_order(
                        symbol=sym, side="SELL", order_type="MARKET",
                        quantity=str(qty),
                    )
                    order_id = order.get("orderId", "")
                    review["action"]   = "closed"
                    review["order_id"] = order_id
                    if pnl_usd:
                        risk_manager.record_loss(abs(pnl_usd))
                    logger.info(
                        "EOD stop-loss close: SELL %s ×%.4f | orderId=%s | P&L=%.2f",
                        sym, qty, order_id, pnl_usd or 0,
                    )
                except Exception as exc:
                    review["action"]       = "close_failed"
                    review["close_reason"] += f" — order failed: {exc}"
                    logger.error("EOD close failed for %s: %s", sym, exc)

        position_reviews.append(review)

    n_closed = sum(1 for r in position_reviews if r["action"] == "closed")

    # Pull today's logs and generate a Claude narrative recap
    log_text  = _fetch_todays_log_events()
    narrative = _generate_eod_narrative(position_reviews, buying_power, log_text)

    msg = _build_eod_message(window, position_reviews, buying_power, narrative)
    subject = (
        f"[TraderBot] {n_closed} position(s) closed (stop-loss) — {window}"
        if n_closed else
        f"[TraderBot] {window} — {len(position_reviews)} position(s) open"
    )
    _publish_sns(msg, subject=subject)

    return {"window": window, "positions": len(position_reviews), "closed": n_closed}


def _run_scan(
    window: str,
    scanner: SentimentScanner | None,
    client: PublicClient | None,
    risk_manager: RiskManager | None,
) -> dict:
    logger.info("=== %s scan starting ===", window)

    client = client or PublicClient()

    # Live account state + positions — single portfolio API call
    account_balance = {"cash_balance": 0.0, "buying_power": 0.0, "portfolio_value": 0.0}
    positions = []
    try:
        account_balance, positions = client.get_account_and_positions()
        logger.info("Open positions: %d", len(positions))
    except Exception as exc:
        logger.warning("Could not fetch account state: %s — trading disabled this scan", exc)

    buying_power = account_balance["cash_balance"]   # Live from Public.com API — do not hardcode

    if buying_power <= 0:
        logger.error("Cash balance is $0 — skipping trades (API may be down)")

    if risk_manager is None:
        risk_manager = RiskManager(account_size=buying_power)

    # Pass the authenticated client into the scanner so price signals
    # reuse the same Public.com session
    scanner = scanner or SentimentScanner(broker_client=client)
    all_results = scanner.scan()

    # EDGAR 8-K scan — runs alongside regular sentiment scan
    edgar_signals: dict[str, dict] = {}
    edgar_stats = {"scanned": len(settings.WATCHLIST), "high_impact": 0, "sent_to_claude": 0}
    try:
        from sentiment.edgar_monitor import scan_watchlist as _edgar_scan
        edgar_signals                = _edgar_scan(settings.WATCHLIST)
        edgar_stats["high_impact"]   = sum(1 for s in edgar_signals.values() if s["priority"])
        if edgar_signals:
            logger.info(
                "EDGAR: %d watchlist filings found (%d high-impact)",
                len(edgar_signals), edgar_stats["high_impact"],
            )
    except Exception as exc:
        logger.warning("EDGAR scan failed (non-fatal): %s", exc)

    # Pull macro summary for the email
    macro_summary = ""
    try:
        from sentiment.news_macro import fetch_macro_headlines, score_macro_sentiment
        headlines = fetch_macro_headlines()
        if headlines:
            scored = score_macro_sentiment(headlines)
            macro_summary = scored.get("summary", "")
    except Exception:
        pass

    # Identify strong signals
    strong = [
        ts for ts in all_results
        if ts.score >= settings.SENTIMENT_BUY_THRESHOLD
        or ts.score <= settings.SENTIMENT_SELL_THRESHOLD
    ]
    logger.info("Strong signals: %d", len(strong))

    market_open = _market_is_open()
    trade_results: list[dict] = []

    # Intra-day rotation: close reversed/weak positions before buying new ones
    if market_open and positions:
        score_map = {ts.ticker: ts.score for ts in all_results}
        portfolio_value = account_balance.get("portfolio_value", 0.0)
        rotation_closes = _evaluate_intraday_rotation(positions, score_map, strong, client, portfolio_value)
        for rc in rotation_closes:
            cr = _close_intraday(rc["position"], client, rc["reason"])
            trade_results.append(cr)
        if any(r["action"] == "closed" for r in trade_results):
            try:
                positions = client.get_positions()
                account_balance, _ = client.get_account_and_positions()
                buying_power = account_balance.get("cash_balance", buying_power)
            except Exception:
                pass

    if market_open and strong and buying_power > 0:
        for ts in strong:
            edgar_ctx = edgar_signals.get(ts.ticker)
            if edgar_ctx:
                edgar_stats["sent_to_claude"] += 1
            tr = _execute_with_agent(ts, client, risk_manager, positions, account_balance, edgar_context=edgar_ctx)
            trade_results.append(tr)
            # Re-fetch positions after each fill so duplicate guard stays current
            if tr["action"] == "order_placed":
                try:
                    positions = client.get_positions()
                except Exception:
                    pass

    # EDGAR-only priority signals: watchlist tickers with a high-impact 8-K that
    # the sentiment scanner didn't flag as a strong signal on its own
    if market_open and buying_power > 0 and edgar_signals:
        from sentiment.scanner import TickerSentiment as _TS
        strong_tickers = {ts.ticker for ts in strong}
        for ticker, sig in edgar_signals.items():
            if ticker in strong_tickers or not sig["priority"]:
                continue
            synthetic_score = 0.55 if sig["direction"] == "bullish" else -0.55
            ts_edgar = _TS(
                ticker=ticker,
                score=synthetic_score,
                price_score=0.0,
                macro_score=0.0,
                polygon_score=0.0,
                signal=sig["direction"],
            )
            edgar_stats["sent_to_claude"] += 1
            tr = _execute_with_agent(
                ts_edgar, client, risk_manager, positions, account_balance,
                edgar_context=sig,
            )
            trade_results.append(tr)
            if tr["action"] == "order_placed":
                try:
                    positions = client.get_positions()
                except Exception:
                    pass
    elif strong and not market_open:
        # Market closed — record signals but don't trade
        for ts in strong:
            trade_results.append({
                "ticker":   ts.ticker,
                "signal":   ts.signal,
                "score":    ts.score,
                "action":   "skipped",
                "reason":   "Market closed",
                "order_id": None,
                "status":   None,
                "amount":   None,
            })
    elif strong and buying_power <= 0:
        for ts in strong:
            trade_results.append({
                "ticker":   ts.ticker,
                "signal":   ts.signal,
                "score":    ts.score,
                "action":   "skipped",
                "reason":   "Cash balance unavailable — API may be down",
                "order_id": None,
                "status":   None,
                "amount":   None,
            })

    message = _build_alert_message(
        window        = window,
        scan_results  = all_results,
        trade_results = trade_results,
        positions_before = positions,
        buying_power  = buying_power,
        risk_manager  = risk_manager,
        market_open   = market_open,
        macro_summary = macro_summary,
        edgar_stats   = edgar_stats if edgar_signals else None,
    )
    logger.info(message)

    try:
        subject = f"[TraderBot] {window}"
        if trade_results and any(r["action"] == "order_placed" for r in trade_results):
            n = sum(1 for r in trade_results if r["action"] == "order_placed")
            subject = f"[TraderBot] {n} trade{'s' if n > 1 else ''} placed — {window}"
        _publish_sns(message, subject=subject)
    except Exception as exc:
        logger.error("SNS publish failed: %s", exc)

    orders_placed = [r for r in trade_results if r["action"] == "order_placed"]
    return {
        "window":         window,
        "signals_found":  len(strong),
        "orders_placed":  len(orders_placed),
        "market_open":    market_open,
        "signals": [{"ticker": s.ticker, "signal": s.signal, "score": s.score}
                    for s in strong],
        "trades":  [{"ticker": r["ticker"], "amount": r["amount"], "order_id": r["order_id"]}
                    for r in orders_placed],
    }


# ---------------------------------------------------------------------------
# Local scheduler entrypoint
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler = BlockingScheduler(timezone="America/New_York")
    scheduler.add_job(
        run_pre_market_scan,
        trigger=CronTrigger(
            hour=settings.PRE_MARKET_HOUR,
            minute=settings.PRE_MARKET_MINUTE,
            timezone="America/New_York",
        ),
        id="pre_market", replace_existing=True,
    )
    scheduler.add_job(
        run_market_open_scan,
        trigger=CronTrigger(
            hour=settings.MARKET_OPEN_HOUR,
            minute=settings.MARKET_OPEN_MINUTE,
            timezone="America/New_York",
        ),
        id="market_open", replace_existing=True,
    )
    scheduler.add_job(
        run_midday_scan,
        trigger=CronTrigger(
            hour=settings.MIDDAY_HOUR,
            minute=settings.MIDDAY_MINUTE,
            timezone="America/New_York",
        ),
        id="midday", replace_existing=True,
    )
    scheduler.add_job(
        run_end_of_day_scan,
        trigger=CronTrigger(
            hour=settings.EOD_HOUR,
            minute=settings.EOD_MINUTE,
            timezone="America/New_York",
        ),
        id="eod", replace_existing=True,
    )

    from scheduler.suggestions import run_suggestions_scan  # noqa: PLC0415
    scheduler.add_job(
        run_suggestions_scan,
        trigger=CronTrigger(
            hour=settings.EVENING_HOUR,
            minute=settings.EVENING_MINUTE,
            timezone="America/New_York",
        ),
        id="evening_suggestions", replace_existing=True,
    )

    logger.info(
        "Scheduler started — %02d:%02d ET, %02d:%02d ET, %02d:%02d ET, %02d:%02d ET, %02d:%02d ET",
        settings.PRE_MARKET_HOUR, settings.PRE_MARKET_MINUTE,
        settings.MARKET_OPEN_HOUR, settings.MARKET_OPEN_MINUTE,
        settings.MIDDAY_HOUR, settings.MIDDAY_MINUTE,
        settings.EOD_HOUR, settings.EOD_MINUTE,
        settings.EVENING_HOUR, settings.EVENING_MINUTE,
    )
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
