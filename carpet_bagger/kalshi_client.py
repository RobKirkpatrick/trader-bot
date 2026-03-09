"""
Kalshi trading API client (v2).

Auth: RSA-SHA256 request signing.
  Each request signs:  timestamp_ms + METHOD + /path (no query string)
  Headers added:
    KALSHI-ACCESS-KEY       — API key ID from Secrets Manager
    KALSHI-ACCESS-SIGNATURE — base64(RSA_SHA256(message))
    KALSHI-ACCESS-TIMESTAMP — milliseconds since epoch

Prices: Kalshi returns prices in cents (integers).
  This client normalises all prices to decimal dollars on return.
  Order methods accept dollar prices and convert internally.

Error handling:
  429 → sleep 10s, retry once
  401 → log + SNS alert + raise (bad API key — halt)
  Other errors → log + raise (caller decides whether to retry)
"""

import base64
import logging
import time
import uuid
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key: str, rsa_private_key_pem: str):
        """
        Args:
            api_key            : Kalshi API key ID (UUID string)
            rsa_private_key_pem: PEM-encoded RSA private key string
        """
        self._api_key = api_key
        self._pem     = rsa_private_key_pem
        self._private_key = self._load_key(rsa_private_key_pem)

    @staticmethod
    def _load_key(pem: str):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        return load_pem_private_key(pem.encode(), password=None)

    def _sign(self, method: str, path: str) -> dict:
        """Build signed auth headers for a request."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        timestamp_ms = str(int(time.time() * 1000))
        # Signature covers full path (including /trade-api/v2 prefix), no query string
        full_path = "/trade-api/v2" + path.split("?")[0]
        message = (timestamp_ms + method.upper() + full_path).encode()

        signature = self._private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self._api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type":            "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """
        Execute a signed request. Retries once on 429.
        Raises on 401 (bad key) and other errors.
        """
        url     = _BASE + path
        headers = self._sign(method, path)

        for attempt in range(2):
            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)

            if resp.status_code == 429:
                if attempt == 0:
                    logger.warning("Kalshi 429 rate limit — sleeping 10s")
                    time.sleep(10)
                    headers = self._sign(method, path)   # fresh timestamp
                    continue
                resp.raise_for_status()

            if resp.status_code == 401:
                logger.error("Kalshi 401 Unauthorized — check KALSHI_API_KEY")
                raise PermissionError("Kalshi API key invalid or expired (401)")

            resp.raise_for_status()
            return resp.json() if resp.content else {}

        return {}  # unreachable but satisfies type checker

    # ------------------------------------------------------------------
    # Exchange
    # ------------------------------------------------------------------

    def get_exchange_status(self) -> dict:
        """Returns {"exchange_active": bool, "trading_active": bool, ...}"""
        return self._request("GET", "/exchange/status")

    def is_trading_active(self) -> bool:
        status = self.get_exchange_status()
        return bool(status.get("trading_active", False))

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return available balance in dollars (API returns cents)."""
        data = self._request("GET", "/portfolio/balance")
        return data.get("balance", 0) / 100.0

    def get_positions(self) -> list[dict]:
        """Return open positions. Each position has 'ticker', 'position' (contracts held), etc."""
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", data.get("positions", []))

    def get_total_deployed(self) -> float:
        """
        Estimate total dollars deployed from open positions.
        Uses current_price × contracts as a rough cost basis.
        """
        positions = self.get_positions()
        total = 0.0
        for p in positions:
            contracts = abs(int(p.get("position", 0)))
            # market_exposure is the max loss (contracts × yes_price for long)
            exposure  = p.get("market_exposure", 0)
            total += abs(exposure) / 100.0   # cents → dollars
        return total

    # ------------------------------------------------------------------
    # Markets / Events
    # ------------------------------------------------------------------

    def get_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        """
        Fetch open events for a sport series.
        Returns list of event dicts (may not embed markets — use get_series_markets).
        """
        path = f"/events?status={status}&series_ticker={series_ticker}"
        data = self._request("GET", path)
        return data.get("events", [])

    def get_series_markets(self, series_ticker: str, status: str = "open", limit: int = 100) -> list[dict]:
        """
        Fetch all open markets for a series directly.
        Preferred over get_events for individual-game series (KXNHLGAME, KXNBAGAMES, etc.)
        where events don't embed markets.
        """
        path = f"/markets?series_ticker={series_ticker}&status={status}&limit={limit}"
        data = self._request("GET", path)
        return data.get("markets", [])

    def discover_sports_game_series(self) -> list[str]:
        """
        Dynamically discover additional sports individual-game series from Kalshi.

        Strict filter: series ticker must start with KX + a major sport prefix AND
        end with GAME or GAMES. This targets individual game winner markets only,
        catching series like KXNBAGAME, KXNCAABBGAME, KXWNBAGAME, KXNFLGAME, etc.

        Returns at most 10 additional series to avoid 429 rate limits during the scout.
        Caller should union with the hardcoded SPORT_SERIES list.
        """
        # Head-to-head team sports only (two competitors, win/loss outcome).
        # Excludes golf (PGA, LPGA), racing (NASCAR, F1, INDYCAR) — multi-competitor markets.
        _SPORT_PREFIXES = ("NBA", "WNBA", "NHL", "MLB", "NCAAB", "NCAAW", "NCAAF", "NFL", "MLS", "IIHF")
        # Series to exclude — non-competitive or multi-competitor (not two-team head-to-head).
        _EXCLUDED_SERIES = {
            "KXMLBSTGAME",  # MLB Spring Training — non-competitive lineups
            "KXPGAH2H",     # PGA golf H2H — multi-player tournament, not a two-team game
            "KXLPGAH2H",    # LPGA golf H2H
        }

        try:
            data = self._request("GET", "/series?limit=200")
        except Exception as exc:
            logger.warning("Series discovery failed: %s", exc)
            return []

        result = []
        for s in data.get("series", []):
            ticker = (s.get("ticker") or "").upper()
            if not ticker.startswith("KX"):
                continue
            if ticker in _EXCLUDED_SERIES:
                continue
            sport_part = ticker[2:]  # strip KX prefix
            # Must end in GAME or GAMES (individual game market, not totals/spreads/futures)
            if not (sport_part.endswith("GAME") or sport_part.endswith("GAMES")):
                continue
            # Must start with a recognized major sport code
            if not any(sport_part.startswith(sp) for sp in _SPORT_PREFIXES):
                continue
            result.append(ticker)

        logger.info("Discovered %d additional sports game series candidates: %s", len(result), result)
        # Cap to avoid excessive API calls and 429s during the scout run
        return result[:10]

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker. Returns market dict."""
        data = self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    def get_yes_ask(self, ticker: str) -> float:
        """Return current yes_ask price in dollars (0.0–1.0)."""
        market = self.get_market(ticker)
        cents  = market.get("yes_ask", 0)
        return cents / 100.0

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_buy(self, ticker: str, yes_price_dollars: float, dollar_amount: float) -> dict:
        """
        Buy 'yes' contracts.

        Args:
            ticker:              Kalshi market ticker
            yes_price_dollars:   current yes_ask in dollars (e.g. 0.87)
            dollar_amount:       dollars to deploy

        Returns order response dict.
        """
        yes_price_cents = int(round(yes_price_dollars * 100))
        count = max(1, int(dollar_amount / yes_price_dollars))

        body = {
            "ticker":           ticker,
            "side":             "yes",
            "action":           "buy",
            "type":             "limit",
            "count":            count,
            "yes_price":        yes_price_cents,
            "client_order_id":  str(uuid.uuid4()),
        }
        logger.info(
            "Kalshi BUY: %s | yes_price=%dc ($%.2f) | count=%d | total~=$%.2f",
            ticker, yes_price_cents, yes_price_dollars, count, count * yes_price_dollars,
        )
        result = self._request("POST", "/portfolio/orders", json=body)
        logger.info("Kalshi BUY result: %s", result)
        return result

    def place_sell(self, ticker: str, contract_count: int, yes_bid_dollars: float = 0.01) -> dict:
        """
        Sell (close) an existing 'yes' position.

        Args:
            ticker:           Kalshi market ticker
            contract_count:   number of contracts to sell
            yes_bid_dollars:  current yes bid price in dollars; set low to guarantee fill
        """
        yes_price_cents = max(1, int(round(yes_bid_dollars * 100)))
        body = {
            "ticker":          ticker,
            "side":            "yes",
            "action":          "sell",
            "type":            "limit",
            "count":           contract_count,
            "yes_price":       yes_price_cents,
            "client_order_id": str(uuid.uuid4()),
        }
        logger.info("Kalshi SELL: %s | count=%d", ticker, contract_count)
        result = self._request("POST", "/portfolio/orders", json=body)
        logger.info("Kalshi SELL result: %s", result)
        return result
