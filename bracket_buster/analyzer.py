"""
bracket_buster/analyzer.py

Core analysis engine for detecting tournament arbitrage opportunities.

The BracketAnalyzer detects both pure and soft arbitrage by:
1. Grouping markets by team and tournament tier
2. Checking probability hierarchy violations (pure arb)
3. Comparing prices to historical correlation models (soft arb)
4. Calculating optimal position sizes within risk constraints
"""

import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

from . import strategy
from .models import ArbitrageOpportunity

logger = logging.getLogger(__name__)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def extract_team_names(market_title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract team names from Kalshi market title.

    Common formats:
    - "Kansas vs Duke - Winner"
    - "Kansas Jayhawks beats Duke Blue Devils"
    - "Kansas to beat Duke"

    Returns:
        (team1, team2) tuple with team names, or (None, None) if parsing fails
    """
    # Strip common Kalshi suffixes before parsing ("Winner?", "Wins?", etc.)
    title_clean = re.sub(r'\s+(?:winner\??|wins?\??)\s*$', '', market_title, flags=re.IGNORECASE).strip()
    title_lower = title_clean.lower()

    # Try "Team1 vs Team2" pattern
    vs_match = re.search(r"^(.+?)\s+(?:vs\.?|v\.?)\s+(.+?)(?:\s*-|$)", title_lower)
    if vs_match:
        team1 = vs_match.group(1).strip().title()
        team2 = vs_match.group(2).strip().title()
        return (team1, team2)

    # Try "Team1 at Team2" pattern (away at home — common Kalshi format)
    at_match = re.search(r"^(.+?)\s+at\s+(.+?)(?:\s*-|$)", title_lower)
    if at_match:
        team1 = at_match.group(1).strip().title()
        team2 = at_match.group(2).strip().title()
        return (team1, team2)

    # Try "Team1 to beat Team2" / "Team1 beats Team2" patterns
    beat_match = re.search(r"^(.+?)\s+(?:to\s+)?beats?\s+(.+?)(?:\s*-|$)", title_lower)
    if beat_match:
        team1 = beat_match.group(1).strip().title()
        team2 = beat_match.group(2).strip().title()
        return (team1, team2)

    if " beats " in title_lower or " beat " in title_lower:
        parts = re.split(r"\s+beats?\s+", title_lower, maxsplit=1)
        if len(parts) == 2:
            team1 = parts[0].strip().title()
            team2 = parts[1].strip().title()
            return (team1, team2)

    logger.warning(f"Could not parse team names from title: {market_title}")
    return (None, None)


def classify_tournament_tier(market_ticker: str, market_title: str) -> Optional[str]:
    """
    Classify market into tournament tier based on ticker series code (primary)
    with keyword fallback.

    The series code embedded in the ticker is the definitive source:
      KXNCAAMBGAME*   → game
      KXNCAAMBS16*    → sweet_sixteen
      KXNCAAMBE8*     → elite_eight
      KXNCAAMBF4*     → final_four
      KXNCAAMBCHAMP*  → championship

    Args:
        market_ticker: Kalshi market ticker code
        market_title: Human-readable market title

    Returns:
        Tier string from strategy.TOURNAMENT_TIER, or None if not classifiable
    """
    ticker_upper = market_ticker.upper()

    # Derive tier from known TOURNAMENT_SERIES series codes (most reliable)
    _SERIES_TO_TIER = {
        "CHAMP":  "championship",
        "F4":     "final_four",
        "E8":     "elite_eight",
        "S16":    "sweet_sixteen",
        "GAME":   "game",
    }
    for suffix, tier in _SERIES_TO_TIER.items():
        # Series code is the part before the first dash
        series_part = ticker_upper.split("-")[0]
        if series_part.endswith(suffix):
            return tier

    # Fallback: keyword scan on title only (not combined with ticker to avoid false "winner" match)
    title_lower = market_title.lower()
    for tier, keywords in strategy.TIER_KEYWORDS.items():
        for keyword in keywords:
            # Skip short keywords that cause false matches ("winner" → championship)
            if len(keyword) >= 5 and keyword in title_lower:
                return tier

    return None


def extract_single_team(team_names: Tuple[Optional[str], Optional[str]]) -> Optional[str]:
    """
    If market is about a single team (e.g., "Team A to make Final Four"),
    extract that team. Returns None if it's a head-to-head game.

    Heuristic: if one team appears in TOURNAMENT_TIER market, use that.
    """
    team1, team2 = team_names
    if team1 and not team2:
        return team1
    if team2 and not team1:
        return team2
    # Both teams present — likely a game market
    return None


# ============================================================================
# BRACKET ANALYZER CLASS
# ============================================================================


class BracketAnalyzer:
    """
    Analyzes Kalshi markets for tournament arbitrage opportunities.

    Maintains team-market mapping and detects both pure and soft arbitrages.
    """

    def __init__(self, kalshi_client=None, historical_data: Optional[Dict] = None):
        """
        Initialize the analyzer.

        Args:
            kalshi_client: Optional KalshiClient for live price fetches
            historical_data: Optional dict mapping (team, tier1, tier2) -> correlation
        """
        self.kalshi_client = kalshi_client
        self.historical_data = historical_data or {}
        self.team_market_map: Dict[str, Dict] = {}

    def build_team_market_map(self, markets: List[Dict]) -> Dict[str, Dict]:
        """
        Group Kalshi markets by team and classify by tournament tier.

        Args:
            markets: List of market dicts from Kalshi API with fields:
                - ticker
                - title
                - subtitle (optional)
                - yes_ask, yes_bid
                - no_ask, no_bid
                - volume_24h (optional)

        Returns:
            {team_name: {tier: {ticker, yes_ask, yes_bid, no_ask, no_bid, ...}}}

        Example output:
            {
                "Kansas Jayhawks": {
                    "game": {
                        "ticker": "KXNCAAMBGAME_KU_DUKE",
                        "yes_ask": 0.55,
                        "yes_bid": 0.53,
                        ...
                    },
                    "sweet_sixteen": {
                        "ticker": "KXNCAAMBS16_KU",
                        "yes_ask": 0.72,
                        ...
                    }
                }
            }
        """
        self.team_market_map = {}

        for market in markets:
            ticker = market.get("ticker", "")
            title = market.get("title", "")
            subtitle = market.get("subtitle", "")

            # Classify tier
            tier = classify_tournament_tier(ticker, title + " " + subtitle)
            if not tier:
                logger.debug(f"Skipping market without tournament tier: {ticker} {title}")
                continue

            # Extract team name(s)
            team_names = extract_team_names(title)
            team_name = extract_single_team(team_names)

            if not team_name:
                # Head-to-head game — use first team as primary
                team_name, _ = team_names
                if not team_name:
                    logger.debug(f"Could not extract team names from: {title}")
                    continue

            # Initialize team entry if needed
            if team_name not in self.team_market_map:
                self.team_market_map[team_name] = {}

            # Store market in tier
            self.team_market_map[team_name][tier] = {
                "ticker": ticker,
                "yes_ask": float(market.get("yes_ask", 0.0)),
                "yes_bid": float(market.get("yes_bid", 0.0)),
                "no_ask": float(market.get("no_ask", 1.0)),
                "no_bid": float(market.get("no_bid", 1.0)),
                "title": title,
                "subtitle": subtitle,
                "volume_24h": float(market.get("volume_24h", 0.0)),
                "liquidity_score": self._calculate_liquidity_score(market),
            }

        logger.info(f"Built market map for {len(self.team_market_map)} teams")
        return self.team_market_map

    def find_pure_arb(self, team_markets: Optional[Dict] = None) -> List[ArbitrageOpportunity]:
        """
        Detect pure arbitrage opportunities by checking probability hierarchy.

        Pure arb exists when a lower-tier market prices higher than a higher-tier market.

        Example:
            - Championship YES ask: 60%
            - Game win YES ask: 55%
            - Buy YES at 55%, buy NO at 40% (= 1 - 60%)
            - Guaranteed profit: 1 - 0.55 - 0.40 = 0.05 per pair

        Args:
            team_markets: Optional override of self.team_market_map

        Returns:
            List of ArbitrageOpportunity objects with arb_type="pure_arb"
        """
        if team_markets is None:
            team_markets = self.team_market_map

        opportunities = []

        for team_name, tiers in team_markets.items():
            # Sort tiers by hierarchy
            tier_items = sorted(tiers.items(), key=lambda x: strategy.TOURNAMENT_TIER[x[0]])

            # Check all pairs: lower tier vs higher tier
            for i, (low_tier, low_market) in enumerate(tier_items):
                for high_tier, high_market in tier_items[i + 1 :]:
                    # Low tier price should be <= high tier price
                    low_price = low_market["yes_ask"]
                    high_price = high_market["yes_ask"]

                    # Check for violation (pure arb signal)
                    if low_price < high_price:
                        # This is the correct hierarchy, skip
                        continue

                    # Violation detected!
                    # Buy YES on lower tier (cheaper), buy NO on higher tier (overpriced)
                    long_price = low_price
                    short_price = 1.0 - high_price  # Price to buy NO contract

                    guaranteed_profit = 1.0 - long_price - short_price
                    guaranteed_profit_pct = guaranteed_profit / (long_price + short_price) if (
                        long_price + short_price
                    ) > 0 else 0

                    # Filter by minimum spread
                    if guaranteed_profit < strategy.PURE_ARB_MIN_SPREAD:
                        logger.debug(
                            f"Arb profit below threshold for {team_name} "
                            f"({low_tier} vs {high_tier}): {guaranteed_profit:.4f}"
                        )
                        continue

                    # Filter by liquidity
                    if not self._check_liquidity(low_market, high_market):
                        logger.debug(f"Insufficient liquidity for {team_name}")
                        continue

                    # Create opportunity
                    opp = ArbitrageOpportunity(
                        arb_type="pure_arb",
                        team_name=team_name,
                        sport=self._extract_sport_from_ticker(low_market["ticker"]),
                        long_ticker=low_market["ticker"],
                        short_ticker=high_market["ticker"],
                        long_tier=low_tier,
                        short_tier=high_tier,
                        long_yes_ask=long_price,
                        short_yes_ask=high_price,
                        guaranteed_profit_per_unit=guaranteed_profit,
                        expected_return_pct=guaranteed_profit_pct,
                        confidence_score=self._calculate_confidence(low_market, high_market),
                        notes=(
                            f"Probability hierarchy violation: {low_tier} YES at "
                            f"{low_price:.2%} > {high_tier} YES at {high_price:.2%}"
                        ),
                    )

                    opportunities.append(opp)
                    logger.info(
                        f"Pure arb found: {team_name} - guaranteed profit per unit: "
                        f"${guaranteed_profit:.4f} ({guaranteed_profit_pct:.1%})"
                    )

        return opportunities

    def find_soft_arb(self, team_markets: Optional[Dict] = None) -> List[ArbitrageOpportunity]:
        """
        Detect soft arbitrage opportunities via statistical mispricing.

        Soft arb compares current prices to historical correlation model.
        If a market is sufficiently mispriced vs correlation, take that single leg.

        Example:
            - Team has 55% implied win probability for next game
            - Historical: teams with 55% next-game odds reach championship ~15% of the time
            - Championship market prices at 30% → OVERPRICED
            - Take soft arb: BUY NO on championship

        Args:
            team_markets: Optional override of self.team_market_map

        Returns:
            List of ArbitrageOpportunity objects with arb_type="soft_arb"
        """
        if team_markets is None:
            team_markets = self.team_market_map

        opportunities = []

        for team_name, tiers in team_markets.items():
            # Start with game-level probability (lowest tier = most fundamental)
            if "game" not in tiers:
                continue

            game_market = tiers["game"]
            game_win_prob = game_market["yes_ask"]

            # Check each higher tier against expected probability
            for tier in ["sweet_sixteen", "elite_eight", "final_four", "championship"]:
                if tier not in tiers:
                    continue

                tier_market = tiers[tier]
                current_price = tier_market["yes_ask"]

                # Estimate fair value using correlation model
                fair_value = self._estimate_fair_value(team_name, tier, game_win_prob)

                if fair_value is None:
                    continue

                # Calculate mispricing
                mispricing = abs(current_price - fair_value)
                mispricing_pct = mispricing / fair_value if fair_value > 0 else 0

                # Filter by minimum mispricing threshold
                if mispricing_pct < strategy.SOFT_ARB_MIN_MISPRICING:
                    continue

                # Determine which side is mispriced
                if current_price > fair_value:
                    # Market is overpriced — BUY NO
                    side = "no"
                    mispriced_side = "no"
                else:
                    # Market is underpriced — BUY YES
                    side = "yes"
                    mispriced_side = "yes"

                # Filter by liquidity
                if not self._check_liquidity(tier_market, min_depth=strategy.MIN_ORDER_BOOK_DEPTH):
                    continue

                # Create opportunity
                opp = ArbitrageOpportunity(
                    arb_type="soft_arb",
                    team_name=team_name,
                    sport=self._extract_sport_from_ticker(tier_market["ticker"]),
                    long_ticker=tier_market["ticker"],
                    short_ticker=None,  # Single-leg position
                    long_tier=tier,
                    mispriced_ticker=tier_market["ticker"],
                    mispriced_side=mispriced_side,
                    current_price=current_price,
                    fair_value_estimate=fair_value,
                    expected_return_pct=mispricing_pct,
                    confidence_score=self._calculate_soft_arb_confidence(
                        mispricing_pct, game_win_prob
                    ),
                    notes=(
                        f"Soft arb: {tier} priced at {current_price:.2%}, "
                        f"fair value ~{fair_value:.2%} based on {game_win_prob:.2%} game odds"
                    ),
                )

                opportunities.append(opp)
                logger.info(
                    f"Soft arb found: {team_name} {tier} - "
                    f"current: {current_price:.2%}, fair value: {fair_value:.2%}, "
                    f"side: {side}"
                )

        return opportunities

    def calculate_position_sizes(
        self,
        opp: ArbitrageOpportunity,
        available_balance: float,
        existing_positions_capital: float = 0.0,
    ) -> Dict[str, float]:
        """
        Calculate optimal position sizing for an opportunity.

        Respects constraints:
        - MAX_POSITION_PER_MARKET
        - MAX_PCT_BANKROLL_PER_POSITION
        - MIN_EXPECTED_RETURN_PCT

        Args:
            opp: ArbitrageOpportunity object
            available_balance: Available trading capital (USD)
            existing_positions_capital: Capital already deployed in existing positions

        Returns:
            {"long_size_usd": X, "short_size_usd": Y, "total_size_usd": Z, "num_contracts": N}
        """
        # Calculate max size based on market constraint
        max_size_market = strategy.MAX_POSITION_PER_MARKET

        # Calculate max size based on bankroll constraint
        remaining_bankroll = available_balance - existing_positions_capital
        max_size_bankroll = remaining_bankroll * strategy.MAX_PCT_BANKROLL_PER_POSITION

        # Use the more conservative constraint
        max_size = min(max_size_market, max_size_bankroll, remaining_bankroll * 0.5)

        if max_size <= 0:
            logger.warning("Insufficient available balance for position")
            return {
                "long_size_usd": 0.0,
                "short_size_usd": 0.0,
                "total_size_usd": 0.0,
                "num_contracts": 0,
            }

        # For pure arb: size both legs proportionally to prices
        if opp.arb_type == "pure_arb":
            long_price = opp.long_yes_ask
            short_price = 1.0 - opp.short_yes_ask

            # Equal notional exposure on both sides
            long_size = max_size * 0.5
            short_size = max_size * 0.5

            long_contracts = int(long_size / long_price)
            short_contracts = int(short_size / short_price)

            # Use the smaller to keep equal exposure
            num_contracts = min(long_contracts, short_contracts)

            return {
                "long_size_usd": num_contracts * long_price,
                "short_size_usd": num_contracts * short_price,
                "total_size_usd": (num_contracts * long_price) + (num_contracts * short_price),
                "num_contracts": num_contracts,
            }

        else:
            # For soft arb: single-leg position
            price = opp.current_price if opp.mispriced_side == "yes" else (1.0 - opp.current_price)
            num_contracts = int(max_size / price)

            return {
                "long_size_usd": num_contracts * price,
                "short_size_usd": 0.0,
                "total_size_usd": num_contracts * price,
                "num_contracts": num_contracts,
            }

    # ========================================================================
    # PRIVATE HELPER METHODS
    # ========================================================================

    def _calculate_liquidity_score(self, market: Dict) -> float:
        """Calculate 0-1 liquidity score for a market"""
        # Simple heuristic: volume-based
        volume = market.get("volume_24h", 0.0)
        bid_ask_spread = (
            market.get("no_ask", 1.0) - market.get("yes_bid", 0.0)
        )  # Approximate spread

        if volume == 0 or bid_ask_spread > 0.05:
            return 0.0

        return min(1.0, volume / 100.0)  # Normalize to 1.0 at $100 volume

    def _check_liquidity(self, market1: Dict, market2: Dict = None, min_depth: float = None) -> bool:
        """
        Check if market(s) have sufficient liquidity.

        Args:
            market1: First market dict
            market2: Optional second market for pure arb
            min_depth: Minimum order book depth (USD) — uses MIN_ORDER_BOOK_DEPTH if None

        Returns:
            True if liquidity is sufficient
        """
        if min_depth is None:
            min_depth = strategy.MIN_ORDER_BOOK_DEPTH

        if self._calculate_liquidity_score(market1) < 0.1:
            return False

        if market2 and self._calculate_liquidity_score(market2) < 0.1:
            return False

        return True

    def _estimate_fair_value(self, team_name: str, tier: str, game_win_prob: float) -> Optional[float]:
        """
        Estimate fair value for a tier market using historical correlation.

        Simple model:
        - Championship: game_prob ^ 6 (need to win 6 games)
        - Final Four: game_prob ^ 4
        - Elite Eight: game_prob ^ 3
        - Sweet Sixteen: game_prob ^ 2

        Args:
            team_name: Team name
            tier: Tournament tier
            game_win_prob: Probability team wins next game

        Returns:
            Estimated fair value as probability (0-1), or None if unable to estimate
        """
        # Check historical data first
        key = (team_name, tier)
        if key in self.historical_data:
            # Adjust historical data by game win prob ratio
            historical_prob = self.historical_data[key]
            return historical_prob  # Simplified: use as-is

        # Fallback: simple model using exponentiation
        exponent = {
            "championship": 6,
            "final_four": 4,
            "elite_eight": 3,
            "sweet_sixteen": 2,
            "game": 1,
        }

        exp = exponent.get(tier, 1)
        if exp <= 1:
            return None

        # Fair value = game_prob ^ exponent
        fair_value = game_win_prob ** exp
        return fair_value

    def _calculate_confidence(self, market1: Dict, market2: Dict) -> float:
        """
        Calculate confidence score for pure arb (0-1).

        Higher confidence if:
        - Both markets liquid
        - Spread is large
        - Recent volume
        """
        liq1 = self._calculate_liquidity_score(market1)
        liq2 = self._calculate_liquidity_score(market2)
        avg_liq = (liq1 + liq2) / 2.0

        return min(1.0, avg_liq + 0.3)  # Add baseline confidence

    def _calculate_soft_arb_confidence(self, mispricing_pct: float, game_win_prob: float) -> float:
        """
        Calculate confidence score for soft arb.

        Higher if mispricing is larger and game_win_prob is not extreme.
        """
        # Mispricing confidence: larger gap = higher confidence
        mispricing_confidence = min(1.0, mispricing_pct * 2)

        # Extremeness penalty: avoid trading near 0% or 100%
        extremeness_penalty = 0.0
        if game_win_prob < 0.25 or game_win_prob > 0.75:
            extremeness_penalty = 0.2

        return max(0.0, mispricing_confidence - extremeness_penalty)

    def _extract_sport_from_ticker(self, ticker: str) -> str:
        """Extract sport code from Kalshi ticker"""
        if "NCAAMB" in ticker:
            return "NCAAMB"  # Basketball
        if "NCAAFB" in ticker:
            return "NCAAFB"  # Football
        if "NCAA" in ticker:
            return "NCAA"
        return "UNKNOWN"
