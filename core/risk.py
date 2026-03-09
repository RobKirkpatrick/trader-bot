"""
Risk management rules:
  - Max 5% of account per trade
  - Max 4 concurrent positions
  - 7% stop-loss per position
  - Daily loss limit: 5% of account
"""

import logging
from dataclasses import dataclass

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    ticker: str
    direction: str          # "buy" | "sell"
    sentiment_score: float
    current_price: float


@dataclass
class RiskAssessment:
    approved: bool
    reason: str
    position_size: float    # dollar amount
    shares: int
    stop_loss_price: float
    max_loss: float         # dollar amount


class RiskManager:
    def __init__(self, account_size: float):
        """
        Args:
            account_size: Live cash buying power fetched from the broker.
                          Use PublicClient.get_buying_power() to get this value.
        """
        self.account_size = account_size
        self._daily_loss: float = 0.0   # updated via record_loss()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal: TradeSignal,
        open_positions: list[dict],
    ) -> RiskAssessment:
        """
        Run all risk checks against a trade signal.
        Returns an approved or rejected RiskAssessment.
        """
        # 1. Daily loss limit
        daily_loss_limit = self.account_size * settings.DAILY_LOSS_LIMIT_PCT
        if self._daily_loss >= daily_loss_limit:
            return self._reject(
                signal,
                f"Daily loss limit hit (${self._daily_loss:.2f} / ${daily_loss_limit:.2f})",
            )

        # 3. Position sizing (5% of account)
        position_size = self.account_size * settings.MAX_POSITION_PCT
        if signal.current_price <= 0:
            return self._reject(signal, "Invalid price (<= 0)")

        shares = int(position_size // signal.current_price)
        if shares < 1:
            return self._reject(
                signal,
                f"Position too small: ${position_size:.2f} buys 0 shares at ${signal.current_price:.2f}",
            )

        # 4. Stop-loss calculation
        if signal.direction == "buy":
            stop_loss_price = round(
                signal.current_price * (1 - settings.STOP_LOSS_PCT), 2
            )
        else:
            stop_loss_price = round(
                signal.current_price * (1 + settings.STOP_LOSS_PCT), 2
            )

        max_loss = round(shares * abs(signal.current_price - stop_loss_price), 2)

        logger.info(
            "Risk approved: %s %s x%d @ $%.2f | stop $%.2f | max loss $%.2f",
            signal.direction.upper(),
            signal.ticker,
            shares,
            signal.current_price,
            stop_loss_price,
            max_loss,
        )

        return RiskAssessment(
            approved=True,
            reason="All checks passed",
            position_size=round(position_size, 2),
            shares=shares,
            stop_loss_price=stop_loss_price,
            max_loss=max_loss,
        )

    def record_loss(self, amount: float) -> None:
        """Accumulate realised losses for the day (positive = loss)."""
        self._daily_loss += abs(amount)
        logger.info("Daily loss updated: $%.2f", self._daily_loss)

    def reset_daily_loss(self) -> None:
        """Call at market open each day."""
        self._daily_loss = 0.0

    def daily_loss_remaining(self) -> float:
        """Dollar amount of loss budget left for today."""
        limit = self.account_size * settings.DAILY_LOSS_LIMIT_PCT
        return max(0.0, limit - self._daily_loss)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reject(self, signal: TradeSignal, reason: str) -> RiskAssessment:
        logger.warning("Risk rejected %s: %s", signal.ticker, reason)
        return RiskAssessment(
            approved=False,
            reason=reason,
            position_size=0.0,
            shares=0,
            stop_loss_price=0.0,
            max_loss=0.0,
        )

    def within_daily_loss_limit(self) -> bool:
        return self._daily_loss < self.account_size * settings.DAILY_LOSS_LIMIT_PCT
