"""
DynamoDB record model for the carpet-bagger-watchlist table.

Each record tracks one Kalshi market from pre-game scout through close.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class WatchlistRecord:
    market_ticker: str          # Kalshi market ticker — DynamoDB hash key
    sport: str                  # KXNBA | KXNHL | KXNCAAB | KXMLB
    teams: str                  # human-readable "TeamA vs TeamB"
    game_time: str              # ISO-8601 string
    pre_game_prob: float        # yes_ask at scout time (0.55–0.70)
    current_prob: float         # most recently fetched yes_ask
    status: str                 # watching | bought | closed

    position_size: float = 0.0      # dollars deployed (total including pre-game stake)
    contract_count: int = 0         # Kalshi contracts held
    entry_price: float = 0.0        # yes_price at buy (in dollars)
    peak_prob: float = 0.0          # highest yes_ask seen since entry (for trailing stop)
    trigger_time: str = ""          # ISO-8601 when buy order placed
    pnl: float = 0.0                # realised P&L in dollars (negative = loss)
    last_updated: str = ""          # ISO-8601 of last DynamoDB write
    pre_game_staked: float = 0.0    # dollars spent in pre-game stake (legacy field — kept for DB compat)
    sell_order_id: str = ""         # Kalshi order ID of the resting $0.97 sell limit (cancel on stop-loss)

    def to_dynamodb(self) -> dict:
        return {
            "market_ticker": {"S": self.market_ticker},
            "sport":         {"S": self.sport},
            "teams":         {"S": self.teams},
            "game_time":     {"S": self.game_time},
            "pre_game_prob": {"N": str(self.pre_game_prob)},
            "current_prob":  {"N": str(self.current_prob)},
            "status":        {"S": self.status},
            "position_size": {"N": str(self.position_size)},
            "contract_count":{"N": str(self.contract_count)},
            "entry_price":   {"N": str(self.entry_price)},
            "peak_prob":     {"N": str(self.peak_prob)},
            "trigger_time":  {"S": self.trigger_time},
            "pnl":             {"N": str(self.pnl)},
            "last_updated":    {"S": self.last_updated},
            "pre_game_staked": {"N": str(self.pre_game_staked)},
            "sell_order_id":   {"S": self.sell_order_id},
        }

    @classmethod
    def from_dynamodb(cls, item: dict) -> "WatchlistRecord":
        return cls(
            market_ticker = item["market_ticker"]["S"],
            sport         = item.get("sport",         {"S": ""})["S"],
            teams         = item.get("teams",         {"S": ""})["S"],
            game_time     = item.get("game_time",     {"S": ""})["S"],
            pre_game_prob = float(item.get("pre_game_prob", {"N": "0"})["N"]),
            current_prob  = float(item.get("current_prob",  {"N": "0"})["N"]),
            status        = item.get("status",        {"S": "watching"})["S"],
            position_size = float(item.get("position_size", {"N": "0"})["N"]),
            contract_count= int(float(item.get("contract_count", {"N": "0"})["N"])),
            entry_price   = float(item.get("entry_price",   {"N": "0"})["N"]),
            peak_prob     = float(item.get("peak_prob",     {"N": "0"})["N"]),
            trigger_time  = item.get("trigger_time",  {"S": ""})["S"],
            pnl              = float(item.get("pnl",              {"N": "0"})["N"]),
            last_updated     = item.get("last_updated",   {"S": ""})["S"],
            pre_game_staked  = float(item.get("pre_game_staked",  {"N": "0"})["N"]),
            sell_order_id    = item.get("sell_order_id",  {"S": ""})["S"],
        )
