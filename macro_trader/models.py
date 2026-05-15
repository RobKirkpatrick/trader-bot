"""
Data models for the macro trader module.

Defines the MacroPosition data structure for tracking economic prediction market trades,
and MacroOpportunity for qualifying market opportunities awaiting execution.

NEW: These dataclasses provide type safety and serialization for DynamoDB storage.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Literal, Optional
import uuid


@dataclass
class MacroPosition:
    """
    Represents a single trade position in a Kalshi economic prediction market.

    Stored in DynamoDB table "macro-positions" with position_id as hash key.
    NEW: Unified position tracking allows P&L analysis and signal validation.
    """

    position_id: str                           # UUID hash key
    market_ticker: str                         # Kalshi market ticker (e.g., "KXFED_20260515")
    series: str                                # Series ID (e.g., "KXFED")
    event_description: str                     # Human-readable: "Fed Funds Rate ≥ 5.5% — May 2026"

    # Signal at entry: what macro event and sentiment triggered this trade
    signal_key: str                            # Key from news_macro output (fed_signal, etc.)
    entry_signal_value: float                  # Signal score at entry (-1.0 to +1.0)
    entry_confidence: float                    # Model confidence at entry (0.0 to 1.0)
    entry_summary: str                         # Claude's macro summary snapshot at entry

    # Trade execution details
    direction: Literal["yes", "no"]            # Which side we bought
    entry_price: float                         # Price paid (0.0 to 1.0 contract value)
    contracts: int                             # Number of contracts
    position_size_usd: float                   # Total USD deployed
    order_id: str                              # Kalshi order ID for tracking

    # Market details
    status: Literal["open", "closed"]          # Current position state
    resolution_date: str                       # ISO date when market resolves

    # Lifecycle timestamps
    opened_at: str                             # ISO-8601 when position was opened
    closed_at: Optional[str] = None            # ISO-8601 when position was closed (if closed)

    # Outcome and P&L
    pnl: float = 0.0                           # Profit/loss in USD
    outcome: Optional[Literal["won", "lost", "early_exit"]] = None  # How position ended
    exit_reason: Optional[
        Literal["resolved_win", "resolved_loss", "signal_reversal", "manual", "max_hold_days"]
    ] = None                                   # Why position was exited

    # Tracking and audit
    last_updated: str = ""                     # ISO-8601 last modification timestamp

    @classmethod
    def create(
        cls,
        market_ticker: str,
        series: str,
        event_description: str,
        signal_key: str,
        entry_signal_value: float,
        entry_confidence: float,
        entry_summary: str,
        direction: str,
        entry_price: float,
        contracts: int,
        position_size_usd: float,
        order_id: str,
        resolution_date: str,
    ) -> "MacroPosition":
        """Factory method to create a new MacroPosition with initialized timestamps."""
        now = datetime.utcnow().isoformat() + "Z"
        return cls(
            position_id=str(uuid.uuid4()),
            market_ticker=market_ticker,
            series=series,
            event_description=event_description,
            signal_key=signal_key,
            entry_signal_value=entry_signal_value,
            entry_confidence=entry_confidence,
            entry_summary=entry_summary,
            direction=direction,
            entry_price=entry_price,
            contracts=contracts,
            position_size_usd=position_size_usd,
            order_id=order_id,
            status="open",
            resolution_date=resolution_date,
            opened_at=now,
            last_updated=now,
        )

    def close(
        self,
        pnl: float,
        outcome: Literal["won", "lost", "early_exit"],
        exit_reason: str,
    ) -> None:
        """Mark position as closed and record final P&L."""
        now = datetime.utcnow().isoformat() + "Z"
        self.status = "closed"
        self.pnl = pnl
        self.outcome = outcome
        self.exit_reason = exit_reason
        self.closed_at = now
        self.last_updated = now

    def to_dynamodb_item(self) -> dict:
        """Convert to DynamoDB-compatible dict for storage."""
        item = asdict(self)
        # Ensure numeric values are float/int (not Decimal from boto3)
        for key in ["entry_signal_value", "entry_confidence", "entry_price",
                    "position_size_usd", "pnl", "contracts"]:
            if key in item and item[key] is not None:
                item[key] = float(item[key]) if key != "contracts" else int(item[key])
        return item


@dataclass
class MacroOpportunity:
    """
    Represents a trading opportunity awaiting execution.

    Stored in DynamoDB table "macro-opportunities" as a staging area between
    scanner (discovery) and monitor (execution).
    NEW: Separates opportunity discovery from execution to allow manual review/filtering.
    """

    opportunity_id: str                        # UUID hash key
    market_ticker: str                         # Kalshi market ticker
    series: str                                # Series ID
    event_description: str                     # Human-readable event description

    # Signal that triggered this opportunity
    signal_key: str                            # "fed_signal", "inflation_signal", etc.
    signal_value: float                        # Signal score (-1.0 to +1.0)
    signal_confidence: float                   # Model confidence (0.0 to 1.0)
    signal_summary: str                        # Claude's macro snapshot

    # Market details at discovery
    direction: Literal["yes", "no"]            # Which side to buy based on signal
    implied_probability: float                 # Signal-derived probability for YES
    market_yes_price: float                    # Current Kalshi YES price
    edge: float                                # implied_probability - market_yes_price

    # Execution planning
    status: Literal["pending", "executed", "skipped", "expired"]  # Opportunity state
    max_contracts: int                         # How many we could buy with available capital
    recommended_contracts: int                 # Suggested position size

    # Timing
    scanned_at: str                            # ISO-8601 when opportunity was discovered
    expires_at: str                            # Opportunity expires if not taken by this time
    resolution_date: str                       # ISO date when market resolves

    # Audit
    executed_position_id: Optional[str] = None # Links to MacroPosition if executed
    skip_reason: Optional[str] = None          # Why opportunity was skipped

    @classmethod
    def create(
        cls,
        market_ticker: str,
        series: str,
        event_description: str,
        signal_key: str,
        signal_value: float,
        signal_confidence: float,
        signal_summary: str,
        direction: str,
        implied_probability: float,
        market_yes_price: float,
        edge: float,
        max_contracts: int,
        recommended_contracts: int,
        resolution_date: str,
    ) -> "MacroOpportunity":
        """Factory method to create a new MacroOpportunity."""
        from datetime import timedelta

        now = datetime.utcnow().isoformat() + "Z"
        expires_at = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"

        return cls(
            opportunity_id=str(uuid.uuid4()),
            market_ticker=market_ticker,
            series=series,
            event_description=event_description,
            signal_key=signal_key,
            signal_value=signal_value,
            signal_confidence=signal_confidence,
            signal_summary=signal_summary,
            direction=direction,
            implied_probability=implied_probability,
            market_yes_price=market_yes_price,
            edge=edge,
            status="pending",
            max_contracts=max_contracts,
            recommended_contracts=recommended_contracts,
            scanned_at=now,
            expires_at=expires_at,
            resolution_date=resolution_date,
        )

    def to_dynamodb_item(self) -> dict:
        """Convert to DynamoDB-compatible dict for storage."""
        item = asdict(self)
        for key in ["signal_value", "signal_confidence", "implied_probability",
                    "market_yes_price", "edge", "max_contracts", "recommended_contracts"]:
            if key in item and item[key] is not None:
                if key in ["max_contracts", "recommended_contracts"]:
                    item[key] = int(item[key])
                else:
                    item[key] = float(item[key])
        return item
