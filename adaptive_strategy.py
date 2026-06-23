from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime

import logging
logger = logging.getLogger(__name__)

@dataclass
class CandidateConfig:
    candidate_id: str
    stop_loss_cents: int
    risk_reward_ratio: float
    position_size_multiplier: float


DEFAULT_CANDIDATES: List[CandidateConfig] = [
    CandidateConfig("base", 10, 2.0, 1.0),
    CandidateConfig("wide_rr", 15, 2.5, 0.8),
    CandidateConfig("tight_fast", 8, 1.8, 1.0),
    CandidateConfig("defensive", 12, 1.5, 0.7),
]


class AdaptiveLearner:
    """
    SQLite-backed adaptive selector for strategy configs.

    It does 3 things:
    1. Stores candidate parameter sets
    2. Scores them by ticker + regime from CLOSED trades
    3. Selects a config with small exploration
    """

    def __init__(
        self,
        db_path: str = "upgainpulse_paper.db",
        lookback_trades: int = 100,
        min_trades_before_promote: int = 15,
        exploration_rate: float = 0.10,
    ) -> None:
        self.db_path = db_path
        self.lookback_trades = lookback_trades
        self.min_trades_before_promote = min_trades_before_promote
        self.exploration_rate = exploration_rate
        self.conn = sqlite3.connect(self.db_path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._seed_candidates(DEFAULT_CANDIDATES)

    def close(self) -> None:
        self.conn.close()

    def _create_tables(self) -> None:
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_candidates (
            candidate_id TEXT PRIMARY KEY,
            stop_loss_cents INTEGER NOT NULL,
            risk_reward_ratio REAL NOT NULL,
            position_size_multiplier REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_features (
            trade_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            regime TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            orb_width_pct REAL,
            gap_pct REAL,
            rel_volume REAL,
            atr_pct REAL,
            market_trend TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        self.conn.commit()

    def _seed_candidates(self, candidates: List[CandidateConfig]) -> None:
        for c in candidates:
            self.conn.execute("""
            INSERT OR IGNORE INTO strategy_candidates
            (candidate_id, stop_loss_cents, risk_reward_ratio, position_size_multiplier, enabled)
            VALUES (?, ?, ?, ?, 1)
            """, (
                c.candidate_id,
                c.stop_loss_cents,
                c.risk_reward_ratio,
                c.position_size_multiplier,
            ))
        self.conn.commit()

    def classify_regime(
        self,
        atr_pct: float,
        gap_pct: float,
        market_above_ma: bool,
        rel_volume: float,
    ) -> str:
        """
        Simple first-pass regime classifier.
        """
        if atr_pct >= 2.0 or abs(gap_pct) >= 1.0:
            return "high_vol_trend" if market_above_ma else "high_vol_range"
        if rel_volume >= 1.2 and market_above_ma:
            return "trend"
        if atr_pct < 1.0 and rel_volume < 1.0:
            return "quiet_range"
        return "range"

    def get_enabled_candidates(self) -> List[CandidateConfig]:
        rows = self.conn.execute("""
        SELECT candidate_id, stop_loss_cents, risk_reward_ratio, position_size_multiplier
        FROM strategy_candidates
        WHERE enabled = 1
        ORDER BY candidate_id
        """).fetchall()
        return [
            CandidateConfig(
                candidate_id=row["candidate_id"],
                stop_loss_cents=row["stop_loss_cents"],
                risk_reward_ratio=row["risk_reward_ratio"],
                position_size_multiplier=row["position_size_multiplier"],
            )
            for row in rows
        ]

    def log_trade_features(
        self,
        trade_id: int,
        ticker: str,
        regime: str,
        candidate_id: str,
        orb_width_pct: float,
        gap_pct: float,
        rel_volume: float,
        atr_pct: float,
        market_trend: str,
    ) -> None:
        self.conn.execute("""
        INSERT OR REPLACE INTO trade_features
        (trade_id, ticker, regime, candidate_id, orb_width_pct, gap_pct, rel_volume, atr_pct, market_trend)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            ticker,
            regime,
            candidate_id,
            orb_width_pct,
            gap_pct,
            rel_volume,
            atr_pct,
            market_trend,
        ))
        self.conn.commit()

    def _candidate_stats(self, ticker: str, regime: str, candidate_id: str) -> Dict[str, float]:
        """
        Assumes your trades table is properly updated to CLOSED with pnl.
        """
        rows = self.conn.execute("""
        SELECT t.pnl
        FROM trades t
        JOIN trade_features f ON f.trade_id = t.id
        WHERE t.status = 'CLOSED'
          AND t.pnl IS NOT NULL
          AND f.ticker = ?
          AND f.regime = ?
          AND f.candidate_id = ?
        ORDER BY t.timestamp_entry DESC
        LIMIT ?
        """, (ticker, regime, candidate_id, self.lookback_trades)).fetchall()

        pnls = [float(r["pnl"]) for r in rows]
        n = len(pnls)
        if n == 0:
            return {
                "n": 0,
                "expectancy": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "score": -999.0,
            }

        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x <= 0]

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        win_rate = len(wins) / n
        expectancy = sum(pnls) / n
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for x in pnls:
            equity += x
            peak = max(peak, equity)
            dd = peak - equity
            max_dd = max(max_dd, dd)

        # Weighted score:
        # expectancy matters most, then PF, then win rate, minus drawdown penalty
        score = (
            expectancy * 1.5
            + min(profit_factor, 5.0) * 0.8
            + win_rate * 0.5
            - max_dd * 0.15
        )

        return {
            "n": n,
            "expectancy": expectancy,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "score": score,
        }

    def select_candidate(
        self,
        ticker: str,
        regime: str,
        default_config: Dict,
    ) -> Tuple[Dict, str, Dict[str, Dict[str, float]]]:
        """
        Returns:
        - chosen config dict
        - candidate_id
        - stats for inspection/logging
        """
        candidates = self.get_enabled_candidates()
        if not candidates:
            return default_config, "default", {}

        stats_map: Dict[str, Dict[str, float]] = {}
        total_obs = 1

        for c in candidates:
            s = self._candidate_stats(ticker, regime, c.candidate_id)
            stats_map[c.candidate_id] = s
            total_obs += s["n"]

        # Small exploration so the bot keeps learning
        if random.random() < self.exploration_rate:
            chosen = random.choice(candidates)
        else:
            best_score = -10**9
            chosen = candidates[0]

            for c in candidates:
                s = stats_map[c.candidate_id]
                # UCB-style exploration bonus for low-sample configs
                exploration_bonus = math.sqrt(math.log(total_obs + 1) / (s["n"] + 1))
                adjusted_score = s["score"] + exploration_bonus

                # Don't promote weak configs too early unless all are low-sample
                if s["n"] < self.min_trades_before_promote:
                    adjusted_score -= 0.25

                if adjusted_score > best_score:
                    best_score = adjusted_score
                    chosen = c

        learned_config = {
            **default_config,
            "stop_loss_cents": chosen.stop_loss_cents,
            "risk_reward_ratio": chosen.risk_reward_ratio,
            "position_size_multiplier": chosen.position_size_multiplier,
        }
        return learned_config, chosen.candidate_id, stats_map
