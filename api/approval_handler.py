"""
Lambda Function URL handler for one-click trade approvals.

Invoked when the user clicks an approval link from the evening suggestion email.
The URL format is:
    https://{function-url}/approve?ticker=AAPL&dollars=3.00&expires=1234567890&token=abc...

Security model:
  - Token is HMAC-SHA256(SECRET, f"{ticker}:{dollars:.2f}:{expires_ts}")
  - Expiry is enforced via unix timestamp in the URL (no database required)
  - SECRET is stored in AWS Secrets Manager and injected via SUGGESTION_TOKEN_SECRET env var

Response: HTML page (rendered in the browser when the user clicks the link).
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

from config.settings import settings
from broker.public_client import PublicClient

_OPTIONS_DTE_MIN     = 14
_OPTIONS_DTE_MAX     = 45
_OPTIONS_DRIFT_BLOCK = 0.15   # block if price moved >15% against the trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def _verify_sell_token(ticker: str, qty: float, expires_ts: int, token: str) -> bool:
    """Return True if the sell HMAC token is valid and not expired."""
    if not settings.SUGGESTION_TOKEN_SECRET:
        return False
    if time.time() > expires_ts:
        return False
    payload  = f"sell:{ticker}:{qty:.4f}:{expires_ts}"
    expected = hmac.new(
        settings.SUGGESTION_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def _verify_token(ticker: str, dollars: float, expires_ts: int, token: str) -> bool:
    """Return True if the HMAC token is valid and not expired."""
    if not settings.SUGGESTION_TOKEN_SECRET:
        logger.error("SUGGESTION_TOKEN_SECRET not set — cannot validate token")
        return False

    if time.time() > expires_ts:
        return False

    payload = f"{ticker}:{dollars:.2f}:{expires_ts}"
    expected = hmac.new(
        settings.SUGGESTION_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, token)


def _verify_batch_token(batch: str, expires_ts: int, token: str) -> bool:
    """Return True if the batch HMAC token is valid and not expired."""
    if not settings.SUGGESTION_TOKEN_SECRET:
        return False
    if time.time() > expires_ts:
        return False
    payload = f"batch:{batch}:{expires_ts}"
    expected = hmac.new(
        settings.SUGGESTION_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# HTML responses
# ---------------------------------------------------------------------------

_HTML_STYLE = """
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 520px;
          margin: 80px auto; padding: 24px; color: #111; }
  h2 { margin-bottom: 8px; }
  .ok { color: #1a7f1a; }
  .detail { background: #f5f5f5; border-radius: 8px; padding: 16px; margin: 16px 0; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  td { padding: 4px 8px; }
  td:first-child { font-weight: bold; }
  .footer { color: #888; font-size: 0.8em; margin-top: 32px; }
"""


def _html_success(ticker: str, dollars: float, order_id: str, current_price: float = 0.0) -> str:
    price_note = f"<br>Current price: <strong>${current_price:.2f}</strong>" if current_price else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trade Approved — TraderBot</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h2 class="ok">&#10003; Trade Approved</h2>
  <div class="detail">
    <strong>Buying ${dollars:.2f} of {ticker}</strong>{price_note}<br>
    Order ID: <code>{order_id}</code>
  </div>
  <p>Market order placed. Fills immediately if the market is open,
     or at the next market open if after hours.</p>
  <p>A confirmation email has been sent.</p>
  <p class="footer">TraderBot</p>
</body>
</html>"""


def _html_batch_success(results: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{r['ticker']}</td><td>${r['dollars']:.2f}</td>"
        f"<td>{'&#10003; ' + r['order_id'] if r.get('order_id') else '&#10005; ' + r.get('error','failed')}</td></tr>"
        for r in results
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>All Trades Approved — TraderBot</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h2 class="ok">&#10003; All Trades Approved</h2>
  <div class="detail">
    <table>
      <tr><td><em>Ticker</em></td><td><em>Amount</em></td><td><em>Status</em></td></tr>
      {rows}
    </table>
  </div>
  <p>Orders fill immediately if the market is open, or at the next market open if after hours.</p>
  <p>A confirmation email has been sent.</p>
  <p class="footer">TraderBot</p>
</body>
</html>"""


def _html_error(reason: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trade Failed — TraderBot</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 480px;
            margin: 80px auto; padding: 24px; color: #111; }}
    h2 {{ color: #c0392b; margin-bottom: 8px; }}
    .reason {{ background: #fff0f0; border-radius: 8px; padding: 16px; margin: 16px 0; }}
    .footer {{ color: #888; font-size: 0.8em; margin-top: 32px; }}
  </style>
</head>
<body>
  <h2>&#10005; Trade Not Placed</h2>
  <div class="reason">{reason}</div>
  <p>No order was placed. Check your email for a new suggestion or
     request one manually.</p>
  <p class="footer">TraderBot</p>
</body>
</html>"""


def _html_response(body: str, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body,
    }


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


def _json_response(data, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_CORS_HEADERS},
        "body": json.dumps(data),
    }


def _cors_preflight() -> dict:
    return {"statusCode": 204, "headers": _CORS_HEADERS, "body": ""}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_current_price(ticker: str) -> float:
    """Fetch current price from Public.com. Returns 0.0 on failure."""
    try:
        client = PublicClient()
        quotes = client.get_quotes([ticker])
        for q in (quotes.get("quotes", []) if isinstance(quotes, dict) else quotes):
            sym = (q.get("instrument", {}).get("symbol") or q.get("symbol") or "").upper()
            if sym == ticker:
                for field in ("last", "lastPrice", "ask", "price"):
                    v = q.get(field)
                    if v:
                        return float(v)
    except Exception as exc:
        logger.debug("Could not fetch price for %s: %s", ticker, exc)
    return 0.0


def _send_confirmation_sns(lines: list[str], subject: str) -> None:
    try:
        import boto3
        from datetime import datetime, timezone
        arn_parts = settings.SNS_TOPIC_ARN.split(":")
        sns_region = arn_parts[3] if len(arn_parts) >= 4 else settings.AWS_REGION
        now_str = datetime.now(timezone.utc).strftime("%a %b %d, %Y  %I:%M %p UTC")
        boto3.client("sns", region_name=sns_region).publish(
            TopicArn=settings.SNS_TOPIC_ARN,
            Subject=subject,
            Message=f"Trade confirmed — {now_str}\n\n" + "\n".join(lines),
        )
    except Exception as exc:
        logger.warning("Confirmation SNS failed: %s", exc)


# ---------------------------------------------------------------------------
# Batch approval handler
# ---------------------------------------------------------------------------

def _handle_batch_approval(params: dict) -> dict:
    """
    Approve all suggestions in one click.

    URL format: /approve?batch=AAPL:3.00,ITA:5.00,NVDA:2.00&expires=...&token=...
    """
    batch      = params.get("batch", "")
    expires_str = params.get("expires", "")
    token      = params.get("token", "")

    if not all([batch, expires_str, token]):
        return _html_response(_html_error("Invalid or incomplete batch approval link."), status=400)

    try:
        expires_ts = int(expires_str)
    except (ValueError, TypeError):
        return _html_response(_html_error("Malformed batch approval link."), status=400)

    if not _verify_batch_token(batch, expires_ts, token):
        if time.time() > expires_ts:
            reason = (
                f"This approval link has expired. "
                f"Links are valid for {settings.SUGGESTION_EXPIRY_HOURS} hours after they are sent."
            )
        else:
            reason = "Invalid batch approval link."
        return _html_response(_html_error(reason), status=403)

    # Parse batch: "AAPL:3.00,ITA:5.00,NVDA:2.00"
    trades = []
    for part in batch.split(","):
        try:
            ticker, dollars_str = part.strip().split(":")
            trades.append({"ticker": ticker.upper(), "dollars": float(dollars_str)})
        except (ValueError, AttributeError):
            logger.warning("Skipping malformed batch part: %s", part)

    if not trades:
        return _html_response(_html_error("No valid trades found in batch link."), status=400)

    client = PublicClient()
    results = []
    for trade in trades:
        ticker  = trade["ticker"]
        dollars = trade["dollars"]
        logger.info("Batch approval: placing BUY $%.2f %s", dollars, ticker)
        try:
            order    = client.place_order(symbol=ticker, side="BUY", order_type="MARKET", amount=f"{dollars:.2f}")
            order_id = order.get("orderId", "unknown")
            results.append({"ticker": ticker, "dollars": dollars, "order_id": order_id})
            logger.info("Batch order placed: %s $%.2f | orderId=%s", ticker, dollars, order_id)
        except Exception as exc:
            logger.error("Batch order failed for %s: %s", ticker, exc)
            results.append({"ticker": ticker, "dollars": dollars, "error": str(exc)})

    placed = [r for r in results if r.get("order_id")]
    conf_lines = [
        f"Batch approval — {len(placed)}/{len(results)} orders placed",
        "",
    ] + [
        f"  {r['ticker']}: ${r['dollars']:.2f} — Order {r.get('order_id', 'FAILED: ' + r.get('error',''))}"
        for r in results
    ] + ["", "Orders fill at market open if placed after hours."]

    _send_confirmation_sns(conf_lines, f"[TraderBot] Batch confirmed: {len(placed)} trades placed")
    return _html_response(_html_batch_success(results))


# ---------------------------------------------------------------------------
# Sell approval handler
# ---------------------------------------------------------------------------

def _html_sell_success(ticker: str, qty: float, order_id: str) -> str:
    import re
    qty_label = f"{int(qty)} contract{'s' if int(qty) != 1 else ''}" if re.search(r'\d', ticker) else f"{qty} shares"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sell Approved — TraderBot</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h2 class="ok">&#10003; Sell Approved</h2>
  <div class="detail">
    <strong>Sold {qty_label} of {ticker}</strong><br>
    Order ID: <code>{order_id}</code>
  </div>
  <p>Market sell order placed. Fills immediately if the market is open.</p>
  <p class="footer">TraderBot</p>
</body>
</html>"""


def _handle_sell_approval(params: dict) -> dict:
    """Execute a sell when Rob clicks the approval link from email."""
    import re
    ticker  = (params.get("ticker") or "").upper().strip()
    qty_str = params.get("qty", "")
    expires_str = params.get("expires", "")
    token   = params.get("token", "")

    if not all([ticker, qty_str, expires_str, token]):
        return _html_response(_html_error("Invalid or incomplete sell approval link."), status=400)

    try:
        qty        = float(qty_str)
        expires_ts = int(expires_str)
    except (ValueError, TypeError):
        return _html_response(_html_error("Malformed sell approval link."), status=400)

    if not _verify_sell_token(ticker, qty, expires_ts, token):
        reason = (
            "This sell approval link has expired."
            if time.time() > expires_ts
            else "Invalid sell approval link."
        )
        return _html_response(_html_error(reason), status=403)

    # PDT guard: block same-day sells to prevent Pattern Day Trader violations
    from scheduler.jobs import _execute_close, _get_today_buy_symbols
    today_buys = _get_today_buy_symbols(0.0)
    if today_buys is None:
        return _html_response(
            _html_error(
                "Sell blocked: trade history is temporarily unavailable (DynamoDB unreachable). "
                "This is a safety measure to prevent Pattern Day Trader rule violations. "
                "Try again in a few minutes or sell manually in your brokerage app."
            ),
            status=503,
        )
    if ticker in today_buys:
        return _html_response(
            _html_error(
                f"Sell blocked for {ticker}: this position was opened today. "
                "Selling a position on the same day it was bought counts as a round-trip day trade. "
                "Accounts under $25,000 are limited to 3 round-trips in any rolling 5-day window "
                "before a 90-day trading freeze is imposed. This sell will be available tomorrow."
            ),
            status=403,
        )

    logger.info("Sell approved — placing SELL %s ×%s", ticker, qty)
    try:
        client = PublicClient()
        result = _execute_close(ticker, qty, client, reason="manual approval")
        order_id = result.get("order_id") or "unknown"
        if result.get("action") == "error":
            raise RuntimeError(result["reason"])
    except Exception as exc:
        logger.error("Sell approval execution failed for %s: %s", ticker, exc)
        return _html_response(_html_error(f"Sell order failed: {exc}"), status=500)

    _send_confirmation_sns(
        [f"SOLD {qty} of {ticker}", f"Order ID: {order_id}", "Manual approval via email link."],
        f"[TraderBot] Confirmed SELL: {ticker}",
    )
    return _html_response(_html_sell_success(ticker, qty, order_id))


# ---------------------------------------------------------------------------
# Options approval handler
# ---------------------------------------------------------------------------

def _verify_options_token(opt_type: str, ticker: str, signal_price: float,
                           size_usd: float, expires_ts: int, token: str) -> bool:
    if not settings.SUGGESTION_TOKEN_SECRET:
        return False
    if time.time() > expires_ts:
        return False
    payload  = f"options:{opt_type}:{ticker}:{signal_price:.4f}:{size_usd:.2f}:{expires_ts}"
    expected = hmac.new(
        settings.SUGGESTION_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def _html_options_success(ticker: str, opt_type: str, contract_info: str,
                           order_id: str, signal_price: float,
                           current_price: float, drift_pct: float) -> str:
    type_label  = "Call" if opt_type == "call" else "Put Spread"
    price_delta = f"${signal_price:.2f} → ${current_price:.2f} ({drift_pct:+.1%})"
    warn_row    = ""
    if abs(drift_pct) >= 0.05:
        warn_row = f"<tr><td>⚠ Price drift</td><td>{price_delta}</td></tr>"
    else:
        warn_row = f"<tr><td>Signal → Now</td><td>{price_delta}</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Options Trade Placed — TraderBot</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h2 class="ok">&#10003; {type_label} Order Placed</h2>
  <div class="detail">
    <table>
      <tr><td>Ticker</td><td><strong>{ticker}</strong></td></tr>
      <tr><td>Contract</td><td>{contract_info}</td></tr>
      {warn_row}
      <tr><td>Order ID</td><td><code>{order_id}</code></td></tr>
    </table>
  </div>
  <p>Limit order submitted. Check Public.com for fill status.</p>
  <p class="footer">TraderBot</p>
</body>
</html>"""


def _html_options_stale(ticker: str, opt_type: str, signal_price: float,
                         current_price: float, drift_pct: float) -> str:
    direction  = "fell" if drift_pct < 0 else "rose"
    type_label = "call" if opt_type == "call" else "put spread"
    reason = (
        f"{ticker} {direction} {abs(drift_pct):.1%} since the signal fired "
        f"(${signal_price:.2f} → ${current_price:.2f}). "
        f"A {type_label} no longer has a favorable risk/reward at this price. "
        f"No order was placed."
    )
    return _html_error(reason)


def _handle_options_approval(params: dict) -> dict:
    """Re-evaluate price drift and place an options order when the approval link is clicked."""
    opt_type    = (params.get("opt_type") or "").lower().strip()
    ticker      = (params.get("ticker") or "").upper().strip()
    expires_str = params.get("expires", "")
    token       = params.get("token", "")
    signal_price_str = params.get("signal_price", "0")
    size_str    = params.get("size", "0")

    if not all([opt_type, ticker, expires_str, token]):
        return _html_response(_html_error("Invalid or incomplete options approval link."), status=400)

    if opt_type not in ("call", "put_spread"):
        return _html_response(_html_error(f"Unknown option type: {opt_type}"), status=400)

    try:
        expires_ts   = int(expires_str)
        signal_price = float(signal_price_str)
        size_usd     = float(size_str)
    except (ValueError, TypeError):
        return _html_response(_html_error("Malformed options approval link."), status=400)

    if not _verify_options_token(opt_type, ticker, signal_price, size_usd, expires_ts, token):
        reason = (
            "This options approval link has expired."
            if time.time() > expires_ts
            else "Invalid options approval link."
        )
        return _html_response(_html_error(reason), status=403)

    # Re-evaluate: fetch current price and check drift
    current_price = _fetch_current_price(ticker)
    if current_price <= 0:
        current_price = signal_price   # fallback — proceed without drift check

    drift_pct = (current_price - signal_price) / signal_price if signal_price > 0 else 0.0
    adverse   = (drift_pct < 0) if opt_type == "call" else (drift_pct > 0)
    if adverse and abs(drift_pct) > _OPTIONS_DRIFT_BLOCK:
        logger.info("Options approval blocked — %s drift=%.1f%% exceeds threshold", ticker, drift_pct * 100)
        return _html_response(
            _html_options_stale(ticker, opt_type, signal_price, current_price, drift_pct)
        )

    client = PublicClient()

    # ---- CALL ----
    if opt_type == "call":
        expirations = client.get_option_expirations(ticker)
        if not expirations:
            return _html_response(_html_error(f"No option expirations available for {ticker}."))

        today      = datetime.now(timezone.utc).date()
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
            return _html_response(
                _html_error(f"No expiration in {_OPTIONS_DTE_MIN}–{_OPTIONS_DTE_MAX} DTE window for {ticker}.")
            )
        candidates.sort()
        expiration = candidates[0][1]

        chain = client.get_option_chain(ticker, expiration, option_type="CALL")
        if not chain:
            return _html_response(_html_error(f"Empty call chain for {ticker} exp {expiration}."))

        chain.sort(key=lambda c: abs(float(c.get("strikePrice", 0)) - current_price))

        chosen = None
        chosen_mid = 0.0
        for contract in chain[:10]:
            strike = float(contract.get("strikePrice", 0))
            if strike < current_price * 0.99:
                continue
            bid = float(contract.get("bid") or 0)
            ask = float(contract.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            if mid * 100 <= size_usd:
                chosen     = contract
                chosen_mid = mid
                break

        if not chosen:
            return _html_response(
                _html_error(f"No affordable call for {ticker} within ${size_usd:.0f} budget at current prices.")
            )

        opt_sym    = chosen.get("optionSymbol", "")
        strike_str = chosen.get("strikePrice", "")
        try:
            order    = client.place_options_order(
                option_symbol=opt_sym, side="BUY", quantity="1",
                order_type="LIMIT", limit_price=f"{chosen_mid:.2f}",
            )
            order_id = order.get("orderId", "unknown")
        except Exception as exc:
            logger.error("Options call order failed for %s: %s", ticker, exc)
            return _html_response(_html_error(f"Order placement failed: {exc}"), status=500)

        contract_info = f"1 CALL · {ticker} ${strike_str} exp {expiration} @ ${chosen_mid:.2f}/share"
        logger.info("Options approval placed CALL %s @ $%.2f | orderId=%s", opt_sym, chosen_mid, order_id)
        _send_confirmation_sns(
            [f"CALL placed: {ticker} ${strike_str} exp {expiration}",
             f"Limit @ ${chosen_mid:.2f}/share | Order: {order_id}",
             f"Signal @ ${signal_price:.2f} → Current ${current_price:.2f} ({drift_pct:+.1%})"],
            f"[TraderBot] Options placed: CALL {ticker}",
        )
        return _html_response(
            _html_options_success(ticker, opt_type, contract_info, order_id,
                                  signal_price, current_price, drift_pct)
        )

    # ---- PUT SPREAD ----
    expirations = client.get_option_expirations(ticker)
    if not expirations:
        return _html_response(_html_error(f"No option expirations available for {ticker}."))

    today      = datetime.now(timezone.utc).date()
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
        return _html_response(
            _html_error(f"No expiration in {_OPTIONS_DTE_MIN}–{_OPTIONS_DTE_MAX} DTE window for {ticker}.")
        )
    candidates.sort()
    expiration   = candidates[0][1]
    long_strike  = int(round(current_price * 0.995))
    short_strike = int(round(current_price * 0.975))

    legs = [
        PublicClient.make_option_leg(
            base_symbol=ticker, option_type="PUT",
            strike=f"{long_strike}.00", expiration=expiration,
            side="BUY", open_close="OPEN", ratio=1,
        ),
        PublicClient.make_option_leg(
            base_symbol=ticker, option_type="PUT",
            strike=f"{short_strike}.00", expiration=expiration,
            side="SELL", open_close="OPEN", ratio=1,
        ),
    ]

    try:
        pf        = client.preflight_multi_leg(legs=legs, quantity="1",
                                               order_type="LIMIT", limit_price="1.00")
        net_debit = float(pf.get("estimatedCost") or pf.get("buyingPowerRequirement") or 1.0)
    except Exception:
        net_debit = 1.0

    contracts = max(1, int(size_usd / max(net_debit * 100, 1.0)))
    try:
        order    = client.place_multi_leg(legs=legs, quantity=str(contracts),
                                          order_type="LIMIT", limit_price=f"{net_debit:.2f}")
        order_id = order.get("orderId", "unknown")
    except Exception as exc:
        logger.error("Put spread approval failed for %s: %s", ticker, exc)
        return _html_response(_html_error(f"Order placement failed: {exc}"), status=500)

    contract_info = (
        f"{contracts}× PUT SPREAD · {ticker} ${long_strike}/${short_strike} "
        f"exp {expiration} @ ${net_debit:.2f} net debit"
    )
    logger.info("Options approval placed PUT SPREAD %s | orderId=%s", ticker, order_id)
    _send_confirmation_sns(
        [f"PUT SPREAD placed: {ticker} ${long_strike}/${short_strike} exp {expiration}",
         f"{contracts} contract(s) @ ${net_debit:.2f} debit | Order: {order_id}",
         f"Signal @ ${signal_price:.2f} → Current ${current_price:.2f} ({drift_pct:+.1%})"],
        f"[TraderBot] Options placed: PUT SPREAD {ticker}",
    )
    return _html_response(
        _html_options_success(ticker, opt_type, contract_info, order_id,
                              signal_price, current_price, drift_pct)
    )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def _check_bearer(event: dict) -> bool:
    """Return True if the request carries a valid Bearer token."""
    auth = (event.get("headers") or {}).get("authorization", "")
    secret = settings.SUGGESTION_TOKEN_SECRET or ""
    return bool(secret) and auth == f"Bearer {secret}"


def _handle_balance(event: dict) -> dict:
    """Return account balance as JSON. Requires Bearer token."""
    if not _check_bearer(event):
        return _json_response({"error": "Unauthorized"}, status=401)

    try:
        balance = PublicClient().get_account_balance()
    except Exception as exc:
        logger.error("Failed to fetch balance: %s", exc)
        return _json_response({"error": str(exc)}, status=500)

    return _json_response(balance)


def _handle_orders(event: dict) -> dict:
    """Return open orders as JSON. Requires Bearer token matching SUGGESTION_TOKEN_SECRET."""
    if not _check_bearer(event):
        return _json_response({"error": "Unauthorized"}, status=401)

    try:
        orders = PublicClient().get_orders(status="open")
    except Exception as exc:
        logger.error("Failed to fetch open orders: %s", exc)
        return _json_response({"error": str(exc)}, status=500)

    return _json_response({"orders": orders})


def _handle_place_order(event: dict) -> dict:
    """Place a new order. Requires Bearer token."""
    if not _check_bearer(event):
        return _json_response({"error": "Unauthorized"}, status=401)

    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response({"error": "Invalid JSON body"}, status=400)

    symbol      = (body.get("symbol") or "").upper().strip()
    side        = (body.get("side") or "").upper().strip()
    order_type  = (body.get("orderType") or "MARKET").upper().strip()
    amount      = body.get("amount")       # dollar amount (market buy)
    quantity    = body.get("quantity")     # share quantity
    limit_price = body.get("limitPrice")

    if not symbol or side not in ("BUY", "SELL"):
        return _json_response({"error": "symbol and side (BUY/SELL) are required"}, status=400)
    if not amount and not quantity:
        return _json_response({"error": "Provide amount (dollars) or quantity (shares)"}, status=400)

    try:
        result = PublicClient().place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=str(amount) if amount else None,
            quantity=str(quantity) if quantity else None,
            limit_price=str(limit_price) if limit_price else None,
        )
    except Exception as exc:
        logger.error("Failed to place order %s %s: %s", side, symbol, exc)
        return _json_response({"error": str(exc)}, status=500)

    return _json_response(result, status=201)


def _handle_edit_order(event: dict, order_id: str) -> dict:
    """Edit a live order in place. Requires Bearer token."""
    if not _check_bearer(event):
        return _json_response({"error": "Unauthorized"}, status=401)

    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response({"error": "Invalid JSON body"}, status=400)

    quantity = body.get("quantity")
    limit_price = body.get("limitPrice")
    if not quantity and not limit_price:
        return _json_response({"error": "Provide quantity or limitPrice"}, status=400)

    try:
        result = PublicClient().edit_order(order_id, quantity=quantity, limit_price=limit_price)
    except Exception as exc:
        logger.error("Failed to edit order %s: %s", order_id, exc)
        return _json_response({"error": str(exc)}, status=500)

    return _json_response(result)


def handle_approval(event: dict) -> dict:
    """
    Handle an HTTP GET approval request.

    Single:  /approve?ticker=AAPL&dollars=3.00&expires=...&token=...
    Batch:   /approve?batch=AAPL:3.00,ITA:5.00&expires=...&token=...
    """
    # Handle CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _cors_preflight()

    # Route orders API
    raw_path = event.get("rawPath", "")
    if raw_path == "/balance":
        return _handle_balance(event)
    if raw_path == "/orders":
        return _handle_orders(event)
    if raw_path == "/orders/new":
        return _handle_place_order(event)
    if raw_path.startswith("/orders/") and raw_path.endswith("/edit"):
        order_id = raw_path[len("/orders/"):-len("/edit")]
        if order_id:
            return _handle_edit_order(event, order_id)

    params = event.get("queryStringParameters") or {}

    # Route batch approvals
    if "batch" in params:
        return _handle_batch_approval(params)

    # Route sell approvals
    if params.get("action") == "sell":
        return _handle_sell_approval(params)

    # Route options approvals
    if params.get("action") == "options":
        return _handle_options_approval(params)

    ticker      = (params.get("ticker") or "").upper().strip()
    dollars_str = params.get("dollars", "")
    expires_str = params.get("expires", "")
    token       = params.get("token", "")

    # Validate required params
    if not all([ticker, dollars_str, expires_str, token]):
        logger.warning("Approval request missing params: %s", list(params.keys()))
        return _html_response(_html_error("Invalid or incomplete approval link."), status=400)

    try:
        dollars    = float(dollars_str)
        expires_ts = int(expires_str)
    except (ValueError, TypeError):
        return _html_response(_html_error("Malformed approval link."), status=400)

    # Validate HMAC + expiry
    if not _verify_token(ticker, dollars, expires_ts, token):
        if time.time() > expires_ts:
            reason = (
                f"This approval link has expired. "
                f"Links are valid for {settings.SUGGESTION_EXPIRY_HOURS} hours after they are sent."
            )
        else:
            reason = "Invalid approval link. The link may have been modified or is from a different session."
        logger.warning("Token validation failed for %s $%.2f", ticker, dollars)
        return _html_response(_html_error(reason), status=403)

    # Fetch current price for display (informational — does not block the trade)
    current_price = _fetch_current_price(ticker)

    # Place the trade
    logger.info("Approval validated — placing BUY $%.2f %s", dollars, ticker)
    try:
        client   = PublicClient()
        order    = client.place_order(symbol=ticker, side="BUY", order_type="MARKET", amount=f"{dollars:.2f}")
        order_id = order.get("orderId", "unknown")
        logger.info("Approval order placed: BUY $%.2f %s | orderId=%s", dollars, ticker, order_id)
    except Exception as exc:
        logger.error("Approval order failed for %s: %s", ticker, exc)
        return _html_response(_html_error(f"Order placement failed: {exc}"), status=500)

    price_note = f" at ${current_price:.2f}" if current_price else ""
    _send_confirmation_sns(
        [f"Bought ${dollars:.2f} of {ticker}{price_note}", f"Order ID: {order_id}", "",
         "Order fills immediately if market is open, or at next market open if after hours."],
        f"[TraderBot] Confirmed: ${dollars:.2f} {ticker} approved",
    )

    return _html_response(_html_success(ticker, dollars, order_id, current_price))
