"""
Price-based market signals via Public.com quotes API.

Flow:
  1. Fetch current prices for all tickers from Public.com (1 authenticated call).
  2. Fetch previous trading day's close prices from Polygon grouped daily bars
     (1 unauthenticated call covers ALL symbols at once).
  3. Compute intraday % change = (current - prev_close) / prev_close * 100.
  4. Normalize to sentiment score [-1.0, +1.0].

Signal blending:
  price_score = 0.60 * own_change + 0.25 * spy_change + 0.10 * qqq_change + 0.05 * vixy_change
"""

import logging
from datetime import date, timedelta

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

_BREADTH_TICKERS = ["SPY", "QQQ", "VIXY"]


def _normalize(change_pct: float) -> float:
    """
    Map a daily % change to a sentiment score in [-1.0, +1.0].

    Calibration:
      ±2%  → ±0.70  (triggers signal when combined with macro)
      ±5%  → ±1.00  (clamped)

    Formula: score = change_pct * 0.35, clamped.
    """
    return max(-1.0, min(1.0, change_pct * 0.35))


def _fetch_prev_closes(symbols: list[str], api_key: str) -> dict[str, float]:
    """
    Fetch previous trading day's close prices from Polygon grouped daily bars.

    One API call returns data for all US stocks — no per-ticker rate limiting.
    Returns {ticker: close_price}. Returns {} on failure (graceful degradation).
    """
    # Walk back to the most recent weekday
    prev = date.today() - timedelta(days=1)
    for _ in range(7):
        if prev.weekday() < 5:
            break
        prev -= timedelta(days=1)

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{prev.isoformat()}"
    try:
        resp = requests.get(
            url,
            params={"adjusted": "true", "apiKey": api_key},
            timeout=15,
        )
        if resp.status_code in (403, 404):
            logger.warning("Polygon grouped daily bars returned %s — no prev-close data", resp.status_code)
            return {}
        resp.raise_for_status()
        results = resp.json().get("results", [])
        symbols_upper = {s.upper() for s in symbols}
        lookup = {
            r["T"]: float(r["c"])
            for r in results
            if r.get("T") in symbols_upper and r.get("c")
        }
        logger.info(
            "Polygon prev closes (%s): found %d of %d symbols",
            prev.isoformat(), len(lookup), len(symbols),
        )
        return lookup
    except Exception as exc:
        logger.warning("Polygon prev close fetch failed: %s", exc)
        return {}


def fetch_price_signals(
    tickers: list[str] | None = None,
    broker_client=None,
    api_key: str | None = None,  # unused — kept for interface consistency
) -> dict[str, float]:
    """
    Fetch intraday price signals for all tickers.

    Returns a dict mapping ticker → price signal score [-1.0, +1.0].
    Tickers with no data return 0.0.

    Args:
        tickers      : list of ticker symbols; defaults to settings.WATCHLIST
        broker_client: a PublicClient instance (reused if already authenticated)
    """
    tickers = [t.upper() for t in (tickers or settings.WATCHLIST)]
    all_symbols = list({*tickers, *_BREADTH_TICKERS})

    if broker_client is None:
        from broker.public_client import PublicClient
        broker_client = PublicClient()

    # 1. Current prices from Public.com
    try:
        resp = broker_client.get_quotes(all_symbols)
    except Exception as exc:
        logger.error("Public.com quotes failed: %s", exc)
        return {t: 0.0 for t in tickers}

    # Response: {"quotes": [{"instrument": {"symbol": "AAPL"}, "last": "185.50", ...}]}
    quote_list = resp.get("quotes", []) if isinstance(resp, dict) else resp

    current_prices: dict[str, float] = {}
    for q in quote_list:
        sym = (
            q.get("instrument", {}).get("symbol")
            or q.get("symbol")
            or q.get("ticker")
            or ""
        ).upper()
        raw = q.get("last") or q.get("lastPrice") or q.get("price")
        if sym and raw:
            try:
                current_prices[sym] = float(raw)
            except (ValueError, TypeError):
                pass

    if not current_prices:
        logger.warning("Public.com quotes returned no price data — response: %s", resp)
        return {t: 0.0 for t in tickers}

    logger.info("Current prices: %s", {k: f"${v:.2f}" for k, v in current_prices.items()})

    # 2. Previous closes from Polygon (1 call, all symbols)
    prev_closes = _fetch_prev_closes(all_symbols, settings.POLYGON_API_KEY)

    def change_pct(sym: str) -> float:
        curr = current_prices.get(sym, 0.0)
        prev = prev_closes.get(sym, 0.0)
        if curr and prev:
            return (curr - prev) / prev * 100
        return 0.0

    # 3. Breadth scores
    spy_pct   = change_pct("SPY")
    qqq_pct   = change_pct("QQQ")
    vixy_pct  = change_pct("VIXY")

    spy_score  = _normalize(spy_pct)
    qqq_score  = _normalize(qqq_pct)
    vixy_score = _normalize(-vixy_pct)   # VIXY up = fear = bearish

    logger.info(
        "Market breadth — SPY: %.2f%% (%.3f) | QQQ: %.2f%% (%.3f) | VIXY: %.2f%% (fear=%.3f)",
        spy_pct, spy_score,
        qqq_pct, qqq_score,
        vixy_pct, vixy_score,
    )

    # 4. Per-ticker scores
    results: dict[str, float] = {}
    for ticker in tickers:
        own_pct   = change_pct(ticker)
        own_score = _normalize(own_pct)

        combined = (
            0.60 * own_score
            + 0.25 * spy_score
            + 0.10 * qqq_score
            + 0.05 * vixy_score
        )
        combined = round(max(-1.0, min(1.0, combined)), 4)
        results[ticker] = combined

        logger.info(
            "Price signal — %s: %.2f%% → %.3f (spy=%.3f qqq=%.3f)",
            ticker, own_pct, combined, spy_score, qqq_score,
        )

    return results
