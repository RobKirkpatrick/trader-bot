# Public.com API Reference

All requests go to `https://api.public.com`. Authentication uses the API secret
generated at Public.com → Account Settings → Security → API.

The `PublicClient` in `broker/public_client.py` handles auth, token refresh,
and all endpoint calls. Scripts import from there — do not call the API directly.

---

## Authentication

Public uses short-lived JWT tokens exchanged from your API secret:

```
POST /userapiauthservice/personal/access-tokens
Authorization: Bearer <PUBLIC_API_SECRET>
```

Returns a token valid for ~60 minutes. `PublicClient` refreshes automatically.

---

## Portfolio & Account

```python
client.get_account_balance()
# → {"cash_balance": "1234.56", "buying_power": "1234.56", "equity": "1500.00"}

client.get_positions()
# → list of positions, each with:
#   position["instrument"]["symbol"]    ← ticker (stocks) or OSI symbol (options)
#   position["quantity"]                ← shares / contracts
#   position["costBasis"]["unitCost"]   ← avg cost per share (costBasis is a DICT)
```

---

## Market Data

```python
client.get_quotes(["AAPL", "MSFT"])
# POST /userapigateway/marketdata/{accountId}/quotes
# Body: {"instruments": [{"symbol": "AAPL", "type": "EQUITY"}, ...]}
# → {"quotes": [{"instrument": {"symbol": "AAPL"}, "last": "185.50", "bid": ..., "ask": ...}]}

client.get_option_expirations("AAPL")
# → ["2026-03-21", "2026-03-28", ...]

client.get_option_chain("AAPL", "2026-04-17", option_type="CALL")
# → [{"optionSymbol": "AAPL260417C00185000", "strikePrice": "185", "bid": "2.50", "ask": "2.60", ...}]

client.get_option_greeks("AAPL260417C00185000")
# → {"greeks": [{"symbol": ..., "greeks": {"delta": 0.52, "gamma": ..., "impliedVolatility": 0.31}}]}
```

---

## Orders

```python
# Stock buy by dollar amount (fractional)
client.place_order("AAPL", "BUY", order_type="MARKET", amount="50.00")

# Stock sell by quantity
client.place_order("AAPL", "SELL", order_type="MARKET", quantity="0.5")

# Preflight (cost estimate — always run before placing)
client.preflight_order("AAPL", "BUY", amount="50.00")
# → {"estimatedCost": "50.00", "buyingPowerRequirement": "50.00"}

# Options buy — LIMIT required (MARKET rejected by Public for options)
client.place_options_order(
    option_symbol="AAPL260417C00185000",
    side="BUY",
    quantity="1",
    order_type="LIMIT",
    limit_price="2.55",   # typically mid of bid/ask
)

# Options sell — LIMIT required, use current bid as floor
client.place_options_order(
    option_symbol="AAPL260417C00185000",
    side="SELL",
    quantity="1",
    order_type="LIMIT",
    limit_price="2.30",   # fetch via get_option_chain → bid field
)

# Multi-leg (spreads)
legs = [
    PublicClient.make_option_leg("SPY", "PUT", "580.00", "2026-04-17", "BUY", "OPEN", ratio=1),
    PublicClient.make_option_leg("SPY", "PUT", "568.00", "2026-04-17", "SELL", "OPEN", ratio=1),
]
client.place_multi_leg(legs=legs, quantity="1", order_type="LIMIT", limit_price="1.50")

# Order status
client.get_order(order_id)
# → {"status": "FILLED", "orderId": "...", ...}
```

---

## OSI Option Symbol Format

```
AAPL260417C00185000
│   │     │ │
│   │     │ └── Strike × 1000, 8 digits (185.00 → 00185000)
│   │     └──── C = Call, P = Put
│   └────────── Expiration YYMMDD (April 17, 2026 → 260417)
└────────────── Underlying symbol
```

Parse: `re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', osi_symbol)`

Strike in dollars: `int(strike_raw) / 1000.0`

---

## Error Handling

- `400` on options orders → usually wrong `order_type` (use LIMIT, not MARKET)
- `openCloseIndicator` required for options: `"OPEN"` (buy) or `"CLOSE"` (sell)
- Token expiry → `PublicClient` auto-refreshes, retry once on 401
- Rate limit: 10 req/sec — scripts include backoff on 429

---

## Kalshi API (Carpet Bagger)

```
Base URL: https://trading-api.kalshi.com/trade-api/v2
Auth: RSA-SHA256 signed requests (key stored as KALSHI_RSA_PRIVATE_KEY env var)

GET  /markets?series_ticker=KXNCAAMBGAME&status=open
GET  /markets/{market_ticker}/orderbook
POST /portfolio/orders   {"ticker": ..., "action": "buy", "type": "limit", ...}
GET  /portfolio/fills
GET  /portfolio/balance
```

See `carpet_bagger/kalshi_client.py` for implementation.
