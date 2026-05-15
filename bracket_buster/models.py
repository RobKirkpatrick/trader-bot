"""
bracket_buster/models.py

Data models for bracket_buster strategy positions and trading state.

Defines position lifecycle: open -> (closing) -> closed
Tracks pure and soft arbitrage positions separately with tier-specific P&L calculations.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Any
import uuid


@dataclass
class BracketPosition:
    """
    Represents a single bracket_buster arbitrage position.

    A position consists of:
    - Pure arb: simultaneous YES buy on lower tier + NO buy on higher tier
    - Soft arb: single-leg YES or NO position on mispriced market

    Position lifecycle: "open" -> "closing" (partial settlement) -> "closed"
    """

    # =========================================================================
    # IDENTIFIERS
    # =========================================================================

    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    """Unique position identifier (DynamoDB hash key)"""

    arb_type: str = ""  # "pure_arb" | "soft_arb"
    """Classification of arbitrage type"""

    team_name: str = ""
    """Human-readable team name (e.g., 'Kansas Jayhawks')"""

    sport: str = ""  # "NCAAMBM" (Men), "NCAABW" (Women), etc.
    """Sport classification for organizing positions"""

    # =========================================================================
    # MARKET REFERENCES
    # =========================================================================

    long_ticker: str = ""
    """Kalshi ticker for long leg (underpriced market we buy YES on)"""

    short_ticker: Optional[str] = None
    """Kalshi ticker for short leg (overpriced market we buy NO on)"""
    """None for soft_arb single-leg positions"""

    long_tier: str = ""  # "game", "sweet_sixteen", "elite_eight", "final_four", "championship"
    """Tournament tier of long leg"""

    short_tier: Optional[str] = None
    """Tournament tier of short leg (if pure_arb)"""

    # =========================================================================
    # ENTRY PRICES & SIZING
    # =========================================================================

    long_entry_price: float = 0.0
    """Entry price for long leg (YES contract USD per contract)"""

    short_entry_price: float = 0.0
    """Entry price for short leg (NO contract USD per contract)"""
    """For NO contracts: short_entry_price = 1 - yes_ask at entry time"""

    long_contracts: int = 0
    """Number of contracts bought on long leg"""

    short_contracts: int = 0
    """Number of contracts bought on short leg (if pure_arb)"""

    long_cost_basis: float = 0.0
    """Total capital deployed on long leg: long_contracts * long_entry_price"""

    short_cost_basis: float = 0.0
    """Total capital deployed on short leg: short_contracts * short_entry_price"""

    # =========================================================================
    # ORDER TRACKING
    # =========================================================================

    long_order_id: str = ""
    """Kalshi order ID for long leg"""

    short_order_id: Optional[str] = None
    """Kalshi order ID for short leg (if pure_arb)"""

    long_fill_time: Optional[str] = None
    """ISO timestamp when long order filled"""

    short_fill_time: Optional[str] = None
    """ISO timestamp when short order filled"""

    # =========================================================================
    # POSITION STATE
    # =========================================================================

    status: str = "open"  # "open" | "closing" | "closed"
    """Current position state"""

    # For pure_arb: guaranteed profit locked in at entry (should be > 0)
    # For soft_arb: expected profit based on entry price vs fair value estimate
    guaranteed_profit: float = 0.0
    """Locked-in profit (pure_arb) or expected profit (soft_arb)"""

    guaranteed_profit_pct: float = 0.0
    """Guaranteed profit as percentage of total capital deployed"""

    # =========================================================================
    # DYNAMIC PRICING & P&L TRACKING
    # =========================================================================

    long_current_price: float = 0.0
    """Current YES ask price on long leg"""

    short_current_price: float = 0.0
    """Current NO ask price on short leg"""

    pnl: float = 0.0
    """Current unrealized P&L (marked-to-market)"""

    pnl_pct: float = 0.0
    """P&L as percentage of total capital deployed"""

    realized_pnl: float = 0.0
    """Realized P&L from closed-out legs"""

    # =========================================================================
    # SOFT ARB SPECIFIC FIELDS
    # =========================================================================

    # For soft arb: store the single leg we're positioned on
    soft_arb_side: Optional[str] = None  # "yes" | "no"
    """Which side of the single-leg soft arb we're long on"""

    soft_arb_exit_price_target: float = 0.0
    """Target price to exit for profit (convergence play)"""

    soft_arb_stop_loss_price: float = 0.0
    """Stop loss level for soft arb"""

    # =========================================================================
    # TIMING
    # =========================================================================

    opened_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    """ISO timestamp when position was opened"""

    closed_at: Optional[str] = None
    """ISO timestamp when position was fully closed"""

    last_updated: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    """ISO timestamp of last P&L update"""

    # =========================================================================
    # METADATA & NOTES
    # =========================================================================

    notes: str = ""
    """Trader notes (e.g., reason for exit, market observations)"""

    alert_sent: bool = False
    """Whether SNS alert was sent for this position"""

    tags: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary metadata tags for filtering/grouping"""

    # =========================================================================
    # METHODS
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert position to DynamoDB-compatible dictionary"""
        return asdict(self)

    def is_pure_arb(self) -> bool:
        """Check if this is a pure arbitrage position"""
        return self.arb_type == "pure_arb" and self.short_ticker is not None

    def is_soft_arb(self) -> bool:
        """Check if this is a soft arbitrage position"""
        return self.arb_type == "soft_arb" and self.short_ticker is None

    def total_capital_deployed(self) -> float:
        """Sum of both legs' cost basis"""
        return self.long_cost_basis + self.short_cost_basis

    def mark_to_market(self, long_price: float, short_price: float = 0.0) -> None:
        """
        Update position P&L based on current market prices.

        Args:
            long_price: Current YES ask on long leg
            short_price: Current NO ask on short leg (0.0 if soft_arb)
        """
        self.long_current_price = long_price
        self.short_current_price = short_price
        self.last_updated = datetime.utcnow().isoformat()

        # Calculate unrealized P&L
        if self.is_pure_arb():
            # Pure arb: both legs marked to market
            long_pnl = self.long_contracts * (self.long_entry_price - long_price)
            short_pnl = self.short_contracts * (self.short_entry_price - short_price)
            self.pnl = long_pnl + short_pnl + self.realized_pnl

        else:
            # Soft arb: single leg
            if self.soft_arb_side == "yes":
                self.pnl = self.long_contracts * (self.long_entry_price - long_price) + self.realized_pnl
            elif self.soft_arb_side == "no":
                self.pnl = self.short_contracts * (self.short_entry_price - short_price) + self.realized_pnl

        # Calculate percentage return
        total_capital = self.total_capital_deployed()
        if total_capital > 0:
            self.pnl_pct = self.pnl / total_capital
        else:
            self.pnl_pct = 0.0


@dataclass
class ArbitrageOpportunity:
    """
    Represents a potential arbitrage opportunity detected by BracketAnalyzer.
    Stored in DynamoDB table "bracket-buster-opportunities" for monitor.py to execute.
    """

    opportunity_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    """Unique opportunity identifier"""

    arb_type: str = ""  # "pure_arb" | "soft_arb"
    """Type of arbitrage"""

    team_name: str = ""
    """Team involved"""

    sport: str = ""
    """Sport classification"""

    # =========================================================================
    # PURE ARB DETAILS
    # =========================================================================

    long_ticker: str = ""
    """Underpriced market (buy YES side)"""

    short_ticker: Optional[str] = None
    """Overpriced market (buy NO side) — None for soft_arb"""

    long_tier: str = ""
    """Tier of long leg"""

    short_tier: Optional[str] = None
    """Tier of short leg"""

    long_yes_ask: float = 0.0
    """YES ask price on long leg"""

    short_yes_ask: float = 0.0
    """YES ask price on short leg (will buy NO = 1 - this)"""

    # =========================================================================
    # SOFT ARB DETAILS
    # =========================================================================

    # For soft arb: which side is mispriced
    mispriced_ticker: Optional[str] = None
    """Market that is mispriced (for soft_arb)"""

    mispriced_side: Optional[str] = None  # "yes" | "no"
    """Side of soft arb to take"""

    current_price: float = 0.0
    """Current price of mispriced market"""

    fair_value_estimate: float = 0.0
    """Estimated fair value based on correlation analysis"""

    # =========================================================================
    # PROFITABILITY METRICS
    # =========================================================================

    guaranteed_profit_per_unit: float = 0.0
    """For pure_arb: guaranteed profit per contract pair"""

    expected_return_pct: float = 0.0
    """For soft_arb: expected return percentage"""

    mispricing_amount: float = 0.0
    """How far mispriced from fair value (basis points)"""

    # =========================================================================
    # EXECUTION DETAILS
    # =========================================================================

    suggested_size_usd: float = 0.0
    """Recommended position size in USD"""

    suggested_contracts: int = 0
    """Suggested number of contracts to trade"""

    confidence_score: float = 0.0
    """0-1 score of arbitrage quality"""

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    """When opportunity was detected"""

    expires_at: Optional[str] = None
    """When opportunity should be considered stale"""

    status: str = "new"  # "new" | "executing" | "executed" | "expired"
    """Execution status"""

    linked_position_id: Optional[str] = None
    """Reference to BracketPosition if executed"""

    notes: str = ""
    """Analysis notes"""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to DynamoDB-compatible dictionary"""
        return asdict(self)
