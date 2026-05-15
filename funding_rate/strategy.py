"""
Basis / cash-and-carry arbitrage strategy configuration and helpers.

Coinbase offers dated quarterly futures (e.g., BTC-27JUN25-CDE) settled in USD.
The strategy exploits the basis (futures premium over spot) by holding:
  - Long spot (BTC-USD)
  - Short dated futures (BTC-27JUN25-CDE)

Profit = (futures_price - spot_price) at entry, locked in at expiry when
futures converge to spot (cash-settled). No funding payments; P&L is
realized in full when the contract expires.

Entry: annualized basis APR > MIN_BASIS_APR
Exit:  basis compresses below EXIT_BASIS_APR early, OR hold to expiry
"""

import re
from datetime import datetime

# Spot ticker → Coinbase futures ticker prefix
# Coinbase uses abbreviated tickers: BIT (Bitcoin), ET (Ethereum), SOL (Solana)
FUTURES_PAIRS = {
    "BTC-USD": "BIT",   # Bitcoin futures listed as BIT-DDMMMYY-CDE
    "ETH-USD": "ET",    # Ethereum futures listed as ET-DDMMMYY-CDE
    "SOL-USD": "SOL",   # Solana futures listed as SOL-DDMMMYY-CDE
}

# Basis thresholds (annualized)
MIN_BASIS_APR = 0.08    # Enter if annualized basis > 8%
EXIT_BASIS_APR = 0.03   # Exit early if basis compresses below 3%

# Position sizing
MAX_POSITION_USD = 100.00    # Max notional USD per position
MAX_SIMULTANEOUS_PAIRS = 3   # Max concurrent basis positions
MAX_PCT_BALANCE = 0.30       # Max 30% of available spot balance per position

# Exit this many days before expiry to avoid settlement slippage
DAYS_BEFORE_EXPIRY_EXIT = 2

# Regex for Coinbase dated futures product IDs
# Matches: BTC-27JUN25-CDE  or  BTC-27JUN25
FUTURES_PRODUCT_RE = re.compile(r"^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})(?:-[A-Z]+)?$")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_expiry_date(product_id: str) -> datetime | None:
    """
    Parse expiry date from a Coinbase dated futures product ID.

    E.g., "BTC-27JUN25-CDE" → datetime(2025, 6, 27)
         "BTC-27JUN25"      → datetime(2025, 6, 27)

    Returns None if the product_id format is unrecognized.
    """
    m = FUTURES_PRODUCT_RE.match(product_id)
    if not m:
        return None
    date_str = m.group(2)   # e.g., "27JUN25"
    try:
        day = int(date_str[:2])
        month_abbr = date_str[2:5]
        year = 2000 + int(date_str[5:7])
        month = MONTH_MAP.get(month_abbr)
        if month is None:
            return None
        return datetime(year, month, day)
    except (ValueError, IndexError):
        return None


def days_to_expiry(product_id: str, as_of: datetime | None = None) -> int | None:
    """
    Return calendar days remaining until expiry.

    Returns None if the product_id is not a recognized dated futures format.
    """
    expiry = parse_expiry_date(product_id)
    if expiry is None:
        return None
    ref = as_of or datetime.utcnow()
    delta = (expiry - ref).days
    return max(delta, 0)


def calc_basis_apr(spot_price: float, futures_price: float, dte: int) -> float:
    """
    Annualized basis APR = (futures - spot) / spot / dte * 365.

    Returns 0.0 if dte <= 0 (avoid division by zero near expiry).
    """
    if dte <= 0 or spot_price <= 0:
        return 0.0
    basis = (futures_price - spot_price) / spot_price
    return basis / dte * 365


def is_worth_entering(spot_price: float, futures_price: float, dte: int) -> bool:
    """Return True if annualized basis APR exceeds MIN_BASIS_APR."""
    return calc_basis_apr(spot_price, futures_price, dte) > MIN_BASIS_APR


def is_worth_exiting(spot_price: float, futures_price: float, dte: int) -> bool:
    """Return True if basis has compressed below EXIT_BASIS_APR."""
    return calc_basis_apr(spot_price, futures_price, dte) < EXIT_BASIS_APR


def near_expiry(product_id: str) -> bool:
    """Return True if within DAYS_BEFORE_EXPIRY_EXIT days of expiry."""
    dte = days_to_expiry(product_id)
    if dte is None:
        return False
    return dte <= DAYS_BEFORE_EXPIRY_EXIT
