"""
Lambda Function URL handler for one-click trade approvals.

Invoked when Rob clicks an approval link from the evening suggestion email.
The URL format is:
    https://{function-url}/approve?ticker=AAPL&dollars=3.00&expires=1234567890&token=abc...

Security model:
  - Token is HMAC-SHA256(SECRET, f"{ticker}:{dollars:.2f}:{expires_ts}")
  - Expiry is enforced via unix timestamp in the URL (no database required)
  - SECRET is stored in AWS Secrets Manager and injected via SUGGESTION_TOKEN_SECRET env var

Response: HTML page (rendered in the browser when Rob clicks the link).
"""

import hashlib
import hmac
import logging
import time

from config.settings import settings
from broker.public_client import PublicClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

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
# Main handler
# ---------------------------------------------------------------------------

def handle_approval(event: dict) -> dict:
    """
    Handle an HTTP GET approval request.

    Single:  /approve?ticker=AAPL&dollars=3.00&expires=...&token=...
    Batch:   /approve?batch=AAPL:3.00,ITA:5.00&expires=...&token=...
    """
    params = event.get("queryStringParameters") or {}

    # Route batch approvals
    if "batch" in params:
        return _handle_batch_approval(params)

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
