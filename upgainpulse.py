import os
import sqlite3
import asyncio
import math
import threading
import logging
from datetime import datetime, time, timedelta
from typing import Optional, Dict, List
from pytz import timezone
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.models import TakeProfitRequest, StopLossRequest
from alpaca.trading.errors import APIError
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

from adaptive_strategy import AdaptiveLearner, CandidateConfig # NEW: Import AdaptiveLearner

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
        risk = float(config.get("risk_per_trade_usd", 50.0))
        sl_cents = float(config.get("stop_loss_cents", 10))
        multiplier = float(config.get("position_size_multiplier", 1.0))
        rr_ratio = float(config.get("risk_reward_ratio", 2.0))
        account_capital = float(config.get("account_capital", 0.0)) # Ensure account_capital is checked

        if risk <= 0:
            raise ConfigError("risk_per_trade_usd must be > 0")
        if sl_cents <= 0:
            raise ConfigError("stop_loss_cents must be > 0")
        if multiplier <= 0:
            raise ConfigError("position_size_multiplier must be > 0")
        if rr_ratio <= 0:
            raise ConfigError("risk_reward_ratio must be > 0")
        if rr_ratio > 10:
            raise ConfigError("risk_reward_ratio seems unrealistic (> 10)")
        if account_capital <= 0:
            logger.warning("account_capital not set or <= 0. Using available buying power as cap.")

        logger.info(f"Config validated: risk=${risk}, SL={sl_cents}c, multiplier={multiplier}x, RR={rr_ratio}:1")
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Config type error: {e}")

def calculate_position_size(current_price: float, config: Dict, available_buying_power: float) -> int:
    """
    Correct position sizing:
    1) Size from risk per share
    2) Cap by configured account capital
    3) Cap by live available buying power
    """
    if current_price <= 0:
        return 0

    risk_per_trade = float(config.get("risk_per_trade_usd", 50.0))
    stop_loss_cents = float(config.get("stop_loss_cents", 10))
    multiplier = float(config.get("position_size_multiplier", 1.0))
    account_capital = float(config.get("account_capital", available_buying_power)) # Use BP if not set

    stop_loss_dollars = stop_loss_cents / 100.0
    if stop_loss_dollars <= 0:
        return 0

    qty_by_risk = math.floor((risk_per_trade / stop_loss_dollars) * multiplier)
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
            # MODIFIED: Added regime and candidate_id columns
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            logger.info("Trade database initialized")
 
    def log_entry(self, ticker: str, setup_type: str, entry_price: float, qty: int, sl: float, tp: float, 
                  order_id: Optional[str] = None, regime: Optional[str] = None, candidate_id: Optional[str] = None) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                # MODIFIED: Added regime and candidate_id to insert statement
                cursor.execute('''
                    INSERT INTO trades (timestamp_entry, ticker, setup_type, entry_price, quantity, stop_loss, take_profit, status, order_id, regime, candidate_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (timestamp, ticker, setup_type, entry_price, qty, sl, tp, 'OPEN', order_id, regime, candidate_id))
                conn.commit()
                logger.info(f"OPEN | {ticker} ${entry_price:.2f} x{qty} | SL ${sl:.2f} | TP ${tp:.2f} | Order: {order_id} | Regime: {regime} | Config: {candidate_id}")
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
# ALPACA PAPER TRADER
# =============================================================================
class AlpacaPaperTrader:
    def __init__(self, api_key: str, secret_key: str, logger_obj: TradeLogger, account_validator: AccountValidator):
        self.client = TradingClient(api_key, secret_key, paper=True)
        self.logger_obj = logger_obj
        self.validator = account_validator
        self.last_order_time = {}
        self.order_cooldown = 1.0 # seconds

    def execute_orb_setup(self, ticker: str, current_price: float, config: Dict) -> Optional[Dict]:
        now = datetime.now().timestamp()
        last_time = self.last_order_time.get(ticker, 0)
        if now - last_time < self.order_cooldown:
            return None

        try:
            risk = float(config.get("risk_per_trade_usd", 50.0))
            stop_loss_cents = float(config.get("stop_loss_cents", 10))
            multiplier = float(config.get("position_size_multiplier", 1.0))
            rr_ratio = float(config.get("risk_reward_ratio", 2.0))

            available_bp = self.validator.get_buying_power()
            if available_bp <= 0:
                logger.error("No buying power available")
                return None

            qty = calculate_position_size(current_price, config, available_bp)
            if qty <= 0:
                logger.error(
                    f"Qty calc failed | ticker={ticker} | price=${current_price:.2f} | "
                    f"risk=${risk:.2f} | stop=${stop_loss_cents:.2f} | bp=${available_bp:.2f}"
                )
                return None

            logger.info(f"SIZING {ticker} | qty={qty} | required_capital=${current_price * qty:.2f} | risk=${risk:.2f} | stop=${stop_loss_cents:.2f} | rr={rr_ratio:.2f}")

            sl_price = round(current_price - (stop_loss_cents / 100.0), 2)
            tp_price = round(current_price + ((stop_loss_cents / 100.0) * rr_ratio), 2)

            order_data = MarketOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp_price),
                stop_loss=StopLossRequest(stop_price=sl_price)
            )
            order = self.client.submit_order(order_data)
            self.last_order_time[ticker] = now
            logger.info(f"ORDER {ticker} | {order.id}")
            # MODIFIED: Added regime and candidate_id to log_entry
            self.logger_obj.log_entry(ticker, "ORB_LONG", current_price, qty, sl_price, tp_price, order.id, 
                                      regime=config.get("current_regime"), candidate_id=config.get("candidate_id"))
            return {
                "order_id": order.id,
                "entry_price": current_price,
                "quantity": qty,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
            }
        except APIError as e:
            logger.error(f"API Error: {getattr(e, 'message', str(e))}")
            return None
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            return None

# =============================================================================
# ORB STATE MACHINE - MODIFIED FOR ADAPTIVE LAYER
# =============================================================================
class ORBStateMachine:
    def __init__(self, ticker: str, trader: AlpacaPaperTrader, config: Dict, adaptive_learner: AdaptiveLearner):
        self.ticker = ticker
        self.trader = trader
        self.config = config
        self.adaptive_learner = adaptive_learner # NEW: AdaptiveLearner instance
        self.market_open_et = time(9, 30)
        self.range_end_et = time(9, 45)
        self.reset_daily_state()

        # Active trade details
        self.active_trade_id: Optional[int] = None # NEW: Store internal trade ID for feature logging
        self.active_trade_order_id: Optional[str] = None
        self.active_trade_entry_price: float = 0.0
        self.active_trade_sl_price: float = 0.0
        self.active_trade_tp_price: float = 0.0
        self.active_trade_qty: int = 0
        self.current_regime: str = "unknown" # NEW: Store current regime
        self.current_candidate_id: str = "default" # NEW: Store current candidate ID

    def reset_daily_state(self):
        self.orb_high = 0.0
        self.orb_low = float('inf')
        self.range_established = False
        self.position_taken = False
        self.last_reset_date = datetime.now().date()
        self.active_trade_id = None # NEW: Reset internal trade ID
        self.active_trade_order_id = None
        self.active_trade_entry_price = 0.0
        self.active_trade_sl_price = 0.0
        self.active_trade_tp_price = 0.0
        self.active_trade_qty = 0
        self.current_regime = "unknown"
        self.current_candidate_id = "default"

    def check_daily_reset(self):
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.reset_daily_state()
            logger.info(f"RESET {self.ticker}")

    async def process_minute_bar(self, bar: Bar):
        self.check_daily_reset()
        bar_time_et = convert_utc_to_et(bar.timestamp).time()

        # During ORB range building
        if self.market_open_et <= bar_time_et < self.range_end_et:
            self.orb_high = max(self.orb_high, bar.high)
            self.orb_low = min(self.orb_low, bar.low)
            return

        # After ORB range is established
        if bar_time_et >= self.range_end_et and not self.range_established:
            self.range_established = True
            logger.info(f"LOCKED {self.ticker} | ${self.orb_low:.2f}-${self.orb_high:.2f}")

        # Check for position closure if a position is active
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
                # NEW: Log trade features after trade closure
                if self.active_trade_id and self.current_regime and self.current_candidate_id:
                    # Placeholder for actual feature values - you'll need to calculate these
                    orb_width_pct = (self.orb_high - self.orb_low) / self.orb_low * 100 if self.orb_low > 0 else 0
                    gap_pct = 0.0 # Placeholder
                    rel_volume = 1.0 # Placeholder
                    atr_pct = 0.0 # Placeholder
                    market_trend = "unknown" # Placeholder

                    self.adaptive_learner.log_trade_features(
                        trade_id=self.active_trade_id,
                        ticker=self.ticker,
                        regime=self.current_regime,
                        candidate_id=self.current_candidate_id,
                        orb_width_pct=orb_width_pct,
                        gap_pct=gap_pct,
                        rel_volume=rel_volume,
                        atr_pct=atr_pct,
                        market_trend=market_trend,
                    )

                self.position_taken = False
                self.active_trade_id = None # NEW: Clear internal trade ID
                self.active_trade_order_id = None # Clear active trade
                self.active_trade_entry_price = 0.0
                self.active_trade_sl_price = 0.0
                self.active_trade_tp_price = 0.0
                self.active_trade_qty = 0
                self.current_regime = "unknown"
                self.current_candidate_id = "default"
                return

        # If range established and no position taken, check for breakout
        if self.range_established and not self.position_taken:
            if bar.close > self.orb_high:
                logger.info(f"BREAKOUT {self.ticker} @ ${bar.close:.2f}")
                
                # NEW: Classify regime and select adaptive config
                # Placeholder values for regime classification - replace with real data
                atr_pct = 0.0 # Calculate actual ATR % from recent bars
                gap_pct = 0.0 # Calculate actual gap % from previous close
                market_above_ma = True # Determine if market is above key moving average
                rel_volume = 1.0 # Calculate actual relative volume

                self.current_regime = self.adaptive_learner.classify_regime(
                    atr_pct=atr_pct, gap_pct=gap_pct, market_above_ma=market_above_ma, rel_volume=rel_volume
                )
                # Create a temporary config dict to pass to select_candidate
                temp_config_for_adaptive = {
                    "risk_per_trade_usd": self.config.get("risk_per_trade_usd", 50.0),
                    "stop_loss_cents": self.config.get("stop_loss_cents", 10),
                    "position_size_multiplier": self.config.get("position_size_multiplier", 1.0),
                    "risk_reward_ratio": self.config.get("risk_reward_ratio", 2.0),
                    "account_capital": self.config.get("account_capital", 0.0)
                }
                adaptive_config, candidate_id, stats = self.adaptive_learner.select_candidate(
                    ticker=self.ticker,
                    regime=self.current_regime,
                    default_config=temp_config_for_adaptive
                )
                self.current_candidate_id = candidate_id
                logger.info(f"ADAPTIVE | {self.ticker} | Regime: {self.current_regime} | Config: {self.current_candidate_id} | SL: {adaptive_config.get("stop_loss_cents")}c | RR: {adaptive_config.get("risk_reward_ratio")}:1")

                order_details = await asyncio.to_thread(
                    self.trader.execute_orb_setup, self.ticker, bar.close, adaptive_config # Use adaptive_config
                )
                if order_details:
                    self.position_taken = True
                    # NEW: Fetch the internal trade ID from the logger after entry
                    # This assumes log_entry returns the ID or you can query it
                    # For now, we'll use a placeholder or query the last inserted ID
                    # A more robust solution would be to have log_entry return the ID
                    self.active_trade_id = self.trader.logger_obj._get_connection().execute("SELECT id FROM trades ORDER BY id DESC LIMIT 1").fetchone()["id"]
                    self.active_trade_order_id = order_details["order_id"]
                    self.active_trade_entry_price = order_details["entry_price"]
                    self.active_trade_sl_price = order_details["stop_loss_price"]
                    self.active_trade_tp_price = order_details["take_profit_price"]
                    self.active_trade_qty = order_details["quantity"]
                else:
                    logger.warning(f"Order submission failed for {self.ticker}")

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
        self.subscriptions: Dict[str, List] = {} # Store subscriptions to re-apply

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
            self.stream.subscribe_bars(handler, *symbols)
            for symbol in symbols:
                if 'bars' not in self.subscriptions:
                    self.subscriptions['bars'] = []
                # Store handler and symbols together for re-subscription
                # This assumes one handler for all bars, which is true in your main.
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
                                # Re-subscribe each (handler, symbol) pair
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
        self.state_machines = {ticker: ORBStateMachine(ticker, self.trader, config, self.adaptive_learner) for ticker in tickers} # NEW: Pass adaptive_learner
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
    config = {"account_capital": 500.0, "risk_per_trade_usd": 50.0, "stop_loss_cents": 10, "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
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
