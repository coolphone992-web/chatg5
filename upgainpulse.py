import os
import sqlite3
import asyncio
import math
import threading
import logging
from datetime import datetime, time, timedelta
from typing import Optional, Dict, List, Deque
from collections import deque
from pytz import timezone
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.models import TakeProfitRequest, StopLossRequest
from alpaca.trading.errors import APIError
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

from adaptive_strategy import AdaptiveLearner, CandidateConfig, DEFAULT_CANDIDATES # NEW: Import AdaptiveLearner and CandidateConfig

# =============================================================================
# LOGGING SETUP
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("upgainpulse.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables securely
load_dotenv()
API_KEY = os.getenv("ALPACA_PAPER_API_KEY")
SECRET_KEY = os.getenv("ALPACA_PAPER_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    logger.error("CRITICAL: Alpaca API keys not found in .env file.")
    raise ValueError("Missing Alpaca API keys in .env")

# =============================================================================
# TIMEZONE HANDLING
# =============================================================================
ET = timezone('US/Eastern')
UTC = timezone('UTC')

def convert_utc_to_et(utc_datetime) -> datetime:
    """Convert UTC datetime to Eastern Time for market hours comparison."""
    if utc_datetime.tzinfo is None:
        utc_datetime = UTC.localize(utc_datetime)
    return utc_datetime.astimezone(ET)

# =============================================================================
# CONFIGURATION VALIDATION
# =============================================================================
class ConfigError(Exception):
    """Raised when configuration is invalid."""
    pass

def validate_config(config: Dict) -> None:
    """Validate trading configuration parameters."""
    try:
        # These are base config values, individual candidates will override/add
        risk = float(config.get("risk_per_trade_usd", 50.0))
        sl_pct = float(config.get("stop_loss_pct", 0.002)) # Default to percentage
        multiplier = float(config.get("position_size_multiplier", 1.0))
        rr_ratio = float(config.get("risk_reward_ratio", 2.0))
        account_capital = float(config.get("account_capital", 0.0)) 

        if risk <= 0:
            raise ConfigError("risk_per_trade_usd must be > 0")
        if sl_pct <= 0:
            raise ConfigError("stop_loss_pct must be > 0")
        if multiplier <= 0:
            raise ConfigError("position_size_multiplier must be > 0")
        if rr_ratio <= 0:
            raise ConfigError("risk_reward_ratio must be > 0")
        if rr_ratio > 10:
            raise ConfigError("risk_reward_ratio seems unrealistic (> 10)")
        if account_capital <= 0:
            logger.warning("account_capital not set or <= 0. Using available buying power as cap.")

        logger.info(f"Base Config validated: risk=${risk}, SL={sl_pct*100:.2f}%, multiplier={multiplier}x, RR={rr_ratio}:1")
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Config type error: {e}")

def calculate_position_size(current_price: float, config: Dict, available_buying_power: float) -> int:
    """
    Correct position sizing based on percentage stop loss.
    1) Size from risk per trade (using percentage stop loss)
    2) Cap by configured account capital
    3) Cap by live available buying power
    """
    if current_price <= 0:
        return 0

    risk_per_trade = float(config.get("risk_per_trade_usd", 50.0))
    stop_loss_pct = float(config.get("stop_loss_pct", 0.002)) # Use percentage stop loss
    multiplier = float(config.get("position_size_multiplier", 1.0))
    account_capital = float(config.get("account_capital", available_buying_power)) # Use BP if not set

    if stop_loss_pct <= 0:
        return 0

    # Calculate stop loss in dollars per share
    stop_loss_dollars_per_share = current_price * stop_loss_pct
    if stop_loss_dollars_per_share <= 0:
        return 0

    qty_by_risk = math.floor((risk_per_trade / stop_loss_dollars_per_share) * multiplier)
    qty_by_config_capital = math.floor(account_capital / current_price)
    qty_by_buying_power = math.floor(available_buying_power / current_price)

    qty = min(qty_by_risk, qty_by_config_capital, qty_by_buying_power)
    return max(qty, 0)

# =============================================================================
# TRADE LOGGING (DATABASE) - MODIFIED FOR ADAPTIVE LAYER
# =============================================================================
class TradeLogger:
    def __init__(self, db_path="upgainpulse_paper.db"):
        self.db_path = db_path
        self.local = threading.local()
        self.lock = threading.RLock() # Use RLock for re-entrant locks
        self._create_table()
 
    def _get_connection(self):
        if not hasattr(self.local, 'conn'):
            self.local.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
            self.local.conn.execute("PRAGMA journal_mode=WAL")
            self.local.conn.row_factory = sqlite3.Row
        return self.local.conn
 
    def _create_table(self):
        with self.lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            # MODIFIED: Added regime, candidate_id, and strategy_type columns
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_entry TEXT NOT NULL,
                    timestamp_exit TEXT,
                    ticker TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity INTEGER NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    status TEXT DEFAULT "OPEN",
                    order_id TEXT,
                    pnl REAL,
                    regime TEXT,           -- NEW: Market regime at time of trade
                    candidate_id TEXT,     -- NEW: ID of the candidate config used
                    strategy_type TEXT,    -- NEW: Type of strategy used (ORB, SMA_CROSS, RSI_MR)
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            logger.info("Trade database initialized")
 
    def log_entry(self, ticker: str, setup_type: str, entry_price: float, qty: int, sl: float, tp: float, 
                  order_id: Optional[str] = None, regime: Optional[str] = None, 
                  candidate_id: Optional[str] = None, strategy_type: Optional[str] = None) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                # MODIFIED: Added regime, candidate_id, and strategy_type to insert statement
                cursor.execute('''
                    INSERT INTO trades (timestamp_entry, ticker, setup_type, entry_price, quantity, stop_loss, take_profit, status, order_id, regime, candidate_id, strategy_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (timestamp, ticker, setup_type, entry_price, qty, sl, tp, 'OPEN', order_id, regime, candidate_id, strategy_type))
                conn.commit()
                logger.info(f"OPEN | {ticker} ${entry_price:.2f} x{qty} | SL ${sl:.2f} | TP ${tp:.2f} | Order: {order_id} | Regime: {regime} | Config: {candidate_id} | Strategy: {strategy_type}")
            except sqlite3.Error as e:
                logger.error(f"DB insert error: {e}")

    def update_exit_details(self, order_id: str, exit_price: float, pnl: float, timestamp_exit: Optional[datetime] = None) -> None:
        timestamp_exit_str = (timestamp_exit or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    UPDATE trades
                    SET timestamp_exit = ?, exit_price = ?, pnl = ?, status = 'CLOSED'
                    WHERE order_id = ?
                """, (timestamp_exit_str, exit_price, pnl, order_id))
                conn.commit()
                logger.info(f"CLOSED | Order {order_id} | PnL: ${pnl:.2f}")
            except sqlite3.Error as e:
                logger.error(f"DB update error for order {order_id}: {e}")

# =============================================================================
# ACCOUNT VALIDATION
# =============================================================================
class AccountValidator:
    def __init__(self, alpaca_client: TradingClient):
        self.client = alpaca_client

    def get_buying_power(self) -> float:
        try:
            account = self.client.get_account()
            return float(account.buying_power)
        except APIError as e:
            logger.error(f"Failed to fetch account: {getattr(e, 'message', str(e))}")
            return 0.0

    def check_buying_power(self, required_capital: float) -> bool:
        available_bp = self.get_buying_power()
        if available_bp < required_capital:
            logger.warning(f"Insufficient BP: ${available_bp:.2f} < ${required_capital:.2f}")
            return False
        return True

# =============================================================================
# ALPACA PAPER TRADER - MODIFIED FOR ADAPTIVE LAYER
# =============================================================================
class AlpacaPaperTrader:
    def __init__(self, api_key: str, secret_key: str, logger_obj: TradeLogger, account_validator: AccountValidator):
        self.client = TradingClient(api_key, secret_key, paper=True)
        self.logger_obj = logger_obj
        self.validator = account_validator
        self.last_order_time = {}
        self.order_cooldown = 1.0 # seconds

    def execute_trade_setup(self, ticker: str, current_price: float, config: Dict) -> Optional[Dict]: # RENAMED
        now = datetime.now().timestamp()
        last_time = self.last_order_time.get(ticker, 0)
        if now - last_time < self.order_cooldown:
            return None

        try:
            risk = float(config.get("risk_per_trade_usd", 50.0))
            stop_loss_pct = float(config.get("stop_loss_pct", 0.002)) # Expect percentage stop loss
            multiplier = float(config.get("position_size_multiplier", 1.0))
            rr_ratio = float(config.get("take_profit_rr", 2.0)) # Use RR for ORB/SMA
            take_profit_pct = float(config.get("take_profit_pct") or 0.0) # Use absolute % for RSI, default 0 if None
            strategy_type = config.get("strategy_type", "UNKNOWN")

            available_bp = self.validator.get_buying_power()
            if available_bp <= 0:
                logger.error("No buying power available")
                return None

            qty = calculate_position_size(current_price, config, available_bp)
            if qty <= 0:
                logger.error(
                    f"Qty calc failed | ticker={ticker} | price=${current_price:.2f} | "
                    f"risk=${risk:.2f} | stop_pct={stop_loss_pct*100:.2f}% | bp=${available_bp:.2f}"
                )
                return None

            # Calculate SL and TP prices based on strategy type
            sl_price = round(current_price * (1 - stop_loss_pct), 2)
            tp_price = 0.0

            if strategy_type == "RSI_MR" and take_profit_pct > 0:
                tp_price = round(current_price * (1 + take_profit_pct), 2)
            elif rr_ratio > 0: # Default to RR for ORB and SMA_CROSS
                risk_dollars = current_price * stop_loss_pct
                tp_price = round(current_price + (risk_dollars * rr_ratio), 2)
            else:
                logger.warning(f"Strategy {strategy_type} has no valid TP defined. Using default RR 2.0.")
                risk_dollars = current_price * stop_loss_pct
                tp_price = round(current_price + (risk_dollars * 2.0), 2)

            if tp_price <= sl_price: # Ensure TP is above SL for long positions
                logger.error(f"Invalid TP/SL for {ticker}: TP ${tp_price:.2f} <= SL ${sl_price:.2f}")
                return None

            logger.info(f"SIZING {ticker} | qty={qty} | required_capital=${current_price * qty:.2f} | risk=${risk:.2f} | SL_pct={stop_loss_pct*100:.2f}% | TP_pct={take_profit_pct*100:.2f}% | RR={rr_ratio:.2f}:1 | Strategy: {strategy_type}")

            order_data = MarketOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp_price),
                stop_loss=StopLossRequest(stop_price=sl_price)
            )
            order = self.client.submit_order(order_data)
            self.last_order_time[ticker] = now
            logger.info(f"ORDER {ticker} | {order.id}")
            # MODIFIED: Added strategy_type, regime and candidate_id to log_entry
            self.logger_obj.log_entry(ticker, strategy_type, current_price, qty, sl_price, tp_price, order.id, 
                                      regime=config.get("current_regime"), candidate_id=config.get("candidate_id"),
                                      strategy_type=strategy_type)
            return {
                "order_id": order.id,
                "entry_price": current_price,
                "quantity": qty,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
                "strategy_type": strategy_type # NEW: Return strategy type
            }
        except APIError as e:
            logger.error(f"API Error: {getattr(e, 'message', str(e))}")
            return None
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            return None

# =============================================================================
# TRADING STATE MACHINE (FORMERLY ORB STATE MACHINE) - MODIFIED FOR ADAPTIVE LAYER
# =============================================================================
class TradingStateMachine:
    def __init__(self, ticker: str, trader: AlpacaPaperTrader, base_config: Dict, adaptive_learner: AdaptiveLearner):
        self.ticker = ticker
        self.trader = trader
        self.base_config = base_config # Store base config, adaptive learner provides specific config
        self.adaptive_learner = adaptive_learner
        self.market_open_et = time(9, 30)
        self.market_close_et = time(16, 0) # NEW: Market close time
        self.range_end_et = time(9, 45)
        self.reset_daily_state()

        # Historical data for indicator calculation
        self.bars: Deque[Bar] = deque(maxlen=50) # Store last 50 bars for SMA/RSI
        self.closes: Deque[float] = deque(maxlen=50) # Store last 50 closes for SMA/RSI
        self.volumes: Deque[float] = deque(maxlen=50) # NEW: Store last 50 volumes for Rel Volume
        self.previous_day_close: Optional[float] = None # NEW: Store previous day's close for gap_pct

        # Active trade details
        self.active_trade_id: Optional[int] = None 
        self.active_trade_order_id: Optional[str] = None
        self.active_trade_entry_price: float = 0.0
        self.active_trade_sl_price: float = 0.0
        self.active_trade_tp_price: float = 0.0
        self.active_trade_qty: int = 0
        self.current_regime: str = "unknown"
        self.current_candidate_id: str = "default"
        self.current_strategy_type: str = "ORB" # NEW: Store current strategy type

        # Indicator values for logging/regime classification
        self.last_atr_pct: float = 0.0
        self.last_gap_pct: float = 0.0
        self.last_rel_volume: float = 0.0
        self.last_rsi_value: Optional[float] = None
        self.last_short_sma: Optional[float] = None
        self.last_long_sma: Optional[float] = None
        self.last_market_trend: str = "sideways"

    def reset_daily_state(self):
        self.orb_high = 0.0
        self.orb_low = float('inf')
        self.range_established = False
        self.position_taken = False
        self.last_reset_date = datetime.now().date()
        self.active_trade_id = None 
        self.active_trade_order_id = None
        self.active_trade_entry_price = 0.0
        self.active_trade_sl_price = 0.0
        self.active_trade_tp_price = 0.0
        self.active_trade_qty = 0
        self.current_regime = "unknown"
        self.current_candidate_id = "default"
        self.current_strategy_type = "ORB" # Reset to default strategy type
        self.bars.clear()
        self.closes.clear()
        self.volumes.clear()
        # self.previous_day_close should ideally be fetched from historical data
        # For now, it will remain None until a proper historical data fetch is implemented.
        # logger.info(f"RESET {self.ticker}") # Logged in check_daily_reset

    def check_daily_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.reset_daily_state()
            logger.info(f"RESET {self.ticker}")

    def _calculate_sma(self, period: int) -> Optional[float]:
        if len(self.closes) < period:
            return None
        return sum(list(self.closes)[-period:]) / period

    def _calculate_rsi(self, period: int) -> Optional[float]:
        if len(self.closes) < period + 1:
            return None
        
        # Proper RSI calculation using rolling average gain/loss
        gains = deque(maxlen=period)
        losses = deque(maxlen=period)

        for i in range(1, len(self.closes)):
            delta = self.closes[i] - self.closes[i-1]
            if delta > 0:
                gains.append(delta)
                losses.append(0)
            else:
                losses.append(abs(delta))
                gains.append(0)
        
        # Ensure we have enough data for the period
        if len(gains) < period:
            return None

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0 # Avoid division by zero
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    def _calculate_atr_pct(self, period: int = 14) -> Optional[float]:
        if len(self.bars) < period:
            return None
        
        # ATR calculation using True Range
        true_ranges = []
        for i in range(1, period + 1):
            current_bar = self.bars[-i]
            previous_bar_close = self.bars[-(i+1)].close if len(self.bars) > i else current_bar.close # Use current close if no previous bar
            
            high_low = current_bar.high - current_bar.low
            high_prev_close = abs(current_bar.high - previous_bar_close)
            low_prev_close = abs(current_bar.low - previous_bar_close)
            
            tr = max(high_low, high_prev_close, low_prev_close)
            true_ranges.append(tr)
            
        atr = sum(true_ranges) / period
        return (atr / self.bars[-1].close * 100) if self.bars[-1].close > 0 else 0.0

    def _calculate_gap_pct(self, current_open: float) -> float:
        if self.previous_day_close is None or self.previous_day_close == 0:
            return 0.0 # Cannot calculate gap without previous day's close
        return (current_open - self.previous_day_close) / self.previous_day_close * 100

    def _calculate_rel_volume(self, current_volume: float, period: int = 20) -> float:
        if len(self.volumes) < period or sum(self.volumes) == 0: # Ensure enough data and non-zero average
            return 1.0 # Default to 1.0 if not enough history
        
        avg_volume = sum(list(self.volumes)[-period:]) / period
        return (current_volume / avg_volume) if avg_volume > 0 else 1.0

    async def process_minute_bar(self, bar: Bar):
        self.check_daily_reset()
        bar_time_et = convert_utc_to_et(bar.timestamp).time()
        current_et_datetime = convert_utc_to_et(bar.timestamp)

        # NEW: Update previous_day_close at the end of the market day
        if bar_time_et >= self.market_close_et and self.previous_day_close is None: # Only set once per day
            self.previous_day_close = bar.close
            logger.info(f"END OF DAY {self.ticker} | Stored previous day close: ${self.previous_day_close:.2f}")

        # NEW: Market hours check for trading logic (ORB range building is pre-market/early market)
        if not (self.market_open_et <= bar_time_et <= self.market_close_et):
            # NEW: Overnight position management - close any open positions at market close
            if self.position_taken and self.active_trade_order_id and bar_time_et >= self.market_close_et:
                logger.warning(f"MARKET CLOSE | Closing open position for {self.ticker} due to market close.")
                # Simulate market order close at current_price
                exit_price = bar.close
                pnl = (exit_price - self.active_trade_entry_price) * self.active_trade_qty
                self.trader.logger_obj.update_exit_details(
                    self.active_trade_order_id,
                    exit_price,
                    pnl,
                    bar.timestamp
                )
                # Log trade features after trade closure
                if self.active_trade_id and self.current_regime and self.current_candidate_id and self.current_strategy_type:
                    orb_width_pct = (self.orb_high - self.orb_low) / self.orb_low * 100 if self.orb_low > 0 else 0
                    self.adaptive_learner.log_trade_features(
                        trade_id=self.active_trade_id,
                        ticker=self.ticker,
                        regime=self.current_regime,
                        candidate_id=self.current_candidate_id,
                        strategy_type=self.current_strategy_type,
                        orb_width_pct=orb_width_pct,
                        gap_pct=self.last_gap_pct,
                        rel_volume=self.last_rel_volume,
                        atr_pct=self.last_atr_pct,
                        market_trend=self.last_market_trend,
                        rsi_value=self.last_rsi_value,
                        short_sma=self.last_short_sma,
                        long_sma=self.last_long_sma,
                    )
                self.position_taken = False
                self.active_trade_id = None 
                self.active_trade_order_id = None 
                self.active_trade_entry_price = 0.0
                self.active_trade_sl_price = 0.0
                self.active_trade_tp_price = 0.0
                self.active_trade_qty = 0
                self.current_regime = "unknown"
                self.current_candidate_id = "default"
                self.current_strategy_type = "ORB"
            return # Do not process bars outside market hours

        self.bars.append(bar)
        self.closes.append(bar.close)
        self.volumes.append(bar.volume) # NEW: Append volume

        # Calculate indicators for regime classification (only if enough data)
        self.last_short_sma = self._calculate_sma(20) # Example short SMA
        self.last_long_sma = self._calculate_sma(50)  # Example long SMA
        self.last_rsi_value = self._calculate_rsi(14)       # Example RSI
        self.last_atr_pct = self._calculate_atr_pct(14) # Example ATR
        self.last_gap_pct = self._calculate_gap_pct(bar.open) # NEW: Calculate gap_pct
        self.last_rel_volume = self._calculate_rel_volume(bar.volume) # NEW: Calculate rel_volume
        
        # Determine market trend for regime classification
        if self.last_short_sma and self.last_long_sma:
            if self.last_short_sma > self.last_long_sma * 1.001: # Short > Long by 0.1%
                self.last_market_trend = "up"
            elif self.last_short_sma < self.last_long_sma * 0.999: # Short < Long by 0.1%
                self.last_market_trend = "down"
            else:
                self.last_market_trend = "sideways"

        # During ORB range building
        if self.market_open_et <= bar_time_et < self.range_end_et:
            self.orb_high = max(self.orb_high, bar.high)
            self.orb_low = min(self.orb_low, bar.low)
            return

        # After ORB range is established
        if bar_time_et >= self.range_end_et and not self.range_established:
            self.range_established = True
            logger.info(f"LOCKED {self.ticker} | ${self.orb_low:.2f}-${self.orb_high:.2f}")

        # Check for position closure if a position is active (during market hours)
        if self.position_taken and self.active_trade_order_id:
            exit_price = None
            pnl = 0.0
            if bar.low <= self.active_trade_sl_price:
                exit_price = self.active_trade_sl_price
                logger.info(f"STOP LOSS HIT {self.ticker} @ ${exit_price:.2f}")
            elif bar.high >= self.active_trade_tp_price:
                exit_price = self.active_trade_tp_price
                logger.info(f"TAKE PROFIT HIT {self.ticker} @ ${exit_price:.2f}")

            if exit_price is not None:
                pnl = (exit_price - self.active_trade_entry_price) * self.active_trade_qty
                self.trader.logger_obj.update_exit_details(
                    self.active_trade_order_id,
                    exit_price,
                    pnl,
                    bar.timestamp # Use bar timestamp for exit
                )
                # Log trade features after trade closure
                if self.active_trade_id and self.current_regime and self.current_candidate_id and self.current_strategy_type:
                    orb_width_pct = (self.orb_high - self.orb_low) / self.orb_low * 100 if self.orb_low > 0 else 0
                    self.adaptive_learner.log_trade_features(
                        trade_id=self.active_trade_id,
                        ticker=self.ticker,
                        regime=self.current_regime,
                        candidate_id=self.current_candidate_id,
                        strategy_type=self.current_strategy_type, # NEW
                        orb_width_pct=orb_width_pct,
                        gap_pct=self.last_gap_pct,
                        rel_volume=self.last_rel_volume,
                        atr_pct=self.last_atr_pct,
                        market_trend=self.last_market_trend,
                        rsi_value=self.last_rsi_value,
                        short_sma=self.last_short_sma,
                        long_sma=self.last_long_sma,
                    )

                self.position_taken = False
                self.active_trade_id = None 
                self.active_trade_order_id = None 
                self.active_trade_entry_price = 0.0
                self.active_trade_sl_price = 0.0
                self.active_trade_tp_price = 0.0
                self.active_trade_qty = 0
                self.current_regime = "unknown"
                self.current_candidate_id = "default"
                self.current_strategy_type = "ORB"
                return

        # If no position taken, check for new trade opportunities based on selected strategy
        if not self.position_taken:
            # Classify regime and select adaptive config
            self.current_regime = self.adaptive_learner.classify_regime(
                ticker=self.ticker, current_price=bar.close, 
                orb_width_pct=(self.orb_high - self.orb_low) / self.orb_low * 100 if self.orb_low > 0 else 0,
                gap_pct=self.last_gap_pct, atr_pct=self.last_atr_pct, rel_volume=self.last_rel_volume,
                rsi_value=self.last_rsi_value, short_sma=self.last_short_sma, long_sma=self.last_long_sma, 
                market_trend=self.last_market_trend
            )
            
            adaptive_config_dict, candidate_id, strategy_type, stats = self.adaptive_learner.select_candidate(
                ticker=self.ticker,
                regime=self.current_regime,
                default_config=self.base_config # Pass the base config to be merged
            )
            self.current_candidate_id = candidate_id
            self.current_strategy_type = strategy_type
            logger.info(f"ADAPTIVE | {self.ticker} | Regime: {self.current_regime} | Strategy: {self.current_strategy_type} | Config: {self.current_candidate_id}")

            # --- Entry Logic for different strategies ---
            order_details = None
            if self.current_strategy_type == "ORB":
                if self.range_established and bar.close > self.orb_high:
                    logger.info(f"ORB BREAKOUT {self.ticker} @ ${bar.close:.2f}")
                    order_details = await asyncio.to_thread(
                        self.trader.execute_trade_setup, self.ticker, bar.close, adaptive_config_dict
                    )
            elif self.current_strategy_type == "SMA_CROSS":
                # NEW: Correct SMA Crossover logic
                if len(self.closes) >= max(adaptive_config_dict.get("short_sma_period", 0), adaptive_config_dict.get("long_sma_period", 0)) + 1:
                    prev_short_sma = self._calculate_sma(adaptive_config_dict["short_sma_period"]) # SMA for previous bar
                    prev_long_sma = self._calculate_sma(adaptive_config_dict["long_sma_period"]) # SMA for previous bar
                    
                    # Recalculate SMAs for current bar (using current self.closes)
                    current_short_sma = self._calculate_sma(adaptive_config_dict["short_sma_period"])
                    current_long_sma = self._calculate_sma(adaptive_config_dict["long_sma_period"])

                    if (prev_short_sma is not None and prev_long_sma is not None and
                        current_short_sma is not None and current_long_sma is not None and
                        prev_short_sma <= prev_long_sma and current_short_sma > current_long_sma): # Bullish crossover
                        logger.info(f"SMA CROSSOVER {self.ticker} @ ${bar.close:.2f} (Short: {current_short_sma:.2f}, Long: {current_long_sma:.2f})")
                        order_details = await asyncio.to_thread(
                            self.trader.execute_trade_setup, self.ticker, bar.close, adaptive_config_dict
                        )
            elif self.current_strategy_type == "RSI_MR":
                rsi_period = adaptive_config_dict.get("rsi_period", 14)
                oversold_level = adaptive_config_dict.get("oversold_level", 30)
                if self.last_rsi_value is not None and self.last_rsi_value < oversold_level:
                    logger.info(f"RSI OVERSOLD {self.ticker} @ ${bar.close:.2f} (RSI: {self.last_rsi_value})")
                    order_details = await asyncio.to_thread(
                        self.trader.execute_trade_setup, self.ticker, bar.close, adaptive_config_dict
                    )
            
            if order_details:
                self.position_taken = True
                self.active_trade_id = self.trader.logger_obj._get_connection().execute("SELECT id FROM trades ORDER BY id DESC LIMIT 1").fetchone()["id"]
                self.active_trade_order_id = order_details["order_id"]
                self.active_trade_entry_price = order_details["entry_price"]
                self.active_trade_sl_price = order_details["stop_loss_price"]
                self.active_trade_tp_price = order_details["take_profit_price"]
                self.active_trade_qty = order_details["quantity"]
            else:
                # Only log warning if a strategy was selected but no order was placed due to internal logic
                # ORB strategy might not trigger if range not established or price not above high
                # SMA/RSI might not trigger if conditions not met
                if self.current_strategy_type != "ORB" or (self.range_established and bar.close > self.orb_high):
                    logger.warning(f"Order submission failed for {self.ticker} with strategy {self.current_strategy_type}")

# =============================================================================
# ROBUST WEBSOCKET MANAGER
# =============================================================================
class RobustWebSocketManager:
    def __init__(self, api_key: str, secret_key: str, max_retries: int = 5):
        self.api_key = api_key
        self.secret_key = secret_key
        self.max_retries = max_retries
        self.retry_count = 0
        self.stream: Optional[StockDataStream] = None
        self.subscriptions: Dict[str, List[Tuple[callable, str]]] = {} # Store (handler, symbol) tuples

    async def connect_with_retry(self) -> StockDataStream:
        for attempt in range(self.max_retries):
            try:
                self.stream = StockDataStream(self.api_key, self.secret_key)
                logger.info(f"WebSocket connected on attempt {attempt + 1}")
                self.retry_count = 0
                return self.stream
            except Exception as e:
                wait_time = 2 ** attempt
                logger.warning(f"Connection failed: {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        raise ConnectionError("Could not establish WebSocket")

    def subscribe_bars(self, handler, *symbols):
        if self.stream:
            for symbol in symbols:
                self.stream.subscribe_bars(handler, symbol)
                if 'bars' not in self.subscriptions:
                    self.subscriptions['bars'] = []
                # Store handler and symbol as a tuple for re-subscription
                if (handler, symbol) not in self.subscriptions['bars']:
                    self.subscriptions['bars'].append((handler, symbol))
        else:
            logger.error("Stream not connected. Cannot subscribe.")

    async def run_with_reconnect(self):
        while True:
            try:
                if not self.stream:
                    self.stream = await self.connect_with_retry()
                    # Re-apply all previous subscriptions after reconnect
                    for sub_type, items in self.subscriptions.items():
                        if sub_type == 'bars':
                            if items:
                                for handler, symbol in items:
                                    self.stream.subscribe_bars(handler, symbol)
                                    logger.info(f"Re-subscribed to bars for {symbol} with handler {handler.__name__}")

                await self.stream._run_forever()
            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)
                self.retry_count += 1
                if self.retry_count >= self.max_retries:
                    logger.critical("Max retries reached. Shutting down.")
                    break
                wait_time = 2 ** self.retry_count
                logger.info(f"Reconnecting in {wait_time}s...")
                await asyncio.sleep(wait_time)
                self.stream = None # Force re-connection

# =============================================================================
# MAIN ENGINE - MODIFIED FOR ADAPTIVE LAYER
# =============================================================================
class UpGainPulseEngine:
    def __init__(self, tickers: List[str], config: Dict):
        self.tickers = tickers
        self.config = config
        validate_config(self.config)
        self.logger_obj = TradeLogger()
        alpaca_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
        validator = AccountValidator(alpaca_client)
        self.trader = AlpacaPaperTrader(API_KEY, SECRET_KEY, self.logger_obj, validator)
        self.adaptive_learner = AdaptiveLearner() # NEW: Initialize AdaptiveLearner
        self.state_machines = {ticker: TradingStateMachine(ticker, self.trader, config, self.adaptive_learner) for ticker in tickers} # NEW: Pass adaptive_learner and use TradingStateMachine
        self.ws_manager = RobustWebSocketManager(API_KEY, SECRET_KEY)

    async def run(self):
        try:
            logger.info(f"START | Tickers: {', '.join(self.tickers)} | Risk: ${self.config.get('risk_per_trade_usd', 50)}/trade")
            stream = await self.ws_manager.connect_with_retry()
            
            async def handle_bar(bar):
                ticker = bar.symbol
                if ticker in self.state_machines:
                    await self.state_machines[ticker].process_minute_bar(bar)

            # Correctly subscribe to bars for all tickers
            # MODIFIED: Store handler and symbols as tuples for re-subscription
            for ticker_sym in self.tickers:
                self.ws_manager.subscribe_bars(handle_bar, ticker_sym)
            
            logger.info(f"Subscribed to {len(self.tickers)} tickers")
            await self.ws_manager.run_with_reconnect()
        except Exception as e:
            logger.error(f"Engine error: {e}", exc_info=True)
            raise
        finally:
            logger.info("Shutdown complete")

async def main():
    TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]
    config = {"account_capital": 500.0, "risk_per_trade_usd": 50.0, 
              "stop_loss_pct": 0.002, "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
    engine = UpGainPulseEngine(tickers=TICKERS, config=config)
    await engine.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        exit(1)
