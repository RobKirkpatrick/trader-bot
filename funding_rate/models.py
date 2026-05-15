"""
Data models for basis / cash-and-carry arbitrage positions and opportunities.

Tracks both legs of the trade (long spot + short dated futures), entry details,
sizing, and realized P&L. Stored in DynamoDB.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Any


@dataclass
class BasisOpportunity:
    """
    Represents a potential basis arb opportunity identified by the scanner.

    The monitor converts pending opportunities into BasisPositions after
    re-verifying the basis APR is still attractive.
    """

    spot_ticker: str            # e.g., "BTC-USD"
    futures_ticker: str         # e.g., "BTC-27JUN25-CDE"
    scanned_at: int             # Unix timestamp (DynamoDB range key)
    spot_price: float           # Spot mid price at scan time
    futures_price: float        # Futures mid price at scan time
    basis_apr: float            # Annualized basis APR at scan time
    days_to_expiry: int         # Calendar days until futures expiry
    expiry_date: str            # ISO date string, e.g., "2025-06-27"
    status: str = "pending"     # "pending" | "executed" | "stale"


@dataclass
class BasisPosition:
    """
    Represents an active or closed basis arb position.

    Both legs:
      - Long spot (BTC-USD): held until early exit or near-expiry close
      - Short dated futures (BTC-27JUN25-CDE): auto-settles at expiry

    P&L is locked in at entry as (futures_price - spot_price) × quantity.
    No intra-period cash flows — profit realized fully at expiry.
    """

    position_id: str            # UUID (DynamoDB hash key)
    spot_ticker: str            # e.g., "BTC-USD"
    futures_ticker: str         # e.g., "BTC-27JUN25-CDE"
    expiry_date: str            # ISO date string, e.g., "2025-06-27"
    days_to_expiry: int         # DTE at entry

    # Entry prices
    entry_spot_price: float     # Spot ask at entry fill
    entry_futures_price: float  # Futures bid at entry fill
    entry_basis_apr: float      # Annualized basis at entry

    # Position sizing
    notional_usd: float         # USD notional per leg
    spot_quantity: float        # Units of spot asset held (e.g., 0.001 BTC)

    # Expected P&L locked in at entry
    expected_basis_usd: float   # (futures_price - spot_price) × spot_quantity

    # Order IDs
    spot_order_id: str
    futures_order_id: str

    # Lifecycle
    status: str                         # "open" | "closing" | "closed"
    opened_at: str                      # ISO-8601 timestamp
    closed_at: Optional[str] = None     # ISO-8601 timestamp when closed
    exit_reason: Optional[str] = None   # "near_expiry" | "basis_compressed" | "manual"

    # P&L (set on close)
    realized_pnl: float = 0.0

    # Metadata
    last_updated: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for DynamoDB (converts floats to Decimal-safe strings)."""
        d = asdict(self)
        # DynamoDB doesn't accept Python floats in all SDK versions; stringify them
        for key in (
            "entry_spot_price", "entry_futures_price", "entry_basis_apr",
            "notional_usd", "spot_quantity", "expected_basis_usd", "realized_pnl",
        ):
            if key in d and d[key] is not None:
                d[key] = str(d[key])
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BasisPosition":
        """Reconstruct from DynamoDB item (parses stringified floats back)."""
        d = dict(data)
        for key in (
            "entry_spot_price", "entry_futures_price", "entry_basis_apr",
            "notional_usd", "spot_quantity", "expected_basis_usd", "realized_pnl",
        ):
            if key in d and d[key] is not None:
                d[key] = float(d[key])
        if "days_to_expiry" in d:
            d["days_to_expiry"] = int(d["days_to_expiry"])
        return cls(**d)


# Aliases so lambda_handlers.py (which imports FundingOpportunity / FundingPosition) still works
FundingOpportunity = BasisOpportunity
FundingPosition = BasisPosition
