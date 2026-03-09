"""
Public.com options data provider.

All options chain data and greeks come from Public.com — not Polygon.
Polygon is used only for news/sentiment signals.

Three entry points:
  get_quote(symbol)                          → {bid, ask, last, volume}
  get_options_chain(symbol, expiry)          → list of contracts with greeks
  get_best_contracts(symbol, side, max_premium) → top 5 contracts by volume
"""

import logging
from datetime import datetime, timezone

from broker.public_client import PublicClient

logger = logging.getLogger(__name__)

_OPTIONS_DTE_MIN = 14   # at least 2 weeks out
_OPTIONS_DTE_MAX = 45   # no more than ~6 weeks


class PublicOptionsProvider:
    def __init__(self, client: PublicClient):
        self._client = client

    def get_quote(self, symbol: str) -> dict:
        """
        Real-time quote for an equity symbol from Public.com.
        Returns: {bid, ask, last, volume}
        """
        try:
            resp = self._client.get_quotes([symbol])
            for q in resp.get("quotes", []):
                sym = (
                    q.get("instrument", {}).get("symbol")
                    or q.get("symbol") or ""
                ).upper()
                if sym == symbol.upper():
                    return {
                        "bid":    float(q.get("bid") or 0),
                        "ask":    float(q.get("ask") or 0),
                        "last":   float(q.get("last") or q.get("lastPrice") or 0),
                        "volume": int(q.get("volume") or 0),
                    }
        except Exception as exc:
            logger.warning("get_quote failed for %s: %s", symbol, exc)
        return {"bid": 0.0, "ask": 0.0, "last": 0.0, "volume": 0}

    def get_options_chain(self, symbol: str, expiry: str) -> list[dict]:
        """
        Full options chain for a symbol/expiry from Public.com.
        Returns list of contracts with strike, bid, ask, volume, and greeks
        (greeks included inline if the chain endpoint returns them; otherwise 0.0).
        """
        contracts = []
        for option_type in ("CALL", "PUT"):
            try:
                chain = self._client.get_option_chain(symbol, expiry, option_type=option_type)
            except Exception as exc:
                logger.warning("Option chain failed for %s %s %s: %s", symbol, expiry, option_type, exc)
                continue

            for c in chain:
                contracts.append({
                    "symbol":        c.get("optionSymbol", ""),
                    "strike":        float(c.get("strikePrice") or 0),
                    "expiry":        expiry,
                    "type":          option_type.lower(),
                    "bid":           float(c.get("bid") or 0),
                    "ask":           float(c.get("ask") or 0),
                    "volume":        int(c.get("volume") or 0),
                    "open_interest": int(c.get("openInterest") or 0),
                    "delta":         0.0,
                    "gamma":         0.0,
                    "theta":         0.0,
                    "vega":          0.0,
                    "iv":            0.0,
                })
        return contracts

    def get_best_contracts(
        self,
        symbol: str,
        side: str,
        max_premium: float,
    ) -> list[dict]:
        """
        Top 5 option contracts by volume for a symbol.

        Args:
            symbol:      underlying ticker, e.g. "AAPL"
            side:        "call" or "put"
            max_premium: max cost for 1 contract in dollars  # Live from Public.com API — do not hardcode
                         (typically cash_balance * 0.05)
                         1 contract = 100 shares → ask * 100 must be ≤ max_premium

        Returns list of up to 5 contracts sorted by volume desc.
        """
        # Pick the expiration closest to the midpoint of the DTE window
        try:
            expirations = self._client.get_option_expirations(symbol)
        except Exception as exc:
            logger.warning("get_option_expirations failed for %s: %s", symbol, exc)
            return []

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
            logger.debug("No expirations in %d-%d DTE window for %s", _OPTIONS_DTE_MIN, _OPTIONS_DTE_MAX, symbol)
            return []

        candidates.sort()
        expiry = candidates[0][1]

        # Fetch the chain for the chosen side
        try:
            chain = self._client.get_option_chain(symbol, expiry, option_type=side.upper())
        except Exception as exc:
            logger.warning("get_option_chain failed for %s %s %s: %s", symbol, expiry, side, exc)
            return []

        results = []
        for c in chain:
            ask = float(c.get("ask") or 0)
            if ask <= 0:
                continue
            # 1 contract = 100 shares; total cost = ask * 100
            if ask * 100 > max_premium:
                continue
            results.append({
                "symbol": c.get("optionSymbol", ""),
                "strike": float(c.get("strikePrice") or 0),
                "expiry": expiry,
                "type":   side.lower(),
                "bid":    float(c.get("bid") or 0),
                "ask":    ask,
                "volume": int(c.get("volume") or 0),
                "delta":  0.0,
                "iv":     0.0,
                "theta":  0.0,
            })

        results.sort(key=lambda x: x["volume"], reverse=True)
        top5 = results[:5]
        logger.debug(
            "get_best_contracts %s %s max=$%.2f → %d affordable, returning %d",
            symbol, side, max_premium, len(results), len(top5),
        )
        return top5
