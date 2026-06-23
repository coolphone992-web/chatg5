from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime

import logging
logger = logging.getLogger(__name__)

@dataclass
class CandidateConfig:
    candidate_id: str
    strategy_type: str # e.g., "ORB", "SMA_CROSS", "RSI_MR"
    
    # Common parameters
    position_size_multiplier: float = 1.0
    risk_per_trade_usd: float = 50.0 # Max USD to risk per trade
    
    # Stop Loss / Take Profit (percentage based for flexibility)
    stop_loss_pct: float = 0.002 # 0.2% of entry price
    take_profit_rr: float = 2.0 # Risk-reward ratio (for ORB/SMA Cross)
    take_profit_pct: Optional[float] = None # Absolute % target (for RSI MR)

    # ORB specific
    orb_sl_cents: Optional[int] = None # Original ORB absolute cents SL (if preferred)

    # SMA Crossover specific
    short_sma_period: Optional[int] = None
    long_sma_period: Optional[int] = None

    # RSI Mean Reversion specific
    rsi_period: Optional[int] = None
    oversold_level: Optional[int] = None
    overbought_level: Optional[int] = None


DEFAULT_CANDIDATES: List[CandidateConfig] = [
    # --- ORB Strategy Candidates ---
    CandidateConfig("orb_base_2_1", "ORB", stop_loss_pct=0.002, take_profit_rr=2.0, position_size_multiplier=1.0),
    CandidateConfig("orb_tight_3_1", "ORB", stop_loss_pct=0.0015, take_profit_rr=3.0, position_size_multiplier=0.8),
    CandidateConfig("orb_wide_1_5_1", "ORB", stop_loss_pct=0.003, take_profit_rr=1.5, position_size_multiplier=1.2),

    # --- SMA Crossover Strategy Candidates ---
    CandidateConfig("sma_5_20_trend", "SMA_CROSS", short_sma_period=5, long_sma_period=20, 
                    stop_loss_pct=0.005, take_profit_rr=2.5, position_size_multiplier=0.7),
    CandidateConfig("sma_10_50_swing", "SMA_CROSS", short_sma_period=10, long_sma_period=50, 
                    stop_loss_pct=0.01, take_profit_rr=2.0, position_size_multiplier=0.6),

    # --- RSI Mean Reversion Strategy Candidates ---
    CandidateConfig("rsi_14_oversold", "RSI_MR", rsi_period=14, oversold_level=30, overbought_level=70, 
                    stop_loss_pct=0.003, take_profit_pct=0.005, position_size_multiplier=0.9),
    CandidateConfig("rsi_7_extreme", "RSI_MR", rsi_period=7, oversold_level=20, overbought_level=80, 
                    stop_loss_pct=0.002, take_profit_pct=0.003, position_size_multiplier=1.1),
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
            strategy_type TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            risk_per_trade_usd REAL NOT NULL,
            stop_loss_pct REAL NOT NULL,
            take_profit_rr REAL,
            take_profit_pct REAL,
            short_sma_period INTEGER,
            long_sma_period INTEGER,
            rsi_period INTEGER,
            oversold_level INTEGER,
            overbought_level INTEGER,
            orb_sl_cents INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_features (
            trade_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            regime TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            strategy_type TEXT NOT NULL, -- NEW: Store strategy type
            orb_width_pct REAL,
            gap_pct REAL,
            rel_volume REAL,
            atr_pct REAL,
            market_trend TEXT,
            rsi_value REAL, -- NEW: RSI value at trade entry
            short_sma REAL, -- NEW: Short SMA value at trade entry
            long_sma REAL,  -- NEW: Long SMA value at trade entry
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        self.conn.commit()

    def _seed_candidates(self, candidates: List[CandidateConfig]) -> None:
        for c in candidates:
            self.conn.execute("""
            INSERT OR IGNORE INTO strategy_candidates
            (candidate_id, strategy_type, position_size_multiplier, risk_per_trade_usd, stop_loss_pct, 
             take_profit_rr, take_profit_pct, short_sma_period, long_sma_period, rsi_period, 
             oversold_level, overbought_level, orb_sl_cents, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                c.candidate_id, c.strategy_type, c.position_size_multiplier, c.risk_per_trade_usd, c.stop_loss_pct,
                c.take_profit_rr, c.take_profit_pct, c.short_sma_period, c.long_sma_period, c.rsi_period,
                c.oversold_level, c.overbought_level, c.orb_sl_cents
            ))
        self.conn.commit()

    def classify_regime(
        self,
        ticker: str,
        current_price: float,
        orb_width_pct: float,
        gap_pct: float,
        atr_pct: float,
        rel_volume: float,
        rsi_value: Optional[float],
        short_sma: Optional[float],
        long_sma: Optional[float],
        market_trend: str, # e.g., "up", "down", "sideways"
    ) -> str:
        """
        Classifies the current market regime based on various indicators.
        This is a simplified example; real-world classification can be more complex.
        """
        regime = "unknown"

        # High Volatility / Gap Play
        if atr_pct >= 2.0 or abs(gap_pct) >= 1.5: # ATR 2% or gap 1.5%
            return "high_vol_gap" if market_trend == "up" else "high_vol_gap_down"

        # Trend Following Regimes
        if market_trend == "up":
            if short_sma is not None and long_sma is not None and short_sma > long_sma and rel_volume > 1.2:
                regime = "strong_uptrend_sma_cross"
            elif rel_volume > 1.0 and orb_width_pct > 0.5: # Strong ORB breakout potential
                regime = "uptrend_orb_momentum"
            else:
                regime = "general_uptrend"
        elif market_trend == "down":
            if short_sma is not None and long_sma is not None and short_sma < long_sma and rel_volume > 1.2:
                regime = "strong_downtrend_sma_cross"
            else:
                regime = "general_downtrend"
        
        # Mean Reversion Regimes (within a range or slight trend)
        if regime == "unknown" or "range" in regime:
            if rsi_value is not None:
                if rsi_value < 30 and market_trend != "down": # Oversold, not in strong downtrend
                    regime = "oversold_bounce"
                elif rsi_value > 70 and market_trend != "up": # Overbought, not in strong uptrend
                    regime = "overbought_fade"
                elif atr_pct < 0.8 and rel_volume < 0.8: # Low vol, low momentum
                    regime = "quiet_range"
                else:
                    regime = "general_range"
        
        return regime

    def get_enabled_candidates(self) -> List[CandidateConfig]:
        rows = self.conn.execute("""
        SELECT candidate_id, strategy_type, position_size_multiplier, risk_per_trade_usd, stop_loss_pct, 
               take_profit_rr, take_profit_pct, short_sma_period, long_sma_period, rsi_period, 
               oversold_level, overbought_level, orb_sl_cents
        FROM strategy_candidates
        WHERE enabled = 1
        ORDER BY candidate_id
        """).fetchall()
        return [
            CandidateConfig(
                candidate_id=row["candidate_id"],
                strategy_type=row["strategy_type"],
                position_size_multiplier=row["position_size_multiplier"],
                risk_per_trade_usd=row["risk_per_trade_usd"],
                stop_loss_pct=row["stop_loss_pct"],
                take_profit_rr=row["take_profit_rr"],
                take_profit_pct=row["take_profit_pct"],
                short_sma_period=row["short_sma_period"],
                long_sma_period=row["long_sma_period"],
                rsi_period=row["rsi_period"],
                oversold_level=row["oversold_level"],
                overbought_level=row["overbought_level"],
                orb_sl_cents=row["orb_sl_cents"],
            )
            for row in rows
        ]

    def log_trade_features(
        self,
        trade_id: int,
        ticker: str,
        regime: str,
        candidate_id: str,
        strategy_type: str, # NEW: Strategy type
        orb_width_pct: Optional[float],
        gap_pct: Optional[float],
        rel_volume: Optional[float],
        atr_pct: Optional[float],
        market_trend: Optional[str],
        rsi_value: Optional[float] = None, # NEW: RSI value at trade entry
        short_sma: Optional[float] = None, # NEW: Short SMA value at trade entry
        long_sma: Optional[float] = None,  # NEW: Long SMA value at trade entry
    ) -> None:
        self.conn.execute("""
        INSERT OR REPLACE INTO trade_features
        (trade_id, ticker, regime, candidate_id, strategy_type, orb_width_pct, gap_pct, rel_volume, atr_pct, market_trend, rsi_value, short_sma, long_sma)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            ticker,
            regime,
            candidate_id,
            strategy_type,
            orb_width_pct,
            gap_pct,
            rel_volume,
            atr_pct,
            market_trend,
            rsi_value,
            short_sma,
            long_sma,
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
    ) -> Tuple[Dict, str, str, Dict[str, Dict[str, float]]]: # NEW: Return strategy_type
        """
        Returns:
        - chosen config dict (merged with default_config)
        - candidate_id
        - strategy_type (NEW)
        - stats for inspection/logging
        """
        candidates = self.get_enabled_candidates()
        if not candidates:
            # If no candidates, return a default config with a default strategy type
            return default_config, "default", "ORB", {}

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
                exploration_bonus = math.sqrt(math.log(total_obs + 1) / (s["n"] + 1)) if s["n"] > 0 else 1.0 # Give new configs a boost
                adjusted_score = s["score"] + exploration_bonus

                # Don't promote weak configs too early unless all are low-sample
                if s["n"] < self.min_trades_before_promote:
                    adjusted_score -= 0.25 # Penalty for insufficient data

                if adjusted_score > best_score:
                    best_score = adjusted_score
                    chosen = c

        # Merge chosen candidate config with default config to ensure all base parameters are present
        learned_config = {
            **default_config,
            **asdict(chosen) # Convert dataclass to dict and merge
        }
        # Remove dataclass-specific fields that shouldn't be in the final config dict
        learned_config.pop("candidate_id", None)
        learned_config.pop("strategy_type", None)
        learned_config.pop("enabled", None)

        return learned_config, chosen.candidate_id, chosen.strategy_type, stats_map
