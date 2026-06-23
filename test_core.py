import unittest
import time
from unittest.mock import Mock, patch, MagicMock, PropertyMock
import threading
import sqlite3
import os
import tempfile
import asyncio
from datetime import datetime, time, timedelta
from collections import deque
from pytz import timezone

ET = timezone('US/Eastern') # Defined in upgainpulse.py
UTC = timezone('UTC') # Defined in upgainpulse.py

# =============================================================================
# Mock external dependencies before importing upgainpulse
# =============================================================================
import sys

# Create a real Exception subclass for APIError so except clauses work
class MockAPIError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message # Alpaca APIError has a .message attribute

# Mock alpaca modules
sys.modules['alpaca'] = MagicMock()
sys.modules['alpaca.trading'] = MagicMock()
sys.modules['alpaca.trading.client'] = MagicMock()
sys.modules['alpaca.trading.requests'] = MagicMock()
sys.modules['alpaca.trading.enums'] = MagicMock()
sys.modules['alpaca.trading.models'] = MagicMock()
sys.modules['alpaca.trading.errors'] = MagicMock()
sys.modules['alpaca.data'] = MagicMock()
sys.modules['alpaca.data.live'] = MagicMock()
sys.modules['alpaca.data.models'] = MagicMock()
sys.modules['dotenv'] = MagicMock()

# Set alpaca.trading.errors.APIError to be a real Exception subclass
sys.modules['alpaca.trading.errors'].APIError = MockAPIError

# Import upgainpulse (will use env vars and mocked modules)
from upgainpulse import *
from adaptive_strategy import AdaptiveLearner, CandidateConfig, DEFAULT_CANDIDATES # NEW: Import AdaptiveLearner and CandidateConfig

# Patch APIError in the module to be a real Exception subclass
upgainpulse.APIError = MockAPIError

# Helper: create trade table for analytics tests
CREATE_TRADES_TABLE = '''CREATE TABLE IF NOT EXISTS trades (
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
)'''

# =============================================================================
# Test: Timezone Conversion
# =============================================================================
class TestTimezoneConversion(unittest.TestCase):
    """Test UTC to ET conversion for market hours."""

    def test_convert_utc_to_et_during_market(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 13, 35, 0)) # 1:35 PM UTC = 9:35 AM ET
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 35)

    def test_convert_utc_to_et_naive_input(self):
        naive_dt = datetime(2026, 6, 15, 13, 35, 0) # Assume UTC if naive
        et_dt = convert_utc_to_et(naive_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 35)

    def test_convert_utc_to_et_premarket(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 8, 0, 0)) # 8:00 AM UTC = 4:00 AM ET
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 4)

    def test_convert_utc_to_et_afterhours(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 23, 0, 0)) # 11:00 PM UTC = 7:00 PM ET
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 19)

    def test_convert_utc_to_et_winter(self):
        utc_dt = UTC.localize(datetime(2026, 1, 15, 14, 30, 0)) # 2:30 PM UTC = 9:30 AM ET (EST is UTC-5)
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 30)

# =============================================================================
# Test: Configuration Validation
# =============================================================================
class TestConfigValidation(unittest.TestCase):
    """Test configuration parameter validation."""

    def test_valid_config(self):
        validate_config({"risk_per_trade_usd": 50.0, "stop_loss_pct": 0.002,
                         "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_risk_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 0, "stop_loss_pct": 0.002,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_risk_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": -10, "stop_loss_pct": 0.002,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_sl_pct_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_pct": 0,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_multiplier_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_pct": 0.002,
                             "position_size_multiplier": 0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_rr_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_pct": 0.002,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": -1.0, "account_capital": 500.0})

    def test_rr_unrealistic(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_pct": 0.002,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 100.0, "account_capital": 500.0})

    def test_missing_keys_use_defaults(self):
        # Should not raise an error, as defaults are used
        validate_config({"risk_per_trade_usd": 50.0})

    def test_config_type_error(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": "abc", "stop_loss_pct": 0.002,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

# =============================================================================
# Test: TradeLogger (Database)
# =============================================================================
class TestTradeLogger(unittest.TestCase):
    """Test SQLite trade logging with thread safety."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.logger = TradeLogger(db_path=self.db_path)

    def tearDown(self):
        self.logger._get_connection().close()
        # Ensure all connections are closed before unlinking
        import gc; gc.collect()
        try:
            os.close(self.db_fd)
        except OSError:
            pass # Already closed by mkstemp on some systems
        try:
            os.unlink(self.db_path)
        except PermissionError:
            pass # File might be locked on Windows, ignore for tests

    def test_create_table(self):
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades';")
        self.assertIsNotNone(cursor.fetchone())

    def test_log_entry(self):
        self.logger.log_entry("SPY", "ORB_LONG", 500.0, 1, 499.90, 500.20, "order_123")
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE ticker='SPY'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['ticker'], "SPY")
        self.assertEqual(row['setup_type'], "ORB_LONG")
        self.assertEqual(row['entry_price'], 500.0)
        self.assertEqual(row['quantity'], 1)
        self.assertEqual(row['stop_loss'], 499.90)
        self.assertEqual(row['take_profit'], 500.20)
        self.assertEqual(row['status'], "OPEN")
        self.assertEqual(row['order_id'], "order_123")
        self.assertIsNone(row['exit_price'])
        self.assertIsNone(row['pnl'])
        self.assertIsNone(row['regime']) 
        self.assertIsNone(row['candidate_id']) 
        self.assertIsNone(row['strategy_type']) # NEW: Check for new columns

    def test_log_entry_with_adaptive_data(self):
        self.logger.log_entry("SPY", "ORB", 500.0, 1, 499.90, 500.20, "order_124", 
                              regime="trend", candidate_id="base", strategy_type="ORB")
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE order_id='order_124'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['regime'], "trend")
        self.assertEqual(row['candidate_id'], "base")
        self.assertEqual(row['strategy_type'], "ORB")

    def test_log_entry_without_order_id(self):
        self.logger.log_entry("AAPL", "SMA_CROSS", 180.0, 2, 179.90, 182.00, None)
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE ticker='AAPL'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row['order_id']) 

    def test_update_exit_details(self):
        # First log an entry
        self.logger.log_entry("SPY", "ORB", 500.0, 1, 499.90, 500.20, "order_123")
        
        # Then update it
        exit_time = datetime.now() + timedelta(minutes=5)
        self.logger.update_exit_details("order_123", 499.90, -0.10, exit_time)

        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE order_id='order_123'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], "CLOSED")
        self.assertEqual(row['exit_price'], 499.90)
        self.assertEqual(row['pnl'], -0.10)
        self.assertEqual(row['timestamp_exit'], exit_time.strftime("%Y-%m-%d %H:%M:%S"))

    def test_thread_safety(self):
        errors = []

        def log_trade(ticker):
            try:
                for _ in range(5):
                    self.logger.log_entry(ticker, "ORB", 100.0, 1, 99.90, 102.00)
                    time.sleep(0.01)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for t in ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]:
            th = threading.Thread(target=log_trade, args=(t,))
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades")
        self.assertEqual(cursor.fetchone()[0], 25)

    def test_wal_mode(self):
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        self.assertEqual(cursor.fetchone()[0], "wal")


# =============================================================================
# Test: AccountValidator
# =============================================================================
class TestAccountValidator(unittest.TestCase):
    """Test account buying power validation."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.patcher = patch('upgainpulse.APIError', MockAPIError)
        self.patcher.start()
        self.validator = AccountValidator(self.mock_client)

    def tearDown(self):
        self.patcher.stop()

    def test_get_buying_power_success(self):
        mock_account = MagicMock(buying_power="1000.00")
        self.mock_client.get_account.return_value = mock_account
        self.assertEqual(self.validator.get_buying_power(), 1000.0)

    def test_get_buying_power_api_error_returns_zero(self):
        self.mock_client.get_account.side_effect = MockAPIError("API error")
        self.assertEqual(self.validator.get_buying_power(), 0.0)

    def test_sufficient_bp(self):
        self.mock_client.get_account.return_value = MagicMock(buying_power="1000.00")
        self.assertTrue(self.validator.check_buying_power(500.0))

    def test_insufficient_bp(self):
        self.mock_client.get_account.return_value = MagicMock(buying_power="100.00")
        self.assertFalse(self.validator.check_buying_power(500.0))

    def test_exact_bp(self):
        self.mock_client.get_account.return_value = MagicMock(buying_power="500.00")
        self.assertTrue(self.validator.check_buying_power(500.0))

    def test_bp_zero(self):
        self.mock_client.get_account.return_value = MagicMock(buying_power="0.00")
        self.assertFalse(self.validator.check_buying_power(1.0))

    def test_api_error_returns_false(self):
        self.mock_client.get_account.side_effect = MockAPIError("API error")
        self.assertFalse(self.validator.check_buying_power(500.0))

# =============================================================================
# Test: Position Sizing
# =============================================================================
class TestPositionSizing(unittest.TestCase):
    """Test the position sizing formula."""

    def setUp(self):
        self.config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_pct": 0.002, # Use percentage stop loss
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0,
        }

    def test_spy_at_500(self):
        # $50 risk / ($500 * 0.002) = $50 / $1 = 50 shares
        self.assertEqual(calculate_position_size(500.0, self.config, 5000.0), 50)

    def test_spy_at_250(self):
        # $50 risk / ($250 * 0.002) = $50 / $0.5 = 100 shares
        self.assertEqual(calculate_position_size(250.0, self.config, 5000.0), 100)

    def test_qqq_at_400(self):
        # $50 risk / ($400 * 0.002) = $50 / $0.8 = 62 shares
        self.assertEqual(calculate_position_size(400.0, self.config, 5000.0), 62)

    def test_max_shares_low_price(self):
        # $50 risk / ($10 * 0.002) = $50 / $0.02 = 2500 shares
        # Capped by $500 capital / $10 price = 50 shares
        self.assertEqual(calculate_position_size(10.0, self.config, 500.0), 50)

    def test_small_risk_large_stop(self):
        cfg = dict(self.config)
        cfg["risk_per_trade_usd"] = 0.25
        cfg["stop_loss_pct"] = 0.01 # 1% stop
        # $0.25 risk / ($500 * 0.01) = $0.25 / $5 = 0 shares
        self.assertEqual(calculate_position_size(500.0, cfg, 5000.0), 0)

    def test_double_multiplier(self):
        cfg = dict(self.config)
        cfg["position_size_multiplier"] = 2.0
        # $50 risk / ($250 * 0.002) * 2 = $50 / $0.5 * 2 = 200 shares
        self.assertEqual(calculate_position_size(250.0, cfg, 5000.0), 200)

    def test_large_account_uses_risk_limit(self):
        cfg = dict(self.config)
        cfg["account_capital"] = 100000.0
        # $50 risk / ($500 * 0.002) = 50 shares (risk limit is lower than capital limit)
        self.assertEqual(calculate_position_size(500.0, cfg, 100000.0), 50)

    def test_zero_current_price(self):
        self.assertEqual(calculate_position_size(0.0, self.config, 500.0), 0)

    def test_zero_stop_loss_pct(self):
        cfg = dict(self.config)
        cfg["stop_loss_pct"] = 0
        self.assertEqual(calculate_position_size(500.0, cfg, 500.0), 0)

    def test_insufficient_buying_power(self):
        # $50 risk / ($500 * 0.002) = 50 shares. But only $10 BP available.
        self.assertEqual(calculate_position_size(500.0, self.config, 10.0), 0)

# =============================================================================
# Test: TP/SL Calculation (Updated for percentage-based stops)
# =============================================================================
class TestTPSLCalculation(unittest.TestCase):
    """Test take-profit and stop-loss price calculations with percentage stops."""

    def setUp(self):
        self.config = {
            "risk_per_trade_usd": 50.0,
            "stop_loss_pct": 0.002, # 0.2% stop
            "take_profit_rr": 2.0,
            "take_profit_pct": 0.005, # 0.5% target for RSI
            "strategy_type": "ORB"
        }

    def test_orb_basic(self):
        current_price = 500.0
        sl_price = round(current_price * (1 - self.config["stop_loss_pct"]), 2) # 500 * (1 - 0.002) = 499.00
        risk_dollars = current_price * self.config["stop_loss_pct"] # 500 * 0.002 = 1.00
        tp_price = round(current_price + (risk_dollars * self.config["take_profit_rr"]), 2) # 500 + (1.00 * 2.0) = 502.00
        
        self.assertEqual(sl_price, 499.00)
        self.assertEqual(tp_price, 502.00)

    def test_rsi_mr_basic(self):
        current_price = 100.0
        config = {**self.config, "strategy_type": "RSI_MR"}
        sl_price = round(current_price * (1 - config["stop_loss_pct"]), 2) # 100 * (1 - 0.002) = 99.80
        tp_price = round(current_price * (1 + config["take_profit_pct"]), 2) # 100 * (1 + 0.005) = 100.50

        self.assertEqual(sl_price, 99.80)
        self.assertEqual(tp_price, 100.50)

    def test_sma_cross_high_rr(self):
        current_price = 200.0
        config = {**self.config, "strategy_type": "SMA_CROSS", "take_profit_rr": 3.0}
        sl_price = round(current_price * (1 - config["stop_loss_pct"]), 2) # 200 * (1 - 0.002) = 199.60
        risk_dollars = current_price * config["stop_loss_pct"] # 200 * 0.002 = 0.40
        tp_price = round(current_price + (risk_dollars * config["take_profit_rr"]), 2) # 200 + (0.40 * 3.0) = 201.20

        self.assertEqual(sl_price, 199.60)
        self.assertEqual(tp_price, 201.20)

    def test_invalid_tp_less_than_sl(self):
        current_price = 100.0
        config = {**self.config, "stop_loss_pct": 0.01, "take_profit_rr": 0.5} # SL 1%, RR 0.5:1
        sl_price = round(current_price * (1 - config["stop_loss_pct"]), 2) # 99.00
        risk_dollars = current_price * config["stop_loss_pct"] # 1.00
        tp_price = round(current_price + (risk_dollars * config["take_profit_rr"]), 2) # 100 + (1.00 * 0.5) = 100.50
        
        # This scenario should be caught by the trader, but here we just check calculation
        self.assertTrue(tp_price > sl_price) # Should still be true if RR > 0

# =============================================================================
# Test: AlpacaPaperTrader execute_trade_setup (formerly execute_orb_setup)
# =============================================================================
class TestAlpacaPaperTrader(unittest.TestCase):
    """Test order execution logic."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_logger = MagicMock(spec=TradeLogger)
        self.mock_validator = MagicMock(spec=AccountValidator)
        self.mock_validator.get_buying_power.return_value = 5000.0 # Sufficient BP

        with patch('upgainpulse.TradingClient', return_value=self.mock_client), \
             patch('upgainpulse.APIError', MockAPIError):
            self.trader = AlpacaPaperTrader(
                api_key='test', secret_key='test',
                logger_obj=self.mock_logger,
                account_validator=self.mock_validator
            )

        self.config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_pct": 0.002, # 0.2% stop
            "position_size_multiplier": 1.0,
            "take_profit_rr": 2.0, # For ORB/SMA
            "take_profit_pct": 0.005, # For RSI
            "strategy_type": "ORB", # Default strategy type
            "current_regime": "trend", # For logging
            "candidate_id": "base" # For logging
        }

    def test_execute_trade_setup_orb_success(self):
        self.mock_client.submit_order.return_value = MagicMock(id="order_orb_123")
        order_details = self.trader.execute_trade_setup("SPY", 500.0, self.config)
        self.assertIsNotNone(order_details)
        self.assertTrue(self.mock_client.submit_order.called_once())
        self.mock_logger.log_entry.assert_called_once_with(
            "SPY", "ORB", 500.0, 50, 499.00, 502.00, "order_orb_123",
            regime="trend", candidate_id="base", strategy_type="ORB"
        )
        self.assertEqual(order_details["order_id"], "order_orb_123")
        self.assertEqual(order_details["entry_price"], 500.0)
        self.assertEqual(order_details["quantity"], 50)
        self.assertEqual(order_details["stop_loss_price"], 499.00)
        self.assertEqual(order_details["take_profit_price"], 502.00)
        self.assertEqual(order_details["strategy_type"], "ORB")

    def test_execute_trade_setup_rsi_mr_success(self):
        rsi_config = {**self.config, "strategy_type": "RSI_MR"}
        self.mock_client.submit_order.return_value = MagicMock(id="order_rsi_456")
        order_details = self.trader.execute_trade_setup("AAPL", 180.0, rsi_config)
        self.assertIsNotNone(order_details)
        self.assertTrue(self.mock_client.submit_order.called_once())
        # SL: 180 * (1 - 0.002) = 179.64
        # TP: 180 * (1 + 0.005) = 180.90
        self.mock_logger.log_entry.assert_called_once_with(
            "AAPL", "RSI_MR", 180.0, 138, 179.64, 180.90, "order_rsi_456",
            regime="trend", candidate_id="base", strategy_type="RSI_MR"
        )
        self.assertEqual(order_details["order_id"], "order_rsi_456")
        self.assertEqual(order_details["quantity"], 138) # 50 / (180*0.002) = 50 / 0.36 = 138.8 -> 138
        self.assertEqual(order_details["stop_loss_price"], 179.64)
        self.assertEqual(order_details["take_profit_price"], 180.90)
        self.assertEqual(order_details["strategy_type"], "RSI_MR")

    def test_execute_trade_setup_insufficient_bp(self):
        self.mock_validator.get_buying_power.return_value = 10.0 # Insufficient BP
        order_details = self.trader.execute_trade_setup("SPY", 500.0, self.config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_not_called()

    def test_execute_trade_setup_cooldown(self):
        self.trader.execute_trade_setup("SPY", 500.0, self.config) # First order
        order_details = self.trader.execute_trade_setup("SPY", 500.0, self.config) # Second order immediately
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_called_once() # Only first call should go through

    def test_execute_trade_setup_qty_zero(self):
        config = {**self.config, "risk_per_trade_usd": 0.25, "stop_loss_pct": 0.01} # Qty will be 0
        order_details = self.trader.execute_trade_setup("SPY", 500.0, config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_not_called()

    def test_execute_trade_setup_api_error(self):
        self.mock_client.submit_order.side_effect = MockAPIError("API error")
        order_details = self.trader.execute_trade_setup("SPY", 500.0, self.config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_called_once()

    def test_execute_trade_setup_invalid_tp_less_than_sl(self):
        config = {**self.config, "stop_loss_pct": 0.01, "take_profit_rr": 0.1} # TP will be < SL
        order_details = self.trader.execute_trade_setup("SPY", 100.0, config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_not_called()

# =============================================================================
# Test: TradingStateMachine (formerly ORB State Machine)
# =============================================================================
class TestTradingStateMachine(unittest.TestCase):
    """Test the adaptive trading state machine."""

    def setUp(self):
        self.mock_trader = MagicMock(spec=AlpacaPaperTrader)
        self.mock_trader.logger_obj = MagicMock(spec=TradeLogger) 
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) 
        self.base_config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_pct": 0.002, 
            "position_size_multiplier": 1.0,
            "take_profit_rr": 2.0,
            "take_profit_pct": 0.005,
        }
        self.sm = TradingStateMachine("SPY", self.mock_trader, self.base_config, self.mock_adaptive_learner) 

    def create_bar(self, hour, minute, open, high, low, close, volume=10000, symbol="SPY", day=15):
        dt = ET.localize(datetime(2026, 6, day, hour, minute, 0))
        mock_bar = MagicMock()
        mock_bar.symbol = symbol
        mock_bar.timestamp = dt.astimezone(UTC)
        mock_bar.open = open
        mock_bar.high = high
        mock_bar.low = low
        mock_bar.close = close
        mock_bar.volume = volume
        return mock_bar

    def test_initial_state(self):
        self.assertEqual(self.sm.orb_high, 0.0)
        self.assertEqual(self.sm.orb_low, float('inf'))
        self.assertFalse(self.sm.range_established)
        self.assertFalse(self.sm.position_taken)
        self.assertEqual(self.sm.current_regime, "unknown") 
        self.assertEqual(self.sm.current_candidate_id, "default") 
        self.assertEqual(self.sm.current_strategy_type, "ORB") # NEW: Check initial strategy type
        self.assertEqual(len(self.sm.bars), 0)
        self.assertEqual(len(self.sm.closes), 0)
        self.assertEqual(len(self.sm.volumes), 0)
        self.assertIsNone(self.sm.previous_day_close)

    async def test_build_range_during_orb_period(self):
        bar1 = self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5)
        await self.sm.process_minute_bar(bar1)
        self.assertEqual(self.sm.orb_high, 501.0)
        self.assertEqual(self.sm.orb_low, 499.0)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(len(self.sm.bars), 1)
        self.assertEqual(len(self.sm.closes), 1)
        self.assertEqual(len(self.sm.volumes), 1)

    async def test_lock_range_at_945(self):
        bar1 = self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5)
        await self.sm.process_minute_bar(bar1)
        bar_lock = self.create_bar(9, 45, 500.5, 502.0, 500.0, 501.0)
        await self.sm.process_minute_bar(bar_lock)
        self.assertTrue(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 502.0)
        self.assertEqual(self.sm.orb_low, 499.0) 

    async def test_orb_breakout_executes_order(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 500.5, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0 
        self.sm.orb_low = 499.0 

        # Mock adaptive learner to return an ORB config
        self.mock_adaptive_learner.classify_regime.return_value = "uptrend_orb_momentum"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {**self.base_config, "strategy_type": "ORB", "stop_loss_pct": 0.002, "take_profit_rr": 2.0},
            "orb_base_2_1",
            "ORB",
            {}
        )

        # Simulate successful order submission
        self.mock_trader.execute_trade_setup.return_value = {
            "order_id": "test_order_1",
            "entry_price": 502.50,
            "quantity": 50,
            "stop_loss_price": 501.49,
            "take_profit_price": 504.51,
            "strategy_type": "ORB"
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 1} 

        # Breakout bar
        breakout_bar = self.create_bar(9, 46, 502.0, 503.0, 501.0, 502.50)
        await self.sm.process_minute_bar(breakout_bar)

        self.assertTrue(self.sm.position_taken)
        self.mock_adaptive_learner.classify_regime.assert_called_once() 
        self.mock_adaptive_learner.select_candidate.assert_called_once() 
        self.mock_trader.execute_trade_setup.assert_called_once_with("SPY", 502.50, {
            **self.base_config, "strategy_type": "ORB", "stop_loss_pct": 0.002, "take_profit_rr": 2.0
        })
        self.assertEqual(self.sm.active_trade_order_id, "test_order_1")
        self.assertEqual(self.sm.active_trade_id, 1) 
        self.assertEqual(self.sm.current_regime, "uptrend_orb_momentum") 
        self.assertEqual(self.sm.current_candidate_id, "orb_base_2_1") 
        self.assertEqual(self.sm.current_strategy_type, "ORB") # NEW: Check current strategy type

    async def test_sma_crossover_executes_order(self):
        # Prime the SMA with enough bars for calculation
        for i in range(49):
            await self.sm.process_minute_bar(self.create_bar(9, 0, 100.0, 100.5, 99.5, 100.0, day=14, volume=5000))
        
        # Simulate SMA cross config
        sma_config = {**self.base_config, "strategy_type": "SMA_CROSS", "short_sma_period": 5, "long_sma_period": 20, "stop_loss_pct": 0.005, "take_profit_rr": 2.5}
        self.mock_adaptive_learner.classify_regime.return_value = "strong_uptrend_sma_cross"
        self.mock_adaptive_learner.select_candidate.return_value = (
            sma_config,
            "sma_5_20_trend",
            "SMA_CROSS",
            {}
        )

        # Simulate successful order submission
        self.mock_trader.execute_trade_setup.return_value = {
            "order_id": "test_order_sma_2",
            "entry_price": 101.00,
            "quantity": 10,
            "stop_loss_price": 100.49,
            "take_profit_price": 102.76,
            "strategy_type": "SMA_CROSS"
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 2}

        # Simulate bars leading to a bullish SMA crossover
        # Previous bar: short_sma < long_sma
        await self.sm.process_minute_bar(self.create_bar(9, 50, 100.0, 100.5, 99.5, 100.0, day=15, volume=5000)) # Bar 50
        # Current bar: short_sma > long_sma
        await self.sm.process_minute_bar(self.create_bar(9, 51, 100.5, 101.5, 100.0, 101.0, day=15, volume=15000)) # Bar 51, crossover

        self.assertTrue(self.sm.position_taken)
        self.mock_adaptive_learner.classify_regime.assert_called_once() 
        self.mock_adaptive_learner.select_candidate.assert_called_once() 
        self.mock_trader.execute_trade_setup.assert_called_once_with("SPY", 101.0, sma_config)
        self.assertEqual(self.sm.current_strategy_type, "SMA_CROSS")

    async def test_rsi_mean_reversion_executes_order(self):
        # Prime the RSI with enough bars for calculation
        for i in range(14):
            await self.sm.process_minute_bar(self.create_bar(9, 0, 100.0, 100.5, 99.5, 100.0, day=14, volume=5000))
        
        # Simulate RSI MR config
        rsi_config = {**self.base_config, "strategy_type": "RSI_MR", "rsi_period": 14, "oversold_level": 30, "take_profit_pct": 0.005}
        self.mock_adaptive_learner.classify_regime.return_value = "oversold_bounce"
        self.mock_adaptive_learner.select_candidate.return_value = (
            rsi_config,
            "rsi_14_oversold",
            "RSI_MR",
            {}
        )

        # Simulate successful order submission
        self.mock_trader.execute_trade_setup.return_value = {
            "order_id": "test_order_rsi_3",
            "entry_price": 99.00,
            "quantity": 10,
            "stop_loss_price": 98.80,
            "take_profit_price": 99.49,
            "strategy_type": "RSI_MR"
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 3}

        # Simulate bars leading to RSI oversold (e.g., several consecutive down closes)
        # For simplicity, we'll force the RSI value in the state machine for this test
        self.sm.last_rsi_value = 25 # Force oversold RSI
        await self.sm.process_minute_bar(self.create_bar(9, 55, 99.5, 99.5, 98.5, 99.0, day=15, volume=10000)) # RSI oversold

        self.assertTrue(self.sm.position_taken)
        self.mock_adaptive_learner.classify_regime.assert_called_once() 
        self.mock_adaptive_learner.select_candidate.assert_called_once() 
        self.mock_trader.execute_trade_setup.assert_called_once_with("SPY", 99.0, rsi_config)
        self.assertEqual(self.sm.current_strategy_type, "RSI_MR")

    async def test_no_breakout_below_range(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 500.5, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Mock adaptive learner to return an ORB config
        self.mock_adaptive_learner.classify_regime.return_value = "general_range"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {**self.base_config, "strategy_type": "ORB"},
            "orb_base_2_1",
            "ORB",
            {}
        )

        # Bar below high
        no_breakout_bar = self.create_bar(9, 46, 500.5, 500.5, 498.0, 499.50)
        await self.sm.process_minute_bar(no_breakout_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.execute_trade_setup.assert_not_called()
        self.mock_adaptive_learner.classify_regime.assert_called_once() 

    async def test_no_double_order(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 500.5, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Mock adaptive learner
        self.mock_adaptive_learner.classify_regime.return_value = "uptrend_orb_momentum"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {**self.base_config, "strategy_type": "ORB"},
            "orb_base_2_1",
            "ORB",
            {}
        )

        # First breakout
        self.mock_trader.execute_trade_setup.return_value = {
            "order_id": "test_order_1", "entry_price": 502.0, "quantity": 1,
            "stop_loss_price": 501.90, "take_profit_price": 502.10,
            "strategy_type": "ORB"
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 1}
        await self.sm.process_minute_bar(self.create_bar(9, 46, 502.0, 503.0, 501.0, 502.0))
        self.assertTrue(self.sm.position_taken)

        # Second breakout attempt
        self.mock_trader.execute_trade_setup.reset_mock() 
        self.mock_adaptive_learner.classify_regime.reset_mock()
        self.mock_adaptive_learner.select_candidate.reset_mock()
        await self.sm.process_minute_bar(self.create_bar(9, 47, 503.0, 504.0, 502.0, 503.0))
        self.assertFalse(self.mock_trader.execute_trade_setup.called)
        self.mock_adaptive_learner.classify_regime.assert_not_called() 
        self.mock_adaptive_learner.select_candidate.assert_not_called() 

    async def test_sl_hit_closes_position(self):
        # Simulate an active position
        self.sm.position_taken = True
        self.sm.active_trade_id = 1 
        self.sm.active_trade_order_id = "test_order_1"
        self.sm.active_trade_entry_price = 502.50
        self.sm.active_trade_sl_price = 502.40
        self.sm.active_trade_tp_price = 502.70
        self.sm.active_trade_qty = 1
        self.sm.current_regime = "trend"
        self.sm.current_candidate_id = "base"
        self.sm.current_strategy_type = "ORB"
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0
        self.sm.last_gap_pct = 0.5
        self.sm.last_rel_volume = 1.5
        self.sm.last_atr_pct = 1.0
        self.sm.last_market_trend = "up"
        self.sm.last_rsi_value = 60.0
        self.sm.last_short_sma = 501.0
        self.sm.last_long_sma = 500.0

        # Bar hits SL
        sl_hit_bar = self.create_bar(10, 0, 502.45, 502.45, 502.30, 502.35) 
        await self.sm.process_minute_bar(sl_hit_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.logger_obj.update_exit_details.assert_called_once_with(
            "test_order_1", 502.40, round((502.40 - 502.50) * 1, 2), sl_hit_bar.timestamp
        )
        self.mock_adaptive_learner.log_trade_features.assert_called_once_with(
            trade_id=1, ticker="SPY", regime="trend", candidate_id="base", strategy_type="ORB",
            orb_width_pct=Mock(return_value=0.0), gap_pct=0.5, rel_volume=1.5, atr_pct=1.0, market_trend="up",
            rsi_value=60.0, short_sma=501.0, long_sma=500.0
        ) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertIsNone(self.sm.active_trade_id)
        self.assertEqual(self.sm.current_regime, "unknown")
        self.assertEqual(self.sm.current_candidate_id, "default")
        self.assertEqual(self.sm.current_strategy_type, "ORB")

    async def test_tp_hit_closes_position(self):
        # Simulate an active position
        self.sm.position_taken = True
        self.sm.active_trade_id = 2 
        self.sm.active_trade_order_id = "test_order_2"
        self.sm.active_trade_entry_price = 502.50
        self.sm.active_trade_sl_price = 502.40
        self.sm.active_trade_tp_price = 502.70
        self.sm.active_trade_qty = 1
        self.sm.current_regime = "range"
        self.sm.current_candidate_id = "wide_rr"
        self.sm.current_strategy_type = "RSI_MR"
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0
        self.sm.last_gap_pct = -0.2
        self.sm.last_rel_volume = 0.8
        self.sm.last_atr_pct = 0.5
        self.sm.last_market_trend = "sideways"
        self.sm.last_rsi_value = 40.0
        self.sm.last_short_sma = 500.0
        self.sm.last_long_sma = 500.5

        # Bar hits TP
        tp_hit_bar = self.create_bar(10, 5, 502.70, 502.80, 502.65, 502.75) 
        await self.sm.process_minute_bar(tp_hit_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.logger_obj.update_exit_details.assert_called_once_with(
            "test_order_2", 502.70, round((502.70 - 502.50) * 1, 2), tp_hit_bar.timestamp
        )
        self.mock_adaptive_learner.log_trade_features.assert_called_once_with(
            trade_id=2, ticker="SPY", regime="range", candidate_id="wide_rr", strategy_type="RSI_MR",
            orb_width_pct=Mock(return_value=0.0), gap_pct=-0.2, rel_volume=0.8, atr_pct=0.5, market_trend="sideways",
            rsi_value=40.0, short_sma=500.0, long_sma=500.5
        ) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertIsNone(self.sm.active_trade_id)
        self.assertEqual(self.sm.current_regime, "unknown")
        self.assertEqual(self.sm.current_candidate_id, "default")
        self.assertEqual(self.sm.current_strategy_type, "ORB")

    async def test_daily_reset_triggers(self):
        self.sm.orb_high = 510.0
        self.sm.orb_low = 490.0
        self.sm.range_established = True
        self.sm.position_taken = True
        self.sm.active_trade_id = 3 
        self.sm.active_trade_order_id = "test_order_3"
        self.sm.current_regime = "high_vol_trend"
        self.sm.current_candidate_id = "tight_fast"
        self.sm.current_strategy_type = "SMA_CROSS"
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)

        # Process a bar on a new day
        bar = self.create_bar(9, 31, 500.0, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)

        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 500.0) 
        self.assertEqual(self.sm.orb_low, 498.0) 
        self.assertEqual(self.sm.last_reset_date, datetime.now().date())
        self.assertIsNone(self.sm.active_trade_id) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertEqual(self.sm.current_regime, "unknown") 
        self.assertEqual(self.sm.current_candidate_id, "default") 
        self.assertEqual(self.sm.current_strategy_type, "ORB")

    async def test_premarket_no_action(self):
        # Set current time to pre-market (e.g., 9:00 AM ET)
        bar = self.create_bar(9, 0, 500.0, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)
        self.assertEqual(self.sm.orb_high, 0.0) 
        self.mock_adaptive_learner.classify_regime.assert_not_called()
        self.mock_adaptive_learner.select_candidate.assert_not_called()

    async def test_afterhours_no_double_order(self):
        # Build range and take position
        await self.sm.process_minute_bar(self.create_bar(9, 35, 500.0, 501.0, 499.0, 500.5))
        await self.sm.process_minute_bar(self.create_bar(9, 45, 500.5, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Mock adaptive learner
        self.mock_adaptive_learner.classify_regime.return_value = "uptrend_orb_momentum"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {**self.base_config, "strategy_type": "ORB"},
            "orb_base_2_1",
            "ORB",
            {}
        )

        # First breakout
        self.mock_trader.execute_trade_setup.return_value = {
            "order_id": "order_afterhours_1", "entry_price": 502.0, "quantity": 1,
            "stop_loss_price": 501.90, "take_profit_price": 502.10,
            "strategy_type": "ORB"
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 4}
        await self.sm.process_minute_bar(self.create_bar(9, 46, 502.0, 503.0, 501.0, 502.0))
        self.assertTrue(self.sm.position_taken)
        self.mock_trader.execute_trade_setup.assert_called_once()

        # Simulate after-hours bar (e.g., 15:00 ET, after 4 PM market close)
        self.mock_trader.execute_trade_setup.reset_mock() 
        self.mock_adaptive_learner.classify_regime.reset_mock()
        self.mock_adaptive_learner.select_candidate.reset_mock()
        await self.sm.process_minute_bar(self.create_bar(15, 0, 510.0, 510.0, 505.0, 508.0))
        self.assertEqual(self.mock_trader.execute_trade_setup.call_count, 0) 
        self.mock_adaptive_learner.classify_regime.assert_not_called()
        self.mock_adaptive_learner.select_candidate.assert_not_called()

    async def test_market_close_closes_position(self):
        # Simulate an active position
        self.sm.position_taken = True
        self.sm.active_trade_id = 5 
        self.sm.active_trade_order_id = "test_order_market_close"
        self.sm.active_trade_entry_price = 100.0
        self.sm.active_trade_sl_price = 99.0
        self.sm.active_trade_tp_price = 102.0
        self.sm.active_trade_qty = 10
        self.sm.current_regime = "general_uptrend"
        self.sm.current_candidate_id = "orb_base_2_1"
        self.sm.current_strategy_type = "ORB"
        self.sm.orb_high = 101.0
        self.sm.orb_low = 99.5
        self.sm.last_gap_pct = 0.1
        self.sm.last_rel_volume = 1.1
        self.sm.last_atr_pct = 0.8
        self.sm.last_market_trend = "up"
        self.sm.last_rsi_value = 55.0
        self.sm.last_short_sma = 100.5
        self.sm.last_long_sma = 100.0

        # Simulate bar at market close (4:00 PM ET)
        market_close_bar = self.create_bar(16, 0, 100.5, 100.5, 100.0, 100.25) 
        await self.sm.process_minute_bar(market_close_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.logger_obj.update_exit_details.assert_called_once_with(
            "test_order_market_close", 100.25, round((100.25 - 100.0) * 10, 2), market_close_bar.timestamp
        )
        self.mock_adaptive_learner.log_trade_features.assert_called_once_with(
            trade_id=5, ticker="SPY", regime="general_uptrend", candidate_id="orb_base_2_1", strategy_type="ORB",
            orb_width_pct=Mock(return_value=0.0), gap_pct=0.1, rel_volume=1.1, atr_pct=0.8, market_trend="up",
            rsi_value=55.0, short_sma=100.5, long_sma=100.0
        ) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertIsNone(self.sm.active_trade_id)
        self.assertEqual(self.sm.current_regime, "unknown")
        self.assertEqual(self.sm.current_candidate_id, "default")
        self.assertEqual(self.sm.current_strategy_type, "ORB")

    async def test_daily_reset_triggers(self):
        self.sm.orb_high = 510.0
        self.sm.orb_low = 490.0
        self.sm.range_established = True
        self.sm.position_taken = True
        self.sm.active_trade_id = 3 
        self.sm.active_trade_order_id = "test_order_3"
        self.sm.current_regime = "high_vol_trend"
        self.sm.current_candidate_id = "tight_fast"
        self.sm.current_strategy_type = "SMA_CROSS"
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)

        # Process a bar on a new day
        bar = self.create_bar(9, 31, 500.0, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)

        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 500.0) 
        self.assertEqual(self.sm.orb_low, 498.0) 
        self.assertEqual(self.sm.last_reset_date, datetime.now().date())
        self.assertIsNone(self.sm.active_trade_id) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertEqual(self.sm.current_regime, "unknown") 
        self.assertEqual(self.sm.current_candidate_id, "default") 
        self.assertEqual(self.sm.current_strategy_type, "ORB")

    async def test_premarket_no_action_before_market_open(self):
        # Set current time to pre-market (e.g., 9:00 AM ET)
        bar = self.create_bar(9, 0, 500.0, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)
        self.assertEqual(self.sm.orb_high, 0.0) 
        self.mock_adaptive_learner.classify_regime.assert_not_called()
        self.mock_adaptive_learner.select_candidate.assert_not_called()

    async def test_afterhours_no_new_trade_after_market_close(self):
        # Simulate market close and no active position
        self.sm.position_taken = False
        self.sm.range_established = True
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Simulate after-hours bar (e.g., 16:01 ET, after 4 PM market close)
        after_hours_bar = self.create_bar(16, 1, 510.0, 510.0, 505.0, 508.0)
        await self.sm.process_minute_bar(after_hours_bar)
        self.mock_trader.execute_trade_setup.assert_not_called() 
        self.mock_adaptive_learner.classify_regime.assert_not_called()
        self.mock_adaptive_learner.select_candidate.assert_not_called()

    async def test_check_daily_reset_triggers(self):
        self.sm.position_taken = True
        self.sm.range_established = True
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)
        self.sm.check_daily_reset()
        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)

    async def test_check_daily_reset_noop(self):
        self.sm.position_taken = True
        self.sm.check_daily_reset()
        self.assertTrue(self.sm.position_taken)

# =============================================================================
# Test: Multi-Day Reset (via process_minute_bar)
# =============================================================================
class TestMultiDayReset(unittest.TestCase):
    """Test state reset via process_minute_bar."""

    def setUp(self):
        self.mock_trader = MagicMock(spec=AlpacaPaperTrader)
        self.mock_trader.logger_obj = MagicMock(spec=TradeLogger)
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) 
        self.base_config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_pct": 0.002, 
            "position_size_multiplier": 1.0,
            "take_profit_rr": 2.0,
            "take_profit_pct": 0.005,
        }
        self.sm = TradingStateMachine("SPY", self.mock_trader, self.base_config, self.mock_adaptive_learner) 

    def create_bar(self, day, hour, minute, open, high, low, close, volume=10000, symbol="SPY"):
        dt = ET.localize(datetime(2026, 6, day, hour, minute, 0))
        mock_bar = MagicMock()
        mock_bar.symbol = symbol
        mock_bar.timestamp = dt.astimezone(UTC)
        mock_bar.open = open
        mock_bar.high = high
        mock_bar.low = low
        mock_bar.close = close
        mock_bar.volume = volume
        return mock_bar

    async def test_force_reset_via_check_daily(self):
        """Simulate multi-day by manipulating last_reset_date."""
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)
        self.sm.position_taken = True
        self.sm.range_established = True
        self.sm.orb_high, self.sm.orb_low = 505.0, 503.0
        self.sm.active_trade_id = 10 
        self.sm.active_trade_order_id = "test_order_reset"
        self.sm.current_regime = "trend"
        self.sm.current_candidate_id = "base"
        self.sm.current_strategy_type = "ORB"

        # Process a bar on the new day, which should trigger a reset
        bar = self.create_bar(16, 9, 31, 500.0, 500.0, 498.0, 499.0) 
        await self.sm.process_minute_bar(bar)

        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 500.0)
        self.assertEqual(self.sm.orb_low, 498.0)
        self.assertEqual(self.sm.last_reset_date, datetime(2026, 6, 16).date())
        self.assertIsNone(self.sm.active_trade_id) 
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertEqual(self.sm.current_regime, "unknown") 
        self.assertEqual(self.sm.current_candidate_id, "default") 
        self.assertEqual(self.sm.current_strategy_type, "ORB")

# =============================================================================
# Test: AdaptiveLearner (NEW)
# =============================================================================
class TestAdaptiveLearner(unittest.TestCase):
    """Test the AdaptiveLearner module."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.learner = AdaptiveLearner(db_path=self.db_path, lookback_trades=5, min_trades_before_promote=2, exploration_rate=0.0)
        # Ensure trades table exists for _candidate_stats
        conn = sqlite3.connect(self.db_path)
        conn.execute(CREATE_TRADES_TABLE)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.learner.close()
        import gc; gc.collect()
        try:
            os.close(self.db_fd)
        except OSError:
            pass
        try:
            os.unlink(self.db_path)
        except PermissionError:
            pass

    def _insert_trade(self, ticker, pnl, regime, candidate_id, strategy_type="ORB", status="CLOSED", trade_id=None):
        if trade_id is None:
            cursor = self.learner.conn.execute("SELECT MAX(id) FROM trades")
            trade_id = (cursor.fetchone()[0] or 0) + 1
        self.learner.conn.execute("""
            INSERT INTO trades (id, timestamp_entry, timestamp_exit, ticker, setup_type, entry_price, exit_price, quantity, stop_loss, take_profit, status, pnl, regime, candidate_id, strategy_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              ticker, strategy_type, 100.0, 100.0 + pnl, 1, 99.0, 101.0, status, pnl, regime, candidate_id, strategy_type))
        self.learner.conn.commit()
        return trade_id

    def test_seed_candidates(self):
        candidates = self.learner.get_enabled_candidates()
        self.assertEqual(len(candidates), len(DEFAULT_CANDIDATES))
        self.assertEqual(candidates[0].candidate_id, "orb_base_2_1")
        self.assertEqual(candidates[0].strategy_type, "ORB")

    def test_classify_regime(self):
        # atr_pct, gap_pct, market_above_ma, rel_volume, rsi_value, short_sma, long_sma, market_trend
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 1.6, 3.0, 1.0, 50.0, 500.0, 499.0, "up"), "high_vol_gap")
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 0.5, 0.5, 1.5, 60.0, 501.0, 500.0, "up"), "strong_uptrend_sma_cross")
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 0.5, 0.5, 1.1, 60.0, 501.0, 500.0, "up"), "uptrend_orb_momentum")
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 0.5, 0.5, 0.7, 25.0, 499.0, 500.0, "sideways"), "oversold_bounce")
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 0.5, 0.5, 0.7, 50.0, 500.0, 500.0, "sideways"), "quiet_range")
        self.assertEqual(self.learner.classify_regime("SPY", 500.0, 0.5, 0.5, 1.5, 1.0, 50.0, 500.0, 500.0, "sideways"), "general_range")

    def test_log_trade_features(self):
        trade_id = self._insert_trade("SPY", 1.0, "trend", "orb_base_2_1", "ORB")
        self.learner.log_trade_features(trade_id, "SPY", "trend", "orb_base_2_1", "ORB", 0.5, 0.1, 1.2, 1.0, "up", 60.0, 501.0, 500.0)
        cursor = self.learner.conn.execute("SELECT * FROM trade_features WHERE trade_id=?", (trade_id,))
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['ticker'], "SPY")
        self.assertEqual(row['regime'], "trend")
        self.assertEqual(row['candidate_id'], "orb_base_2_1")
        self.assertEqual(row['strategy_type'], "ORB")
        self.assertEqual(row['orb_width_pct'], 0.5)
        self.assertEqual(row['rsi_value'], 60.0)
        self.assertEqual(row['short_sma'], 501.0)
        self.assertEqual(row['long_sma'], 500.0)

    def test_candidate_stats_empty(self):
        stats = self.learner._candidate_stats("SPY", "trend", "orb_base_2_1")
        self.assertEqual(stats['n'], 0)
        self.assertEqual(stats['score'], -999.0)

    def test_candidate_stats_winning(self):
        self._insert_trade("SPY", 1.0, "trend", "orb_base_2_1")
        self._insert_trade("SPY", 2.0, "trend", "orb_base_2_1")
        stats = self.learner._candidate_stats("SPY", "trend", "orb_base_2_1")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], 1.5)
        self.assertAlmostEqual(stats['win_rate'], 1.0)
        self.assertAlmostEqual(stats['profit_factor'], 999.0) 

    def test_candidate_stats_losing(self):
        self._insert_trade("SPY", -1.0, "trend", "orb_base_2_1")
        self._insert_trade("SPY", -0.5, "trend", "orb_base_2_1")
        stats = self.learner._candidate_stats("SPY", "trend", "orb_base_2_1")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], -0.75)
        self.assertAlmostEqual(stats['win_rate'], 0.0)
        self.assertAlmostEqual(stats['profit_factor'], 0.0) 

    def test_candidate_stats_mixed(self):
        self._insert_trade("SPY", 2.0, "trend", "orb_base_2_1")
        self._insert_trade("SPY", -1.0, "trend", "orb_base_2_1")
        stats = self.learner._candidate_stats("SPY", "trend", "orb_base_2_1")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], 0.5)
        self.assertAlmostEqual(stats['win_rate'], 0.5)
        self.assertAlmostEqual(stats['profit_factor'], 2.0)

    def test_select_candidate_no_exploration(self):
        self._insert_trade("SPY", 2.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", -1.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", 3.0, "uptrend_orb_momentum", "orb_tight_3_1")
        self._insert_trade("SPY", 0.5, "uptrend_orb_momentum", "orb_tight_3_1")

        chosen_config, candidate_id, strategy_type, _ = self.learner.select_candidate("SPY", "uptrend_orb_momentum", self.base_config)
        self.assertEqual(candidate_id, "orb_tight_3_1")
        self.assertEqual(strategy_type, "ORB")
        self.assertEqual(chosen_config["stop_loss_pct"], 0.0015) 

    @patch('random.random', return_value=0.05) 
    def test_select_candidate_with_exploration(self, mock_random):
        self.learner.exploration_rate = 0.1
        self._insert_trade("SPY", 2.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", -1.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", 3.0, "uptrend_orb_momentum", "orb_tight_3_1")
        self._insert_trade("SPY", 0.5, "uptrend_orb_momentum", "orb_tight_3_1")

        chosen_config, candidate_id, strategy_type, _ = self.learner.select_candidate("SPY", "uptrend_orb_momentum", self.base_config)
        self.assertIn(candidate_id, [c.candidate_id for c in DEFAULT_CANDIDATES])
        self.assertIn(strategy_type, [c.strategy_type for c in DEFAULT_CANDIDATES])

    def test_select_candidate_low_sample_penalty(self):
        self.learner.exploration_rate = 0.0 
        self.learner.min_trades_before_promote = 5

        self._insert_trade("SPY", 2.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", -1.0, "uptrend_orb_momentum", "orb_base_2_1")
        self._insert_trade("SPY", 3.0, "uptrend_orb_momentum", "orb_tight_3_1")

        chosen_config, candidate_id, strategy_type, _ = self.learner.select_candidate("SPY", "uptrend_orb_momentum", self.base_config)
        self.assertEqual(candidate_id, "orb_base_2_1") 
        self.assertEqual(strategy_type, "ORB")

# =============================================================================
# Test: Integration Scenarios
# =============================================================================
class TestIntegrationScenarios(unittest.TestCase):
    """Higher-level integration-style tests."""

    def setUp(self):
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) 
        self.mock_adaptive_learner.classify_regime.return_value = "uptrend_orb_momentum"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {"stop_loss_pct": 0.002, "take_profit_rr": 2.0, "position_size_multiplier": 1.0, "strategy_type": "ORB"},
            "orb_base_2_1",
            "ORB",
            {}
        )

    def test_engine_init(self):
        with patch('upgainpulse.TradingClient'), \
             patch('upgainpulse.AccountValidator'), \
             patch('upgainpulse.TradeLogger'), \
             patch('upgainpulse.RobustWebSocketManager'):
            engine = UpGainPulseEngine(
                tickers=["SPY", "QQQ"],
                config={"account_capital": 500.0, "risk_per_trade_usd": 50.0,
                        "stop_loss_pct": 0.002, "position_size_multiplier": 1.0,
                        "risk_reward_ratio": 2.0}
            )
            self.assertEqual(len(engine.state_machines), 2)
            self.assertIn("SPY", engine.state_machines)
            self.assertIn("QQQ", engine.state_machines)
            self.assertIsNotNone(engine.adaptive_learner) 

    @patch('upgainpulse.TradingClient')
    @patch('upgainpulse.AccountValidator')
    @patch('upgainpulse.TradeLogger')
    @patch('upgainpulse.RobustWebSocketManager')
    async def test_full_day_scenario(self, MockWSManager, MockTradeLogger, MockAccountValidator, MockTradingClient):
        # Mock dependencies
        mock_stream = MagicMock()
        MockWSManager.return_value.connect_with_retry.return_value = mock_stream
        MockWSManager.return_value.run_with_reconnect.side_effect = asyncio.CancelledError 
        
        mock_trader_instance = MagicMock(spec=AlpacaPaperTrader)
        mock_trader_instance.logger_obj = MockTradeLogger.return_value 
        MockTradingClient.return_value = MagicMock() 
        MockAccountValidator.return_value.get_buying_power.return_value = 5000.0 

        # Mock execute_trade_setup to return order details
        mock_trader_instance.execute_trade_setup.return_value = {
            "order_id": "test_order_SPY", "entry_price": 196.30, "quantity": 1,
            "stop_loss_price": 196.20, "take_profit_price": 196.50,
            "strategy_type": "ORB"
        }

        # Patch AlpacaPaperTrader and AdaptiveLearner to return our mock instances
        with patch('upgainpulse.AlpacaPaperTrader', return_value=mock_trader_instance), \
             patch('upgainpulse.AdaptiveLearner', return_value=self.mock_adaptive_learner): 
            sm = TradingStateMachine("AAPL", mock_trader_instance, {
                "risk_per_trade_usd": 50.0, "stop_loss_pct": 0.002,
                "position_size_multiplier": 1.0, "take_profit_rr": 2.0
            }, self.mock_adaptive_learner) 

            def create_bar(h, m, o, hi, lo, cl, vol=10000, day=15):
                d = ET.localize(datetime(2026, 6, day, h, m, 0))
                b = MagicMock()
                b.symbol, b.timestamp, b.open, b.high, b.low, b.close, b.volume = "AAPL", d.astimezone(UTC), o, hi, lo, cl, vol
                return b

            # Simulate ORB range building
            await sm.process_minute_bar(create_bar(9, 31, 195.0, 195.5, 194.8, 195.2))
            await sm.process_minute_bar(create_bar(9, 38, 195.2, 196.0, 195.3, 195.8))
            await sm.process_minute_bar(create_bar(9, 44, 195.8, 195.5, 194.5, 194.5))

            # Lock range at 9:45
            await sm.process_minute_bar(create_bar(9, 45, 194.5, 195.0, 194.0, 194.5))
            self.assertTrue(sm.range_established)
            self.assertFalse(sm.position_taken)

            # Breakout and order execution
            breakout_bar = create_bar(9, 46, 196.0, 196.5, 195.0, 196.3)
            self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 1}
            await sm.process_minute_bar(breakout_bar)
            self.assertTrue(sm.position_taken)
            self.mock_adaptive_learner.classify_regime.assert_called_once() 
            self.mock_adaptive_learner.select_candidate.assert_called_once() 
            mock_trader_instance.execute_trade_setup.assert_called_once_with("AAPL", 196.3, {
                "account_capital": 500.0, "risk_per_trade_usd": 50.0, "stop_loss_pct": 0.002,
                "position_size_multiplier": 1.0, "take_profit_rr": 2.0, "take_profit_pct": 0.005,
                "strategy_type": "ORB", "current_regime": "uptrend_orb_momentum", "candidate_id": "orb_base_2_1"
            })

            # Simulate TP hit
            tp_hit_bar = create_bar(9, 47, 196.3, 196.60, 196.40, 196.50) 
            await sm.process_minute_bar(tp_hit_bar)
            self.assertFalse(sm.position_taken)
            MockTradeLogger.return_value.update_exit_details.assert_called_once_with(
                "test_order_SPY", 196.50, round((196.50 - 196.30) * 1, 2), tp_hit_bar.timestamp
            )
            self.mock_adaptive_learner.log_trade_features.assert_called_once() 

            # Simulate a new day reset and another trade
            sm.last_reset_date = datetime.now().date() - timedelta(days=1)
            sm.position_taken = False 
            sm.range_established = False
            sm.orb_high = 0.0
            sm.orb_low = float('inf')
            self.mock_adaptive_learner.classify_regime.reset_mock()
            self.mock_adaptive_learner.select_candidate.reset_mock()

            await sm.process_minute_bar(create_bar(datetime.now().day, 9, 31, 200.0, 200.0, 199.0, 199.5))
            await sm.process_minute_bar(create_bar(datetime.now().day, 9, 45, 199.5, 201.0, 198.5, 200.0))

            self.mock_adaptive_learner.classify_regime.return_value = "oversold_bounce"
            self.mock_adaptive_learner.select_candidate.return_value = (
                {**self.base_config, "strategy_type": "RSI_MR", "rsi_period": 14, "oversold_level": 30, "take_profit_pct": 0.005},
                "rsi_14_oversold",
                "RSI_MR",
                {}
            )
            mock_trader_instance.execute_trade_setup.reset_mock()
            mock_trader_instance.execute_trade_setup.return_value = {
                "order_id": "test_order_AAPL_day2", "entry_price": 201.50, "quantity": 2,
                "stop_loss_price": 201.30, "take_profit_price": 201.90,
                "strategy_type": "RSI_MR"
            }
            # Simulate RSI oversold condition
            sm.last_rsi_value = 25 # Force oversold RSI
            breakout_bar_day2 = create_bar(datetime.now().day, 9, 46, 201.0, 202.0, 201.0, 201.50)
            self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 2}
            await sm.process_minute_bar(breakout_bar_day2)
            self.assertTrue(sm.position_taken)
            self.mock_adaptive_learner.classify_regime.assert_called_once()
            self.mock_adaptive_learner.select_candidate.assert_called_once()
            mock_trader_instance.execute_trade_setup.assert_called_once_with("AAPL", 201.50, {
                "account_capital": 500.0, "risk_per_trade_usd": 50.0, "stop_loss_pct": 0.002,
                "position_size_multiplier": 1.0, "take_profit_rr": 2.0, "take_profit_pct": 0.005,
                "strategy_type": "RSI_MR", "current_regime": "oversold_bounce", "candidate_id": "rsi_14_oversold"
            })

    @patch('upgainpulse.TradingClient')
    @patch('upgainpulse.AccountValidator')
    @patch('upgainpulse.TradeLogger')
    @patch('upgainpulse.RobustWebSocketManager')
    async def test_check_daily_reset_triggers_on_new_day(self, MockWSManager, MockTradeLogger, MockAccountValidator, MockTradingClient):
        mock_stream = MagicMock()
        MockWSManager.return_value.connect_with_retry.return_value = mock_stream
        MockWSManager.return_value.run_with_reconnect.side_effect = asyncio.CancelledError

        mock_trader_instance = MagicMock(spec=AlpacaPaperTrader)
        mock_trader_instance.logger_obj = MockTradeLogger.return_value
        MockTradingClient.return_value = MagicMock()
        MockAccountValidator.return_value.get_buying_power.return_value = 5000.0

        with patch('upgainpulse.AlpacaPaperTrader', return_value=mock_trader_instance), \
             patch('upgainpulse.AdaptiveLearner', return_value=self.mock_adaptive_learner): 
            sm = TradingStateMachine("SPY", mock_trader_instance, self.base_config, self.mock_adaptive_learner) 
            sm.position_taken = True
            sm.range_established = True
            sm.orb_high = 510.0
            sm.orb_low = 490.0
            sm.last_reset_date = datetime.now().date() - timedelta(days=1) 
            sm.active_trade_id = 5 
            sm.active_trade_order_id = "test_order_reset_day"
            sm.current_regime = "high_vol_range"
            sm.current_candidate_id = "defensive"
            sm.current_strategy_type = "RSI_MR"

            # Process a bar on the "new" day
            bar_new_day = self.create_bar(9, 31, 500.0, 500.0, 498.0, 499.0)
            await sm.process_minute_bar(bar_new_day)

            self.assertFalse(sm.position_taken)
            self.assertFalse(sm.range_established)
            self.assertEqual(sm.orb_high, 500.0) 
            self.assertEqual(sm.orb_low, 498.0) 
            self.assertEqual(sm.last_reset_date, datetime.now().date())
            self.assertIsNone(sm.active_trade_id) 
            self.assertIsNone(sm.active_trade_order_id)
            self.assertEqual(sm.current_regime, "unknown") 
            self.assertEqual(sm.sm.current_candidate_id, "default") 
            self.assertEqual(sm.current_strategy_type, "ORB")


if __name__ == "__main__":
    unittest.main(argv=['first-arg-is-ignored'], exit=False, verbosity=2)
