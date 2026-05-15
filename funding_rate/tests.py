"""
NEW: Unit and integration tests for funding_rate module.

Run with: pytest tests.py -v
"""

import pytest
from datetime import datetime
from funding_rate import strategy
from funding_rate.models import FundingPosition, FundingOpportunity


# ============================================================================
# Strategy Tests
# ============================================================================

class TestStrategy:
    """Test strategy.py helper functions."""

    def test_annualize_funding_rate_zero(self):
        """Zero 8hr rate should annualize to ~zero."""
        apr = strategy.annualize_funding_rate(0.0)
        assert apr < 0.001

    def test_annualize_funding_rate_typical(self):
        """0.03% per 8h should annualize to ~32.85%."""
        rate_8hr = 0.0003
        apr = strategy.annualize_funding_rate(rate_8hr)
        # (1.0003)^(3*365) - 1 ≈ 0.328
        assert 0.30 < apr < 0.35

    def test_annualize_funding_rate_high(self):
        """0.1% per 8h should annualize to ~130%+."""
        rate_8hr = 0.001
        apr = strategy.annualize_funding_rate(rate_8hr)
        assert apr > 1.0

    def test_is_worth_entering_below_threshold(self):
        """Should not enter if rate below MIN_FUNDING_APR."""
        # 0.01% per 8h ≈ 1% APR
        assert not strategy.is_worth_entering(0.00001)

    def test_is_worth_entering_above_threshold(self):
        """Should enter if rate above MIN_FUNDING_APR."""
        # 0.03% per 8h ≈ 32.85% APR > 10%
        assert strategy.is_worth_entering(0.0003)

    def test_is_worth_exiting_above_threshold(self):
        """Should not exit if rate above EXIT_FUNDING_APR."""
        # 0.03% per 8h ≈ 32.85% APR > 5%
        assert not strategy.is_worth_exiting(0.0003)

    def test_is_worth_exiting_below_threshold(self):
        """Should exit if rate below EXIT_FUNDING_APR."""
        # 0.01% per 8h ≈ 1% APR < 5%
        assert strategy.is_worth_exiting(0.00001)

    def test_perp_pairs_defined(self):
        """PERP_PAIRS should have BTC and ETH at minimum."""
        assert "BTC-PERP-INTX" in strategy.PERP_PAIRS
        assert "ETH-PERP-INTX" in strategy.PERP_PAIRS
        assert strategy.PERP_PAIRS["BTC-PERP-INTX"] == "BTC-USD"
        assert strategy.PERP_PAIRS["ETH-PERP-INTX"] == "ETH-USD"


# ============================================================================
# Model Tests
# ============================================================================

class TestModels:
    """Test data models."""

    def test_funding_opportunity_creation(self):
        """FundingOpportunity should initialize correctly."""
        opp = FundingOpportunity(
            perp_ticker="BTC-PERP-INTX",
            spot_ticker="BTC-USD",
            scanned_at=datetime.utcnow().isoformat(),
            funding_rate_8hr=0.0003,
            funding_apr=0.33,
        )
        assert opp.perp_ticker == "BTC-PERP-INTX"
        assert opp.spot_ticker == "BTC-USD"
        assert opp.status == "pending"
        assert opp.spot_price is None

    def test_funding_position_creation(self):
        """FundingPosition should initialize correctly."""
        pos = FundingPosition(
            position_id="test-123",
            perp_ticker="BTC-PERP-INTX",
            spot_ticker="BTC-USD",
            entry_spot_price=83000.0,
            entry_perp_price=83050.0,
            entry_funding_rate_8hr=0.0003,
            entry_funding_apr=0.33,
            notional_usd=100.0,
            spot_quantity=0.001,
            perp_contracts=0.001,
            spot_order_id="spot-order-1",
            perp_order_id="perp-order-1",
            opened_at=datetime.utcnow().isoformat(),
        )
        assert pos.position_id == "test-123"
        assert pos.status == "open"
        assert pos.funding_collected_usd == 0.0
        assert pos.funding_payments_count == 0

    def test_funding_position_to_dict(self):
        """FundingPosition should serialize to dict."""
        pos = FundingPosition(
            position_id="test-123",
            perp_ticker="BTC-PERP-INTX",
            spot_ticker="BTC-USD",
            entry_spot_price=83000.0,
            entry_perp_price=83050.0,
            entry_funding_rate_8hr=0.0003,
            entry_funding_apr=0.33,
            notional_usd=100.0,
            spot_quantity=0.001,
            perp_contracts=0.001,
            spot_order_id="spot-order-1",
            perp_order_id="perp-order-1",
            opened_at=datetime.utcnow().isoformat(),
        )
        data = pos.to_dict()
        assert data["position_id"] == "test-123"
        assert data["perp_ticker"] == "BTC-PERP-INTX"

    def test_funding_position_from_dict(self):
        """FundingPosition should reconstruct from dict."""
        data = {
            "position_id": "test-123",
            "perp_ticker": "BTC-PERP-INTX",
            "spot_ticker": "BTC-USD",
            "entry_spot_price": 83000.0,
            "entry_perp_price": 83050.0,
            "entry_funding_rate_8hr": 0.0003,
            "entry_funding_apr": 0.33,
            "notional_usd": 100.0,
            "spot_quantity": 0.001,
            "perp_contracts": 0.001,
            "spot_order_id": "spot-order-1",
            "perp_order_id": "perp-order-1",
            "status": "open",
            "opened_at": datetime.utcnow().isoformat(),
        }
        pos = FundingPosition.from_dict(data)
        assert pos.position_id == "test-123"
        assert pos.status == "open"


# ============================================================================
# Integration Tests (require Coinbase API access)
# ============================================================================

class TestCoinbaseIntegration:
    """
    Integration tests for Coinbase client.

    Requires COINBASE_API_KEY_NAME and COINBASE_PRIVATE_KEY env vars.
    Run with: pytest tests.py::TestCoinbaseIntegration -v --tb=short
    """

    @pytest.fixture
    def client(self):
        """Create a Coinbase client for testing."""
        import os
        from funding_rate.coinbase_client import CoinbaseClient

        api_key = os.environ.get("COINBASE_API_KEY_NAME")
        private_key = os.environ.get("COINBASE_PRIVATE_KEY")

        if not api_key or not private_key:
            pytest.skip("Coinbase credentials not set")

        return CoinbaseClient(api_key, private_key)

    @pytest.mark.asyncio
    async def test_is_trading_active(self, client):
        """Verify API connectivity."""
        import asyncio
        active = await client.is_trading_active()
        assert active is True
        await client.close()

    @pytest.mark.asyncio
    async def test_get_funding_rate(self, client):
        """Fetch current BTC funding rate."""
        import asyncio
        rate = await client.get_funding_rate("BTC-PERP-INTX")
        assert isinstance(rate, float)
        assert -0.01 < rate < 0.01  # Reasonable bounds
        await client.close()

    @pytest.mark.asyncio
    async def test_get_best_bid_ask(self, client):
        """Fetch current BTC spot bid/ask."""
        import asyncio
        prices = await client.get_best_bid_ask("BTC-USD")
        assert "bid" in prices
        assert "ask" in prices
        assert "mid" in prices
        assert prices["bid"] > 0
        assert prices["ask"] > prices["bid"]
        await client.close()


# ============================================================================
# Example Scenarios
# ============================================================================

class TestScenarios:
    """End-to-end scenario tests."""

    def test_entry_and_exit_scenario(self):
        """
        Simulate: High funding rate entry → collect 3 payments → low rate exit.
        """
        # Entry
        position = FundingPosition(
            position_id="scenario-1",
            perp_ticker="BTC-PERP-INTX",
            spot_ticker="BTC-USD",
            entry_spot_price=80000.0,
            entry_perp_price=80100.0,
            entry_funding_rate_8hr=0.0003,  # ~32% APR
            entry_funding_apr=0.33,
            notional_usd=100.0,
            spot_quantity=0.00125,
            perp_contracts=0.00125,
            spot_order_id="order-1",
            perp_order_id="order-2",
            opened_at=datetime.utcnow().isoformat(),
        )
        assert position.status == "open"

        # Simulate 3 funding payments
        for i in range(3):
            position.funding_collected_usd += position.notional_usd * position.entry_funding_rate_8hr
            position.funding_payments_count += 1

        assert position.funding_payments_count == 3
        assert position.funding_collected_usd > 0.0008  # $0.0009

        # Exit when rate drops
        position.status = "closed"
        position.realized_pnl = position.funding_collected_usd
        assert position.realized_pnl > 0

    def test_rebalance_scenario(self):
        """
        Simulate: Position drifts due to price move → rebalance.
        """
        position = FundingPosition(
            position_id="rebalance-1",
            perp_ticker="BTC-PERP-INTX",
            spot_ticker="BTC-USD",
            entry_spot_price=80000.0,
            entry_perp_price=80000.0,
            entry_funding_rate_8hr=0.0003,
            entry_funding_apr=0.33,
            notional_usd=100.0,
            spot_quantity=0.00125,  # 0.00125 BTC
            perp_contracts=0.00125,  # 0.00125 contracts
            spot_order_id="order-1",
            perp_order_id="order-2",
            opened_at=datetime.utcnow().isoformat(),
        )

        # After price move: BTC is now 82000 (2.5% up)
        current_spot_value = position.spot_quantity * 82000  # $102.50
        current_perp_value = position.perp_contracts * 80000  # $100.00 (short)

        drift = abs(current_spot_value - current_perp_value) / (
            (current_spot_value + current_perp_value) / 2
        )
        assert drift > strategy.REBALANCE_THRESHOLD  # ~1.2% drift

        # Rebalance: add more perp to match spot value
        adjustment = current_spot_value - current_perp_value  # $2.50
        position.perp_contracts += adjustment / 80000
        # Now perp value ≈ spot value, delta-neutral again


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
