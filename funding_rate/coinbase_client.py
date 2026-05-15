"""
Coinbase Advanced Trade API client with support for spot and perpetual futures.

This module provides a thin, well-typed wrapper around Coinbase's REST API
for both spot trading and perpetual futures (INTX) with proper JWT auth,
rate-limit handling, and error recovery.
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Optional

import aiohttp
import jwt
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)


class CoinbaseAPIError(Exception):
    """Raised on unrecoverable Coinbase API errors."""

    pass


class CoinbaseAuthError(Exception):
    """Raised on authentication failures (401 unauthorized)."""

    pass


class CoinbaseClient:
    """
    NEW: Async HTTP client for Coinbase Advanced Trade API.

    Handles JWT authentication, rate-limit retries, and both spot + futures
    trading. Designed for use in Lambda functions with short execution windows.
    """

    def __init__(
        self,
        api_key_name: str,
        private_key_pem: str,
        base_url: str = "https://api.coinbase.com",
        max_retries: int = 3,
        request_timeout: int = 10,
    ):
        """
        Initialize Coinbase client with API credentials.

        Args:
            api_key_name: Full key identifier from Coinbase (e.g., "organizations/xxx/apiKeys/yyy")
            private_key_pem: EC private key in PEM format (P-256 / ES256)
            base_url: API base URL (default: Coinbase production)
            max_retries: Max retries on 429 rate-limit
            request_timeout: Timeout per request in seconds
        """
        self.api_key_name = api_key_name
        self.private_key_pem = private_key_pem
        self.base_url = base_url
        self.max_retries = max_retries
        self.request_timeout = request_timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create and cache aiohttp session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()

    def _build_jwt(self, method: str, path: str) -> str:
        """
        Build JWT token for Coinbase API authentication.

        Uses EC P-256 private key to sign JWT with ES256 algorithm.
        Token is valid for 120 seconds.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., "/api/v3/brokerage/products")

        Returns:
            Signed JWT token

        Raises:
            ValueError: If private key is invalid
        """
        try:
            private_key = serialization.load_pem_private_key(
                self.private_key_pem.encode(),
                password=None,
            )
        except Exception as e:
            raise ValueError(f"Failed to load private key: {e}") from e

        now = int(time.time())
        payload = {
            "sub": self.api_key_name,
            "iss": "coinbase-cloud",
            "nbf": now,
            "exp": now + 120,  # Valid for 2 minutes
            "uri": f"{method} api.coinbase.com{path}",
        }

        token = jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers={
                "kid": self.api_key_name,
                "nonce": secrets.token_hex(16),
            },
        )
        return token

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Perform authenticated request to Coinbase API.

        Handles rate-limit retries (429) and fails fast on auth errors (401).

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            body: JSON request body

        Returns:
            Parsed JSON response

        Raises:
            CoinbaseAuthError: On 401 authentication failure
            CoinbaseAPIError: On other unrecoverable errors
        """
        session = await self._ensure_session()
        token = self._build_jwt(method, path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        url = self.base_url + path
        body_str = json.dumps(body) if body else None

        for attempt in range(self.max_retries):
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    data=body_str,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                ) as response:
                    if response.status == 401:
                        error_msg = await response.text()
                        raise CoinbaseAuthError(
                            f"Authentication failed: {error_msg}"
                        )

                    if response.status == 429:
                        # Rate limited; exponential backoff
                        backoff = 2 ** attempt
                        logger.warning(
                            f"Rate limited on {method} {path}, "
                            f"retrying in {backoff}s (attempt {attempt + 1}/{self.max_retries})"
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if response.status >= 400:
                        error_body = await response.text()
                        raise CoinbaseAPIError(
                            f"{method} {path} returned {response.status}: {error_body}"
                        )

                    return await response.json()

            except asyncio.TimeoutError:
                if attempt == self.max_retries - 1:
                    raise CoinbaseAPIError(f"Timeout on {method} {path}")
                logger.warning(f"Timeout on {method} {path}, retrying...")
                await asyncio.sleep(1)
                continue

        raise CoinbaseAPIError(
            f"Failed after {self.max_retries} attempts: {method} {path}"
        )

    async def get_funding_rate(self, perp_ticker: str) -> float:
        """
        Fetch current 8-hour funding rate for a perpetual contract.

        Args:
            perp_ticker: Perpetual product ID (e.g., "BTC-PERP-INTX")

        Returns:
            8-hour funding rate as a decimal (e.g., 0.0003)

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = f"/api/v3/brokerage/cfm/products/{perp_ticker}"
        response = await self._request("GET", path)
        rate_str = response.get("current_funding_rate", "0")
        return float(rate_str)

    async def get_best_bid_ask(
        self, product_id: str
    ) -> dict[str, float]:
        """
        Get current best bid, ask, and mid prices for a product.

        Args:
            product_id: Product ID (e.g., "BTC-USD" for spot, "BTC-PERP-INTX" for perp)

        Returns:
            Dict with keys: "bid", "ask", "mid"

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = "/api/v3/brokerage/best_bid_ask"
        response = await self._request("GET", path, params={"product_ids": product_id})

        if "pricebooks" not in response or not response["pricebooks"]:
            raise CoinbaseAPIError(
                f"No price data for {product_id}"
            )

        book = response["pricebooks"][0]
        bid = float(book.get("bids", [{"price": "0"}])[0]["price"])
        ask = float(book.get("asks", [{"price": "0"}])[0]["price"])
        mid = (bid + ask) / 2

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }

    async def get_spot_balance(self, currency: str) -> float:
        """
        Get available balance for a spot currency.

        Args:
            currency: Currency code (e.g., "BTC", "USD")

        Returns:
            Available balance as a float

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = "/api/v3/brokerage/accounts"
        response = await self._request("GET", path)

        for account in response.get("accounts", []):
            if account.get("currency") == currency:
                return float(account.get("available_balance", {}).get("value", 0))

        return 0.0

    async def get_futures_position(
        self, perp_ticker: str
    ) -> Optional[dict[str, Any]]:
        """
        Get open perpetual futures position for a contract.

        Args:
            perp_ticker: Perpetual product ID (e.g., "BTC-PERP-INTX")

        Returns:
            Position dict if open, or None if no position

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = "/api/v3/brokerage/cfm/positions"
        response = await self._request("GET", path)

        for position in response.get("positions", []):
            if position.get("product_id") == perp_ticker:
                return position

        return None

    async def place_spot_buy(self, product_id: str, size_usd: float) -> str:
        """
        Place a market buy order on the spot market using USD notional.

        Args:
            product_id: Spot product ID (e.g., "BTC-USD")
            size_usd: USD amount to spend

        Returns:
            Order ID

        Raises:
            CoinbaseAPIError: On API errors
        """
        # Get current price to estimate base size
        prices = await self.get_best_bid_ask(product_id)
        ask = prices["ask"]
        base_size = size_usd / ask

        path = "/api/v3/brokerage/orders"
        body = {
            "client_order_id": secrets.token_hex(16),
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(size_usd),
                }
            },
        }

        response = await self._request("POST", path, body=body)
        order_id = response.get("order_id")
        if not order_id:
            raise CoinbaseAPIError(f"No order_id in response: {response}")

        logger.info(
            f"Spot BUY order placed: {order_id} for {size_usd} USD of {product_id}"
        )
        return order_id

    async def place_perp_short(self, perp_ticker: str, size_usd: float) -> str:
        """
        Place a market short order on perpetual futures.

        Args:
            perp_ticker: Perpetual product ID (e.g., "BTC-PERP-INTX")
            size_usd: USD notional to short

        Returns:
            Order ID

        Raises:
            CoinbaseAPIError: On API errors
        """
        # Get current price to estimate contracts size
        prices = await self.get_best_bid_ask(perp_ticker)
        bid = prices["bid"]
        contracts = size_usd / bid

        path = "/api/v3/brokerage/orders"
        body = {
            "client_order_id": secrets.token_hex(16),
            "product_id": perp_ticker,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(size_usd),
                }
            },
        }

        response = await self._request("POST", path, body=body)
        order_id = response.get("order_id")
        if not order_id:
            raise CoinbaseAPIError(f"No order_id in response: {response}")

        logger.info(
            f"Perp SHORT order placed: {order_id} for {size_usd} USD of {perp_ticker}"
        )
        return order_id

    async def place_spot_sell(self, product_id: str, quantity: float) -> str:
        """
        Place a market sell order to close a spot position.

        Args:
            product_id: Spot product ID (e.g., "BTC-USD")
            quantity: Amount of base asset to sell

        Returns:
            Order ID

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = "/api/v3/brokerage/orders"
        body = {
            "client_order_id": secrets.token_hex(16),
            "product_id": product_id,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": str(quantity),
                }
            },
        }

        response = await self._request("POST", path, body=body)
        order_id = response.get("order_id")
        if not order_id:
            raise CoinbaseAPIError(f"No order_id in response: {response}")

        logger.info(
            f"Spot SELL order placed: {order_id} for {quantity} {product_id.split('-')[0]}"
        )
        return order_id

    async def place_perp_close(self, perp_ticker: str, contracts: float) -> str:
        """
        Place a market BUY order to close a short perpetual position.

        Args:
            perp_ticker: Perpetual product ID (e.g., "BTC-PERP-INTX")
            contracts: Number of contracts to buy back

        Returns:
            Order ID

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = "/api/v3/brokerage/orders"
        body = {
            "client_order_id": secrets.token_hex(16),
            "product_id": perp_ticker,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": str(contracts),
                }
            },
        }

        response = await self._request("POST", path, body=body)
        order_id = response.get("order_id")
        if not order_id:
            raise CoinbaseAPIError(f"No order_id in response: {response}")

        logger.info(
            f"Perp CLOSE order placed: {order_id} for {contracts} contracts of {perp_ticker}"
        )
        return order_id

    async def get_order_status(
        self, order_id: str
    ) -> dict[str, Any]:
        """
        Get the status of an order by ID.

        Args:
            order_id: The order ID to check

        Returns:
            Order status dict with keys: order_id, status, filled_size, etc.

        Raises:
            CoinbaseAPIError: On API errors
        """
        path = f"/api/v3/brokerage/orders/historical/{order_id}"
        response = await self._request("GET", path)
        return response

    async def get_active_futures(self, base_asset: str) -> Optional[dict[str, Any]]:
        """
        Return the nearest-expiry dated futures contract for a base asset.

        Queries all FUTURE products and filters by base_asset prefix
        (e.g., "BTC" matches "BTC-27JUN25-CDE"). Picks the contract with
        the soonest expiry that is still in the future.

        Args:
            base_asset: Asset prefix (e.g., "BTC", "ETH")

        Returns:
            Product dict for nearest-expiry contract, or None if not found

        Raises:
            CoinbaseAPIError: On API errors
        """
        from . import strategy as _strat  # local import to avoid circular

        path = "/api/v3/brokerage/products"
        response = await self._request(
            "GET", path, params={"product_type": "FUTURE", "limit": 100}
        )

        candidates = []
        for product in response.get("products", []):
            pid = product.get("product_id", "")
            if not pid.startswith(f"{base_asset}-"):
                continue
            expiry = _strat.parse_expiry_date(pid)
            if expiry is None:
                continue
            dte = _strat.days_to_expiry(pid)
            if dte is None or dte <= 0:
                continue
            candidates.append((dte, product))

        if not candidates:
            return None

        candidates.sort(key=lambda t: t[0])
        return candidates[0][1]

    async def get_futures_price(self, product_id: str) -> float:
        """
        Return current mid price for a dated futures contract.

        Args:
            product_id: Futures product ID (e.g., "BTC-27JUN25-CDE")

        Returns:
            Mid price as a float

        Raises:
            CoinbaseAPIError: If price data is unavailable
        """
        prices = await self.get_best_bid_ask(product_id)
        return prices["mid"]

    async def is_trading_active(self) -> bool:
        """
        Simple health check: can we list products?

        Returns:
            True if API is responsive

        Raises:
            CoinbaseAPIError: On API errors
        """
        try:
            path = "/api/v3/brokerage/products"
            response = await self._request("GET", path, params={"limit": 1})
            return "products" in response
        except Exception:
            return False
