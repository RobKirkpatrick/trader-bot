"""
Public.com broker client.

Auth flow:
  POST /userapiauthservice/personal/access-tokens
  Body : { "secret": "<api_secret>", "validityInMinutes": 60 }
  Response: { "accessToken": "<jwt>" }

All subsequent requests attach:
  Authorization: Bearer <accessToken>
"""

import logging
import uuid
import requests
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger(__name__)


def _parse_osi_strike(osi_symbol: str) -> float:
    """Extract strike price from an OSI option symbol (last 8 digits represent cents × 10)."""
    try:
        return int(osi_symbol[-8:]) / 1000.0
    except (ValueError, IndexError):
        return 0.0


class PublicClient:
    _BASE = "https://api.public.com"

    def __init__(self, api_secret: str | None = None):
        self._secret = api_secret or settings.PUBLIC_API_SECRET
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._account_id: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        """Fetch a fresh access token from Public.com."""
        url = settings.PUBLIC_AUTH_URL
        payload = {
            "secret": self._secret,
            "validityInMinutes": settings.PUBLIC_TOKEN_VALIDITY_MINUTES,
        }
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()

        data = response.json()
        self._token = data["accessToken"]
        self._token_expires = datetime.now(timezone.utc) + timedelta(
            minutes=settings.PUBLIC_TOKEN_VALIDITY_MINUTES - 1
        )
        logger.info("Public.com: authenticated successfully.")

    def _ensure_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token is None or (self._token_expires and now >= self._token_expires):
            self._authenticate()
        return self._token  # type: ignore[return-value]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_accounts(self) -> list[dict]:
        """Return all accounts for the authenticated user."""
        url = f"{self._BASE}/userapigateway/trading/account"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def get_account_id(self) -> str:
        """Return the primary brokerage accountId, caching after first call."""
        if self._account_id:
            return self._account_id
        accounts = self.get_accounts()
        if not accounts:
            raise RuntimeError("No accounts found on this Public.com profile.")
        # Prefer BROKERAGE type; fallback to first account
        for acct in accounts:
            if acct.get("accountType") == "BROKERAGE":
                self._account_id = acct["accountId"]
                return self._account_id
        self._account_id = accounts[0]["accountId"]
        return self._account_id

    def get_portfolio(self) -> dict:
        """
        Return a full portfolio snapshot from Public.com.

        Key fields:
          portfolio["buyingPower"]["cashOnlyBuyingPower"]  — cash available to trade
          portfolio["buyingPower"]["buyingPower"]          — buying power incl. margin
          portfolio["equity"]                              — list of {type, value, ...}
          portfolio["positions"]                           — open positions
          portfolio["orders"]                              — open orders
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/portfolio/v2"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_buying_power(self) -> float:
        """
        Return cash-only buying power as a float.
        This is the safest balance to use for position sizing — no margin included.
        """
        portfolio = self.get_portfolio()
        bp = portfolio.get("buyingPower", {})
        raw = bp.get("cashOnlyBuyingPower") or bp.get("buyingPower", "0")
        return float(raw)

    def get_account_balance(self) -> dict:
        """
        Return live account balance as a dict.  # Live from Public.com API — do not hardcode

        Keys:
          cash_balance    — cash-only buying power (safest for position sizing)
          buying_power    — total buying power (may include margin)
          portfolio_value — total account value (positions + cash)
        """
        portfolio = self.get_portfolio()
        bp = portfolio.get("buyingPower", {})

        # Live from Public.com API — do not hardcode
        cash_balance  = float(bp.get("cashOnlyBuyingPower") or bp.get("buyingPower") or 0)
        buying_power  = float(bp.get("buyingPower") or bp.get("cashOnlyBuyingPower") or 0)

        # Portfolio value: try common field paths
        portfolio_value = 0.0
        equity_list = portfolio.get("equity", [])
        if isinstance(equity_list, list) and equity_list:
            for item in equity_list:
                v = item.get("value") or item.get("totalValue")
                if v:
                    try:
                        portfolio_value = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
        if not portfolio_value:
            portfolio_value = float(
                portfolio.get("totalValue")
                or portfolio.get("portfolioValue")
                or cash_balance
            )

        logger.info(
            "Account balance — cash: $%.2f  buying_power: $%.2f  portfolio: $%.2f",
            cash_balance, buying_power, portfolio_value,
        )
        return {
            "cash_balance":    cash_balance,    # Live from Public.com API — do not hardcode
            "buying_power":    buying_power,    # Live from Public.com API — do not hardcode
            "portfolio_value": portfolio_value, # Live from Public.com API — do not hardcode
        }

    def get_positions(self) -> list[dict]:
        """Return current open positions from the portfolio snapshot."""
        return self.get_portfolio().get("positions", [])

    def get_account_and_positions(self) -> tuple[dict, list[dict]]:
        """
        Return (account_balance, positions) from a single portfolio API call.
        Use instead of calling get_account_balance() + get_positions() separately.
        """
        portfolio = self.get_portfolio()
        bp = portfolio.get("buyingPower", {})

        cash_balance = float(bp.get("cashOnlyBuyingPower") or bp.get("buyingPower") or 0)
        buying_power = float(bp.get("buyingPower") or bp.get("cashOnlyBuyingPower") or 0)

        portfolio_value = 0.0
        equity_list = portfolio.get("equity", [])
        if isinstance(equity_list, list) and equity_list:
            for item in equity_list:
                v = item.get("value") or item.get("totalValue")
                if v:
                    try:
                        portfolio_value = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
        if not portfolio_value:
            portfolio_value = float(
                portfolio.get("totalValue")
                or portfolio.get("portfolioValue")
                or cash_balance
            )

        account_balance = {
            "cash_balance":    cash_balance,
            "buying_power":    buying_power,
            "portfolio_value": portfolio_value,
        }
        positions = portfolio.get("positions", [])
        logger.info(
            "Account — cash: $%.2f  buying_power: $%.2f  positions: %d",
            cash_balance, buying_power, len(positions),
        )
        return account_balance, positions

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: list[str]) -> dict:
        """Return latest quotes for a list of symbols."""
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/marketdata/{account_id}/quotes"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"instruments": [{"symbol": s, "type": "EQUITY"} for s in symbols]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def preflight_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        amount: str | None = None,
        quantity: str | None = None,
        limit_price: str | None = None,
    ) -> dict:
        """
        Validate a potential order and get estimated cost/proceeds.

        Args:
            symbol     : ticker, e.g. "CAT"
            side       : "BUY" or "SELL"
            order_type : "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT"
            amount     : notional dollar amount, e.g. "5.00"
            quantity   : share quantity, e.g. "1"  (mutually exclusive with amount)
            limit_price: required for LIMIT / STOP_LIMIT orders
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/preflight/single-leg"

        body: dict = {
            "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
            "orderSide": side.upper(),
            "orderType": order_type.upper(),
            "expiration": {"timeInForce": "DAY"},
        }
        if amount:
            body["amount"] = amount
        if quantity:
            body["quantity"] = quantity
        if limit_price:
            body["limitPrice"] = limit_price

        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        amount: str | None = None,
        quantity: str | None = None,
        limit_price: str | None = None,
        order_id: str | None = None,
    ) -> dict:
        """
        Submit a new order to Public.com.

        Returns { "orderId": "<uuid>" } on success.
        Order placement is asynchronous — call get_order() to confirm execution.
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/order"

        body: dict = {
            "orderId": order_id or str(uuid.uuid4()),
            "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
            "orderSide": side.upper(),
            "orderType": order_type.upper(),
            "expiration": {"timeInForce": "DAY"},
        }
        if amount:
            body["amount"] = amount
        if quantity:
            body["quantity"] = quantity
        if limit_price:
            body["limitPrice"] = limit_price

        logger.info(
            "Placing order: %s %s %s | amount=%s qty=%s",
            side.upper(), symbol.upper(), order_type.upper(), amount, quantity,
        )
        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Order submitted: %s", result)
        return result

    def get_order(self, order_id: str) -> dict:
        """Fetch the status/execution details for a submitted order."""
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/order/{order_id}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/order/{order_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_orders(self, status: str = "open") -> list[dict]:
        """Return orders filtered by status."""
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/orders"
        resp = requests.get(
            url, headers=self._headers(), params={"status": status}, timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("orders", [])

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def get_option_greeks(self, option_symbol: str) -> dict:
        """
        Fetch greeks for a specific option contract via the option-details endpoint.
        Returns: {delta, gamma, theta, vega, iv}
        Falls back to empty dict if the endpoint is unavailable.
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/option-details/{account_id}/greeks"
        try:
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"osiSymbols": option_symbol},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            greeks_list = data.get("greeks", [])
            if not greeks_list:
                return {}
            g = greeks_list[0].get("greeks", {})
            return {
                "delta": float(g.get("delta") or 0),
                "gamma": float(g.get("gamma") or 0),
                "theta": float(g.get("theta") or 0),
                "vega":  float(g.get("vega") or 0),
                "iv":    float(g.get("impliedVolatility") or 0),
            }
        except Exception as exc:
            logger.debug("Option greeks unavailable for %s: %s", option_symbol, exc)
            return {}

    def get_option_expirations(self, symbol: str) -> list[str]:
        """
        Return available expiration dates for an underlying symbol.

        Returns a list of date strings, e.g. ["2026-03-21", "2026-04-17", ...].
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/marketdata/{account_id}/option-expirations"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"instrument": {"symbol": symbol.upper(), "type": "EQUITY"}},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("expirations", [])

    def get_option_chain(
        self,
        symbol: str,
        expiration: str,
        option_type: str = "PUT",
    ) -> list[dict]:
        """
        Return the option chain for a symbol/expiration/type combination.

        Args:
            symbol      : underlying, e.g. "SPY"
            expiration  : date string, e.g. "2026-03-21"
            option_type : "PUT" or "CALL"

        Returns a list of option contract dicts with keys:
            { "optionSymbol": str, "strikePrice": float, "bid": float, "ask": float,
              "volume": int, "openInterest": int }
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/marketdata/{account_id}/option-chain"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"instrument": {"symbol": symbol.upper(), "type": "EQUITY"}, "expirationDate": expiration},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response: {"baseSymbol": ..., "calls": [...], "puts": [...]}
        # Each item: {"instrument": {"symbol": "SOFI260320C00015000"}, "bid": "0.50", "ask": "0.55", ...}
        side_key = "calls" if option_type.upper() == "CALL" else "puts"
        raw = data.get(side_key, [])
        contracts = []
        for c in raw:
            osi = c.get("instrument", {}).get("symbol", "")
            contracts.append({
                "optionSymbol": osi,
                "strikePrice":  _parse_osi_strike(osi),
                "bid":          float(c.get("bid") or 0),
                "ask":          float(c.get("ask") or 0),
                "volume":       int(c.get("volume") or 0),
                "openInterest": int(c.get("openInterest") or 0),
            })
        return contracts

    def get_nearest_put(
        self,
        symbol: str,
        target_delta: float = 0.30,
        dte_min: int = 7,
        dte_max: int = 45,
    ) -> dict | None:
        """
        Find the nearest-ATM put for `symbol` within the given DTE window.

        Picks the expiration closest to the midpoint of [dte_min, dte_max] and
        returns the contract whose strike is closest to the current price.

        Returns None if no suitable contract is found.
        """
        from datetime import date as date_type

        expirations = self.get_option_expirations(symbol)
        if not expirations:
            logger.warning("No option expirations found for %s", symbol)
            return None

        today = datetime.now(timezone.utc).date()
        target_dte = (dte_min + dte_max) / 2

        # Filter to DTE window and pick closest to target
        candidates = []
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte_min <= dte <= dte_max:
                candidates.append((abs(dte - target_dte), exp_str))

        if not candidates:
            logger.warning(
                "No %s expirations in %d-%d DTE window for %s",
                symbol, dte_min, dte_max, symbol,
            )
            return None

        candidates.sort()
        chosen_expiration = candidates[0][1]

        # Get the put chain for that expiration
        chain = self.get_option_chain(symbol, chosen_expiration, option_type="PUT")
        if not chain:
            return None

        # Get current price to find ATM strike
        try:
            quotes = self.get_quotes([symbol])
            q0 = quotes.get("quotes", [{}])[0]
            current_price = float(q0.get("last") or q0.get("lastPrice") or 0)
        except Exception:
            current_price = 0.0

        if current_price <= 0:
            # Fallback: return the contract with the median strike
            chain.sort(key=lambda c: float(c.get("strikePrice", 0)))
            return chain[len(chain) // 2] if chain else None

        # Pick contract closest to current price (ATM put)
        chain.sort(key=lambda c: abs(float(c.get("strikePrice", 0)) - current_price))
        best = chain[0]
        logger.info(
            "Nearest put for %s: strike=%s exp=%s",
            symbol, best.get("strikePrice"), chosen_expiration,
        )
        return best

    def place_options_order(
        self,
        option_symbol: str,
        side: str,
        quantity: str,
        order_type: str = "MARKET",
        limit_price: str | None = None,
        order_id: str | None = None,
    ) -> dict:
        """
        Submit an options order to Public.com.

        Args:
            option_symbol : OCC-style option symbol, e.g. "SPY250321P00580000"
            side          : "BUY" or "SELL"
            quantity      : number of contracts as string, e.g. "1"
            order_type    : "MARKET" or "LIMIT"
            limit_price   : required for LIMIT orders (price per contract)
            order_id      : optional idempotency key (UUID generated if omitted)

        Returns { "orderId": "<uuid>" } on success.
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/order"

        body: dict = {
            "orderId":            order_id or str(uuid.uuid4()),
            "instrument":         {"symbol": option_symbol, "type": "OPTION"},
            "orderSide":          side.upper(),
            "orderType":          order_type.upper(),
            "quantity":           quantity,
            "expiration":         {"timeInForce": "DAY"},
            "openCloseIndicator": "OPEN" if side.upper() == "BUY" else "CLOSE",
        }
        if limit_price:
            body["limitPrice"] = limit_price

        logger.info(
            "Placing options order: %s %s %s | qty=%s",
            side.upper(), option_symbol, order_type.upper(), quantity,
        )
        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Options order submitted: %s", result)
        return result

    def preflight_options_order(
        self,
        option_symbol: str,
        side: str,
        quantity: str,
        order_type: str = "MARKET",
        limit_price: str | None = None,
    ) -> dict:
        """
        Validate an options order and get estimated cost/proceeds.

        Same args as place_options_order (minus order_id).
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/preflight/single-leg"

        body: dict = {
            "instrument":         {"symbol": option_symbol, "type": "OPTION"},
            "orderSide":          side.upper(),
            "orderType":          order_type.upper(),
            "quantity":           quantity,
            "expiration":         {"timeInForce": "DAY"},
            "openCloseIndicator": "OPEN" if side.upper() == "BUY" else "CLOSE",
        }
        if limit_price:
            body["limitPrice"] = limit_price

        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Multi-leg orders (spreads, straddles, covered calls, etc.)
    # ------------------------------------------------------------------

    @staticmethod
    def make_option_leg(
        base_symbol: str,
        option_type: str,
        strike: str,
        expiration: str,
        side: str,
        open_close: str = "OPEN",
        ratio: int = 1,
    ) -> dict:
        """
        Build a single leg dict for use in preflight_multi_leg / place_multi_leg.

        Args:
            base_symbol  : underlying ticker, e.g. "SPY"
            option_type  : "CALL" or "PUT"
            strike       : strike price as string, e.g. "580.00"
            expiration   : date string, e.g. "2025-03-21"
            side         : "BUY" or "SELL"
            open_close   : "OPEN" (new position) or "CLOSE" (close existing)
            ratio        : number of contracts relative to other legs (usually 1)
        """
        return {
            "instrument": {
                "symbol":          base_symbol.upper(),
                "type":            "OPTION",
                "baseSymbol":      base_symbol.upper(),
                "optionType":      option_type.upper(),
                "strikePrice":     str(strike),
                "optionExpireDate": expiration,
            },
            "side":               side.upper(),
            "openCloseIndicator": open_close.upper(),
            "ratioQuantity":      ratio,
        }

    def preflight_multi_leg(
        self,
        legs: list[dict],
        quantity: str,
        order_type: str = "LIMIT",
        limit_price: str = "0.01",
    ) -> dict:
        """
        Validate a multi-leg options strategy and get estimated cost.

        Args:
            legs        : list of leg dicts from make_option_leg()
            quantity    : number of spreads/strategies, e.g. "1"
            order_type  : "MARKET" or "LIMIT" (LIMIT recommended for spreads)
            limit_price : net debit/credit limit, e.g. "1.50"

        Common strategies:
            Bear put spread  : BUY lower put + SELL higher put (cheaper than outright put)
            Bull call spread : BUY lower call + SELL higher call
            Long straddle    : BUY call + BUY put at same strike
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/preflight/multi-leg"

        body = {
            "orderType":  order_type.upper(),
            "limitPrice": limit_price,
            "quantity":   quantity,
            "expiration": {"timeInForce": "DAY"},
            "legs":       legs,
        }
        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def place_multi_leg(
        self,
        legs: list[dict],
        quantity: str,
        order_type: str = "LIMIT",
        limit_price: str = "0.01",
        order_id: str | None = None,
    ) -> dict:
        """
        Submit a multi-leg options order.

        Args: same as preflight_multi_leg, plus optional order_id.
        Returns { "orderId": "<uuid>" } on success.
        """
        account_id = self.get_account_id()
        url = f"{self._BASE}/userapigateway/trading/{account_id}/order/multi-leg"

        body = {
            "orderId":    order_id or str(uuid.uuid4()),
            "orderType":  order_type.upper(),
            "limitPrice": limit_price,
            "quantity":   quantity,
            "expiration": {"timeInForce": "DAY"},
            "legs":       legs,
        }
        logger.info(
            "Placing multi-leg order: %d legs, qty=%s, limitPrice=%s",
            len(legs), quantity, limit_price,
        )
        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Multi-leg order submitted: %s", result)
        return result
