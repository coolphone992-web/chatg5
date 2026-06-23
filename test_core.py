import unittest
import time
from unittest.mock import Mock, patch, MagicMock, PropertyMock
import threading
import sqlite3
import os
import tempfile
import asyncio
from datetime import datetime, time, timedelta
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
        validate_config({"risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                         "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_risk_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 0, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_risk_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": -10, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_sl_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 0,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_multiplier_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 0, "risk_reward_ratio": 2.0, "account_capital": 500.0})

    def test_rr_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": -1.0, "account_capital": 500.0})

    def test_rr_unrealistic(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 100.0, "account_capital": 500.0})

    def test_missing_keys_use_defaults(self):
        # Should not raise an error, as defaults are used
        validate_config({"risk_per_trade_usd": 50.0})

    def test_config_type_error(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": "abc", "stop_loss_cents": 10,
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
        self.assertIsNone(row['regime']) # NEW: Check for new columns
        self.assertIsNone(row['candidate_id']) # NEW: Check for new columns

    def test_log_entry_with_adaptive_data(self):
        self.logger.log_entry("SPY", "ORB_LONG", 500.0, 1, 499.90, 500.20, "order_124", 
                              regime="trend", candidate_id="base")
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE order_id='order_124'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['regime'], "trend")
        self.assertEqual(row['candidate_id'], "base")

    def test_log_entry_without_order_id(self):
        self.logger.log_entry("AAPL", "ORB_LONG", 180.0, 2, 179.90, 182.00, None)
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE ticker='AAPL'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row['order_id']) # order_id at index 11

    def test_update_exit_details(self):
        # First log an entry
        self.logger.log_entry("SPY", "ORB_LONG", 500.0, 1, 499.90, 500.20, "order_123")
        
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
                    self.logger.log_entry(ticker, "ORB_LONG", 100.0, 1, 99.90, 102.00)
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
            "stop_loss_cents": 10,
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0,
        }

    def test_spy_at_500(self):
        self.assertEqual(calculate_position_size(500.0, self.config, 500.0), 1)

    def test_spy_at_250(self):
        self.assertEqual(calculate_position_size(250.0, self.config, 500.0), 2)

    def test_qqq_at_400(self):
        self.assertEqual(calculate_position_size(400.0, self.config, 500.0), 1)

    def test_max_shares(self):
        self.assertEqual(calculate_position_size(10.0, self.config, 500.0), 50)

    def test_small_risk_large_stop(self):
        cfg = dict(self.config)
        cfg["risk_per_trade_usd"] = 0.25
        cfg["stop_loss_cents"] = 100
        self.assertEqual(calculate_position_size(500.0, cfg, 500.0), 0)

    def test_double_multiplier(self):
        cfg = dict(self.config)
        cfg["position_size_multiplier"] = 2.0
        self.assertEqual(calculate_position_size(250.0, cfg, 500.0), 2)

    def test_large_account_uses_risk_limit(self):
        cfg = dict(self.config)
        cfg["account_capital"] = 100000.0
        self.assertEqual(calculate_position_size(500.0, cfg, 100000.0), 500)

    def test_zero_current_price(self):
        self.assertEqual(calculate_position_size(0.0, self.config, 500.0), 0)

    def test_zero_stop_loss_cents(self):
        cfg = dict(self.config)
        cfg["stop_loss_cents"] = 0
        self.assertEqual(calculate_position_size(500.0, cfg, 500.0), 0)

    def test_insufficient_buying_power(self):
        self.assertEqual(calculate_position_size(500.0, self.config, 10.0), 0)

# =============================================================================
# Test: TP/SL Calculation
# =============================================================================
class TestTPSLCalculation(unittest.TestCase):
    """Test take-profit and stop-loss price calculations."""

    def test_basic(self):
        # SL: 500 - 0.10 = 499.90
        # TP: 500 + (0.10 * 2.0) = 500.20
        self.assertEqual(round(500.0 - 0.10, 2), 499.90)
        self.assertEqual(round(500.0 + (0.10 * 2.0), 2), 500.20)

    def test_larger_sl(self):
        # SL: 500 - 0.50 = 499.50
        # TP: 500 + (0.50 * 2.0) = 501.00
        self.assertEqual(round(500.0 - 0.50, 2), 499.50)
        self.assertEqual(round(500.0 + (0.50 * 2.0), 2), 501.00)

    def test_high_rr(self):
        # SL: 500 - 0.10 = 499.90
        # TP: 500 + (0.10 * 3.0) = 500.30
        self.assertEqual(round(500.0 - 0.10, 2), 499.90)
        self.assertEqual(round(500.0 + (0.10 * 3.0), 2), 500.30)

    def test_low_price(self):
        # SL: 100 - 0.10 = 99.90
        # TP: 100 + (0.10 * 2.0) = 100.20
        self.assertEqual(round(100.0 - 0.10, 2), 99.90)
        self.assertEqual(round(100.0 + (0.10 * 2.0), 2), 100.20)

# =============================================================================
# Test: AlpacaPaperTrader execute_orb_setup
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
            "stop_loss_cents": 10,
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0
        }

    def test_execute_order_success(self):
        self.mock_client.submit_order.return_value = MagicMock(id="order_abc123")
        order_details = self.trader.execute_orb_setup("SPY", 500.0, self.config)
        self.assertIsNotNone(order_details)
        self.assertTrue(self.mock_client.submit_order.called_once())
        self.mock_logger.log_entry.assert_called_once_with(
            "SPY", "ORB_LONG", 500.0, 1, 499.90, 500.20, "order_abc123",
            regime=None, candidate_id=None # NEW: Check for new adaptive args
        )
        self.assertEqual(order_details["order_id"], "order_abc123")
        self.assertEqual(order_details["entry_price"], 500.0)
        self.assertEqual(order_details["quantity"], 1)
        self.assertEqual(order_details["stop_loss_price"], 499.90)
        self.assertEqual(order_details["take_profit_price"], 500.20)

    def test_execute_order_with_adaptive_data(self):
        adaptive_config = {
            **self.config,
            "current_regime": "trend",
            "candidate_id": "base"
        }
        self.mock_client.submit_order.return_value = MagicMock(id="order_abc124")
        order_details = self.trader.execute_orb_setup("SPY", 500.0, adaptive_config)
        self.assertIsNotNone(order_details)
        self.mock_logger.log_entry.assert_called_once_with(
            "SPY", "ORB_LONG", 500.0, 1, 499.90, 500.20, "order_abc124",
            regime="trend", candidate_id="base"
        )

    def test_execute_order_insufficient_bp(self):
        self.mock_validator.check_buying_power.return_value = False
        order_details = self.trader.execute_orb_setup("SPY", 500.0, self.config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_not_called()

    def test_execute_order_cooldown(self):
        self.trader.execute_orb_setup("SPY", 500.0, self.config) # First order
        order_details = self.trader.execute_orb_setup("SPY", 500.0, self.config) # Second order immediately
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_called_once() # Only first call should go through

    def test_execute_order_qty_zero(self):
        config = {"account_capital": 500.0, "risk_per_trade_usd": 0.25, "stop_loss_cents": 100,
                  "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
        order_details = self.trader.execute_orb_setup("SPY", 500.0, config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_not_called()

    def test_execute_order_api_error(self):
        self.mock_client.submit_order.side_effect = MockAPIError("API error")
        order_details = self.trader.execute_orb_setup("SPY", 500.0, self.config)
        self.assertIsNone(order_details)
        self.mock_client.submit_order.assert_called_once()

# =============================================================================
# Test: ORB State Machine
# =============================================================================
class TestORBStateMachine(unittest.TestCase):
    """Test the Opening Range Breakout state machine."""

    def setUp(self):
        self.mock_trader = MagicMock(spec=AlpacaPaperTrader)
        self.mock_trader.logger_obj = MagicMock(spec=TradeLogger) # Mock the logger within the trader
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) # NEW: Mock AdaptiveLearner
        self.config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_cents": 10,
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0
        }
        self.sm = ORBStateMachine("SPY", self.mock_trader, self.config, self.mock_adaptive_learner) # NEW: Pass adaptive_learner

    def create_bar(self, hour, minute, high, low, close, symbol="SPY"):
        dt = ET.localize(datetime(2026, 6, 15, hour, minute, 0))
        mock_bar = MagicMock()
        mock_bar.symbol = symbol
        mock_bar.timestamp = dt.astimezone(UTC)
        mock_bar.high = high
        mock_bar.low = low
        mock_bar.close = close
        return mock_bar

    def test_initial_state(self):
        self.assertEqual(self.sm.orb_high, 0.0)
        self.assertEqual(self.sm.orb_low, float('inf'))
        self.assertFalse(self.sm.range_established)
        self.assertFalse(self.sm.position_taken)
        self.assertEqual(self.sm.current_regime, "unknown") # NEW: Check initial regime
        self.assertEqual(self.sm.current_candidate_id, "default") # NEW: Check initial candidate ID

    async def test_build_range_during_orb_period(self):
        bar1 = self.create_bar(9, 35, 501.0, 499.0, 500.0)
        await self.sm.process_minute_bar(bar1)
        self.assertEqual(self.sm.orb_high, 501.0)
        self.assertEqual(self.sm.orb_low, 499.0)
        self.assertFalse(self.sm.range_established)

    async def test_lock_range_at_945(self):
        bar1 = self.create_bar(9, 35, 501.0, 499.0, 500.0)
        await self.sm.process_minute_bar(bar1)
        bar_lock = self.create_bar(9, 45, 502.0, 500.0, 501.0)
        await self.sm.process_minute_bar(bar_lock)
        self.assertTrue(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 502.0)
        self.assertEqual(self.sm.orb_low, 499.0) # Low from first bar

    async def test_breakout_executes_order(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 501.0, 499.0, 500.0))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0 # Ensure a clear high for breakout
        self.sm.orb_low = 499.0 # Ensure a clear low

        # Mock adaptive learner to return a specific config
        self.mock_adaptive_learner.classify_regime.return_value = "trend"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {"stop_loss_cents": 12, "risk_reward_ratio": 2.2, "position_size_multiplier": 1.0},
            "adaptive_config_1",
            {}
        )

        # Simulate successful order submission
        self.mock_trader.execute_orb_setup.return_value = {
            "order_id": "test_order_1",
            "entry_price": 502.50,
            "quantity": 1,
            "stop_loss_price": 502.40,
            "take_profit_price": 502.70,
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 1} # Mock internal trade ID

        # Breakout bar
        breakout_bar = self.create_bar(9, 46, 503.0, 501.0, 502.50)
        await self.sm.process_minute_bar(breakout_bar)

        self.assertTrue(self.sm.position_taken)
        self.mock_adaptive_learner.classify_regime.assert_called_once() # NEW: Verify regime classification
        self.mock_adaptive_learner.select_candidate.assert_called_once() # NEW: Verify candidate selection
        self.mock_trader.execute_orb_setup.assert_called_once_with("SPY", 502.50, {
            "account_capital": 500.0, "risk_per_trade_usd": 50.0, "stop_loss_cents": 12, # NEW: Adaptive SL
            "position_size_multiplier": 1.0, "risk_reward_ratio": 2.2 # NEW: Adaptive RR
        })
        self.assertEqual(self.sm.active_trade_order_id, "test_order_1")
        self.assertEqual(self.sm.active_trade_id, 1) # NEW: Check internal trade ID
        self.assertEqual(self.sm.current_regime, "trend") # NEW: Check current regime
        self.assertEqual(self.sm.current_candidate_id, "adaptive_config_1") # NEW: Check current candidate ID

    async def test_no_breakout_below_range(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 501.0, 499.0, 500.0))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Bar below high
        no_breakout_bar = self.create_bar(9, 46, 500.5, 498.0, 499.50)
        await self.sm.process_minute_bar(no_breakout_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.execute_orb_setup.assert_not_called()
        self.mock_adaptive_learner.classify_regime.assert_not_called() # NEW: No adaptive call if no breakout

    async def test_no_double_order(self):
        # Build range
        await self.sm.process_minute_bar(self.create_bar(9, 35, 501.0, 499.0, 500.0))
        # Lock range
        await self.sm.process_minute_bar(self.create_bar(9, 45, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Mock adaptive learner
        self.mock_adaptive_learner.classify_regime.return_value = "trend"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {"stop_loss_cents": 10, "risk_reward_ratio": 2.0, "position_size_multiplier": 1.0},
            "base",
            {}
        )

        # First breakout
        self.mock_trader.execute_orb_setup.return_value = {
            "order_id": "test_order_1", "entry_price": 502.0, "quantity": 1,
            "stop_loss_price": 501.90, "take_profit_price": 502.10
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 1}
        await self.sm.process_minute_bar(self.create_bar(9, 46, 503.0, 501.0, 502.0))
        self.assertTrue(self.sm.position_taken)

        # Second breakout attempt
        self.mock_trader.execute_orb_setup.reset_mock() # Clear previous call
        self.mock_adaptive_learner.classify_regime.reset_mock()
        self.mock_adaptive_learner.select_candidate.reset_mock()
        await self.sm.process_minute_bar(self.create_bar(9, 47, 504.0, 502.0, 503.0))
        self.assertFalse(self.mock_trader.execute_orb_setup.called)
        self.mock_adaptive_learner.classify_regime.assert_not_called() # NEW: No adaptive call if position taken
        self.mock_adaptive_learner.select_candidate.assert_not_called() # NEW: No adaptive call if position taken

    async def test_sl_hit_closes_position(self):
        # Simulate an active position
        self.sm.position_taken = True
        self.sm.active_trade_id = 1 # NEW: Set internal trade ID
        self.sm.active_trade_order_id = "test_order_1"
        self.sm.active_trade_entry_price = 502.50
        self.sm.active_trade_sl_price = 502.40
        self.sm.active_trade_tp_price = 502.70
        self.sm.active_trade_qty = 1
        self.sm.current_regime = "trend"
        self.sm.current_candidate_id = "base"

        # Bar hits SL
        sl_hit_bar = self.create_bar(10, 0, 502.45, 502.30, 502.35) # Low hits SL
        await self.sm.process_minute_bar(sl_hit_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.logger_obj.update_exit_details.assert_called_once_with(
            "test_order_1", 502.40, round((502.40 - 502.50) * 1, 2), sl_hit_bar.timestamp
        )
        self.mock_adaptive_learner.log_trade_features.assert_called_once_with(
            trade_id=1, ticker="SPY", regime="trend", candidate_id="base",
            orb_width_pct=Mock(return_value=0.0), gap_pct=0.0, rel_volume=1.0, atr_pct=0.0, market_trend="unknown"
        ) # NEW: Verify trade features logged
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertIsNone(self.sm.active_trade_id)
        self.assertEqual(self.sm.current_regime, "unknown")
        self.assertEqual(self.sm.current_candidate_id, "default")

    async def test_tp_hit_closes_position(self):
        # Simulate an active position
        self.sm.position_taken = True
        self.sm.active_trade_id = 2 # NEW: Set internal trade ID
        self.sm.active_trade_order_id = "test_order_2"
        self.sm.active_trade_entry_price = 502.50
        self.sm.active_trade_sl_price = 502.40
        self.sm.active_trade_tp_price = 502.70
        self.sm.active_trade_qty = 1
        self.sm.current_regime = "range"
        self.sm.current_candidate_id = "wide_rr"

        # Bar hits TP
        tp_hit_bar = self.create_bar(10, 5, 502.80, 502.65, 502.75) # High hits TP
        await self.sm.process_minute_bar(tp_hit_bar)

        self.assertFalse(self.sm.position_taken)
        self.mock_trader.logger_obj.update_exit_details.assert_called_once_with(
            "test_order_2", 502.70, round((502.70 - 502.50) * 1, 2), tp_hit_bar.timestamp
        )
        self.mock_adaptive_learner.log_trade_features.assert_called_once_with(
            trade_id=2, ticker="SPY", regime="range", candidate_id="wide_rr",
            orb_width_pct=Mock(return_value=0.0), gap_pct=0.0, rel_volume=1.0, atr_pct=0.0, market_trend="unknown"
        ) # NEW: Verify trade features logged
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertIsNone(self.sm.active_trade_id)
        self.assertEqual(self.sm.current_regime, "unknown")
        self.assertEqual(self.sm.current_candidate_id, "default")

    async def test_daily_reset_triggers(self):
        self.sm.orb_high = 510.0
        self.sm.orb_low = 490.0
        self.sm.range_established = True
        self.sm.position_taken = True
        self.sm.active_trade_id = 3 # NEW: Set internal trade ID
        self.sm.active_trade_order_id = "test_order_3"
        self.sm.current_regime = "high_vol_trend"
        self.sm.current_candidate_id = "tight_fast"
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)

        # Process a bar on a new day
        bar = self.create_bar(9, 31, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)

        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 500.0) # Reset and updated by new bar
        self.assertEqual(self.sm.orb_low, 498.0) # Reset and updated by new bar
        self.assertEqual(self.sm.last_reset_date, datetime.now().date())
        self.assertIsNone(self.sm.active_trade_id) # NEW: Check reset of internal trade ID
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertEqual(self.sm.current_regime, "unknown") # NEW: Check reset of regime
        self.assertEqual(self.sm.current_candidate_id, "default") # NEW: Check reset of candidate ID

    async def test_premarket_no_action(self):
        # Set current time to pre-market (e.g., 9:00 AM ET)
        bar = self.create_bar(9, 0, 500.0, 498.0, 499.0)
        await self.sm.process_minute_bar(bar)
        self.assertEqual(self.sm.orb_high, 0.0) # Should not update ORB high/low
        self.mock_adaptive_learner.classify_regime.assert_not_called()
        self.mock_adaptive_learner.select_candidate.assert_not_called()

    async def test_afterhours_no_double_order(self):
        # Build range and take position
        await self.sm.process_minute_bar(self.create_bar(9, 35, 501.0, 499.0, 500.0))
        await self.sm.process_minute_bar(self.create_bar(9, 45, 501.0, 499.0, 500.0))
        self.sm.orb_high = 501.0
        self.sm.orb_low = 499.0

        # Mock adaptive learner
        self.mock_adaptive_learner.classify_regime.return_value = "range"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {"stop_loss_cents": 10, "risk_reward_ratio": 2.0, "position_size_multiplier": 1.0},
            "base",
            {}
        )

        # First breakout
        self.mock_trader.execute_orb_setup.return_value = {
            "order_id": "order_afterhours_1", "entry_price": 502.0, "quantity": 1,
            "stop_loss_price": 501.90, "take_profit_price": 502.10
        }
        self.mock_trader.logger_obj._get_connection.return_value.execute.return_value.fetchone.return_value = {"id": 4}
        await self.sm.process_minute_bar(self.create_bar(9, 46, 503.0, 501.0, 502.0))
        self.assertTrue(self.sm.position_taken)
        self.mock_trader.execute_orb_setup.assert_called_once()

        # Simulate after-hours bar (e.g., 15:00 ET, after 4 PM market close)
        self.mock_trader.execute_orb_setup.reset_mock() # Clear previous call
        self.mock_adaptive_learner.classify_regime.reset_mock()
        self.mock_adaptive_learner.select_candidate.reset_mock()
        await self.sm.process_minute_bar(self.create_bar(15, 0, 510.0, 505.0, 508.0))
        self.assertEqual(self.mock_trader.execute_orb_setup.call_count, 0) # No new order
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
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) # NEW: Mock AdaptiveLearner
        self.config = {
            "account_capital": 500.0,
            "risk_per_trade_usd": 50.0,
            "stop_loss_cents": 10,
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0
        }
        self.sm = ORBStateMachine("SPY", self.mock_trader, self.config, self.mock_adaptive_learner) # NEW: Pass adaptive_learner

    def create_bar(self, day, hour, minute, high, low, close, symbol="SPY"):
        dt = ET.localize(datetime(2026, 6, day, hour, minute, 0))
        mock_bar = MagicMock()
        mock_bar.symbol = symbol
        mock_bar.timestamp = dt.astimezone(UTC)
        mock_bar.high = high
        mock_bar.low = low
        mock_bar.close = close
        return mock_bar

    async def test_force_reset_via_check_daily(self):
        """Simulate multi-day by manipulating last_reset_date."""
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)
        self.sm.position_taken = True
        self.sm.range_established = True
        self.sm.orb_high, self.sm.orb_low = 505.0, 503.0
        self.sm.active_trade_id = 10 # NEW: Set internal trade ID
        self.sm.active_trade_order_id = "test_order_reset"
        self.sm.current_regime = "trend"
        self.sm.current_candidate_id = "base"

        # Process a bar on the new day, which should trigger a reset
        bar = self.create_bar(16, 9, 31, 500.0, 498.0, 499.0) # Day 16
        await self.sm.process_minute_bar(bar)

        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 500.0)
        self.assertEqual(self.sm.orb_low, 498.0)
        self.assertEqual(self.sm.last_reset_date, datetime(2026, 6, 16).date())
        self.assertIsNone(self.sm.active_trade_id) # NEW: Check reset of internal trade ID
        self.assertIsNone(self.sm.active_trade_order_id)
        self.assertEqual(self.sm.current_regime, "unknown") # NEW: Check reset of regime
        self.assertEqual(self.sm.current_candidate_id, "default") # NEW: Check reset of candidate ID

# =============================================================================
# Test: Analytics
# =============================================================================
class TestTradeAnalytics(unittest.TestCase):
    """Test the analytics/reporting module."""

    def setUp(self):
        import analytics as an
        self.an = an
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(CREATE_TRADES_TABLE)
        conn.commit()
        conn.close()
        self.analytics = self.an.TradeAnalytics(db_path=self.db_path)

    def tearDown(self):
        self.analytics.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert(self, status, pnl, ticker="SPY", regime=None, candidate_id=None):
        self.analytics.conn.execute('''INSERT INTO trades
            (timestamp_entry, timestamp_exit, ticker, setup_type, entry_price, exit_price, quantity, stop_loss, take_profit, status, pnl, regime, candidate_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', ("2026-06-15 10:00:00", "2026-06-15 11:00:00", ticker, "ORB_LONG",
              500.0, 502.0, 1, 499.90, 502.0, status, pnl, regime, candidate_id))
        self.analytics.conn.commit()

    def test_fetch_empty(self):
        self.assertEqual(self.analytics.fetch_closed_trades(), [])

    def test_fetch_one(self):
        self._insert("CLOSED", 2.0)
        trades = self.analytics.fetch_closed_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['pnl'], 2.0)
        self.assertIsNone(trades[0]['regime']) # NEW: Check for new columns
        self.assertIsNone(trades[0]['candidate_id']) # NEW: Check for new columns

    def test_fetch_with_adaptive_data(self):
        self._insert("CLOSED", 2.0, regime="trend", candidate_id="base")
        trades = self.analytics.fetch_closed_trades()
        self.assertEqual(trades[0]['regime'], "trend")
        self.assertEqual(trades[0]['candidate_id'], "base")

    def test_fetch_filters_open(self):
        self._insert("OPEN", None)
        self._insert("CLOSED", 2.0)
        self.assertEqual(len(self.analytics.fetch_closed_trades()), 1)

    def test_fetch_by_ticker(self):
        self._insert("CLOSED", 2.0)
        self._insert("CLOSED", 1.5, ticker="QQQ")
        self.assertEqual(len(self.analytics.fetch_closed_trades(ticker="SPY")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(ticker="QQQ")), 1)

    def test_fetch_by_regime(self):
        self._insert("CLOSED", 2.0, regime="trend")
        self._insert("CLOSED", 1.0, regime="range")
        self.assertEqual(len(self.analytics.fetch_closed_trades(regime="trend")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(regime="range")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(regime="unknown")), 0)

    def test_fetch_by_candidate_id(self):
        self._insert("CLOSED", 2.0, candidate_id="base")
        self._insert("CLOSED", 1.0, candidate_id="wide_rr")
        self.assertEqual(len(self.analytics.fetch_closed_trades(candidate_id="base")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(candidate_id="wide_rr")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(candidate_id="unknown")), 0)

    def test_fetch_by_multiple_filters(self):
        self._insert("CLOSED", 2.0, ticker="SPY", regime="trend", candidate_id="base")
        self._insert("CLOSED", 1.0, ticker="QQQ", regime="range", candidate_id="wide_rr")
        self._insert("CLOSED", 3.0, ticker="SPY", regime="range", candidate_id="base")
        trades = self.analytics.fetch_closed_trades(ticker="SPY", regime="trend", candidate_id="base")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['pnl'], 2.0)

    def test_report_all_winning(self):
        for _ in range(3):
            self._insert("CLOSED", 2.0)
        r = self.analytics.generate_performance_report()
        self.assertEqual(r['total_trades'], 3)
        self.assertEqual(r['winning_trades'], 3)
        self.assertEqual(r['net_pnl'], 6.0)

    def test_report_mixed(self):
        self._insert("CLOSED", 2.0)
        self._insert("CLOSED", -1.0)
        self._insert("CLOSED", 2.0)
        r = self.analytics.generate_performance_report()
        self.assertEqual(r['total_trades'], 3)
        self.assertEqual(r['winning_trades'], 2)
        self.assertEqual(r['net_pnl'], 3.0)

    def test_report_all_losing(self):
        for _ in range(2):
            self._insert("CLOSED", -1.0)
        r = self.analytics.generate_performance_report()
        self.assertEqual(r['winning_trades'], 0)
        self.assertEqual(r['win_rate_pct'], 0.0)

    def test_report_empty(self):
        self.assertIsNone(self.analytics.generate_performance_report())

    def test_report_by_regime(self):
        self._insert("CLOSED", 2.0, regime="trend")
        self._insert("CLOSED", -1.0, regime="trend")
        self._insert("CLOSED", 1.0, regime="range")
        report = self.analytics.generate_performance_report(regime="trend")
        self.assertIsNotNone(report)
        self.assertEqual(report['total_trades'], 2)
        self.assertEqual(report['net_pnl'], 1.0)
        self.assertEqual(report['regime'], "trend")

    def test_report_by_candidate_id(self):
        self._insert("CLOSED", 2.0, candidate_id="base")
        self._insert("CLOSED", -1.0, candidate_id="base")
        self._insert("CLOSED", 1.0, candidate_id="wide_rr")
        report = self.analytics.generate_performance_report(candidate_id="base")
        self.assertIsNotNone(report)
        self.assertEqual(report['total_trades'], 2)
        self.assertEqual(report['net_pnl'], 1.0)
        self.assertEqual(report['candidate_id'], "base")

# =============================================================================
# Test: WebSocket Reconnection
# =============================================================================
class TestWebSocketReconnect(unittest.TestCase):
    """Test WebSocket connection and reconnection logic."""

    @patch('upgainpulse.StockDataStream')
    async def test_connect_success(self, MockStockDataStream):
        mock_stream_instance = MockStockDataStream.return_value
        ws = RobustWebSocketManager("key", "secret", max_retries=3)
        result = await ws.connect_with_retry()
        self.assertEqual(result, mock_stream_instance)
        MockStockDataStream.assert_called_once_with("key", "secret")

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_connect_eventually_succeeds(self, mock_sleep, MockStockDataStream):
        MockStockDataStream.side_effect = [Exception("fail1"), Exception("fail2"), MockStockDataStream.return_value]
        ws = RobustWebSocketManager("key", "secret", max_retries=3)
        result = await ws.connect_with_retry()
        self.assertIsNotNone(result)
        self.assertEqual(MockStockDataStream.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_connect_exhausted_retries(self, mock_sleep, MockStockDataStream):
        MockStockDataStream.side_effect = Exception("always fails")
        ws = RobustWebSocketManager("key", "secret", max_retries=2)
        with self.assertRaises(ConnectionError):
            await ws.connect_with_retry()
        self.assertEqual(MockStockDataStream.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_run_with_reconnect_reapplies_subscriptions(self, mock_sleep, MockStockDataStream):
        mock_stream_instance_1 = MagicMock()
        mock_stream_instance_2 = MagicMock()
        
        # Simulate stream failing once, then succeeding
        MockStockDataStream.side_effect = [
            mock_stream_instance_1, # Initial connect
            Exception("Stream disconnected"), # First run_forever fails
            mock_stream_instance_2, # Reconnect attempt
            MagicMock() # Second run_forever succeeds
        ]

        ws = RobustWebSocketManager("key", "secret", max_retries=2)
        
        # Initial connection and subscription
        stream = await ws.connect_with_retry()
        mock_handler = MagicMock()
        ws.subscribe_bars(mock_handler, "SPY", "QQQ")
        
        # Simulate run_forever loop
        mock_stream_instance_1._run_forever.side_effect = Exception("Simulated disconnect")
        mock_stream_instance_2._run_forever.side_effect = asyncio.CancelledError # To stop the loop

        with self.assertRaises(asyncio.CancelledError): # Expecting the loop to be cancelled
            await ws.run_with_reconnect()

        # Verify initial subscription
        mock_stream_instance_1.subscribe_bars.assert_called_once_with(mock_handler, "SPY", "QQQ")
        
        # Verify reconnection and re-subscription
        mock_stream_instance_2.subscribe_bars.assert_called_once_with(mock_handler, "SPY", "QQQ")
        self.assertEqual(mock_sleep.call_count, 1) # One sleep for the reconnect

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

    def _insert_trade(self, ticker, pnl, regime, candidate_id, status="CLOSED", trade_id=None):
        if trade_id is None:
            cursor = self.learner.conn.execute("SELECT MAX(id) FROM trades")
            trade_id = (cursor.fetchone()[0] or 0) + 1
        self.learner.conn.execute("""
            INSERT INTO trades (id, timestamp_entry, timestamp_exit, ticker, setup_type, entry_price, exit_price, quantity, stop_loss, take_profit, status, pnl, regime, candidate_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              ticker, "ORB_LONG", 100.0, 100.0 + pnl, 1, 99.0, 101.0, status, pnl, regime, candidate_id))
        self.learner.conn.commit()
        return trade_id

    def test_seed_candidates(self):
        candidates = self.learner.get_enabled_candidates()
        self.assertEqual(len(candidates), len(DEFAULT_CANDIDATES))
        self.assertEqual(candidates[0].candidate_id, "base")

    def test_classify_regime(self):
        self.assertEqual(self.learner.classify_regime(atr_pct=3.0, gap_pct=0.5, market_above_ma=True, rel_volume=1.0), "high_vol_trend")
        self.assertEqual(self.learner.classify_regime(atr_pct=0.5, gap_pct=0.2, market_above_ma=True, rel_volume=1.5), "trend")
        self.assertEqual(self.learner.classify_regime(atr_pct=0.5, gap_pct=0.2, market_above_ma=False, rel_volume=0.8), "quiet_range")
        self.assertEqual(self.learner.classify_regime(atr_pct=1.5, gap_pct=0.5, market_above_ma=True, rel_volume=1.0), "range")

    def test_log_trade_features(self):
        trade_id = self._insert_trade("SPY", 1.0, "trend", "base")
        self.learner.log_trade_features(trade_id, "SPY", "trend", "base", 0.5, 0.1, 1.2, 1.0, "up")
        cursor = self.learner.conn.execute("SELECT * FROM trade_features WHERE trade_id=?", (trade_id,))
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['ticker'], "SPY")
        self.assertEqual(row['regime'], "trend")
        self.assertEqual(row['candidate_id'], "base")
        self.assertEqual(row['orb_width_pct'], 0.5)

    def test_candidate_stats_empty(self):
        stats = self.learner._candidate_stats("SPY", "trend", "base")
        self.assertEqual(stats['n'], 0)
        self.assertEqual(stats['score'], -999.0)

    def test_candidate_stats_winning(self):
        self._insert_trade("SPY", 1.0, "trend", "base")
        self._insert_trade("SPY", 2.0, "trend", "base")
        stats = self.learner._candidate_stats("SPY", "trend", "base")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], 1.5)
        self.assertAlmostEqual(stats['win_rate'], 1.0)
        self.assertAlmostEqual(stats['profit_factor'], 999.0) # All wins

    def test_candidate_stats_losing(self):
        self._insert_trade("SPY", -1.0, "trend", "base")
        self._insert_trade("SPY", -0.5, "trend", "base")
        stats = self.learner._candidate_stats("SPY", "trend", "base")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], -0.75)
        self.assertAlmostEqual(stats['win_rate'], 0.0)
        self.assertAlmostEqual(stats['profit_factor'], 0.0) # All losses

    def test_candidate_stats_mixed(self):
        self._insert_trade("SPY", 2.0, "trend", "base")
        self._insert_trade("SPY", -1.0, "trend", "base")
        stats = self.learner._candidate_stats("SPY", "trend", "base")
        self.assertEqual(stats['n'], 2)
        self.assertAlmostEqual(stats['expectancy'], 0.5)
        self.assertAlmostEqual(stats['win_rate'], 0.5)
        self.assertAlmostEqual(stats['profit_factor'], 2.0)

    def test_select_candidate_no_exploration(self):
        # With exploration_rate = 0.0, it should always pick the best score
        self._insert_trade("SPY", 2.0, "trend", "base")
        self._insert_trade("SPY", -1.0, "trend", "base")
        self._insert_trade("SPY", 3.0, "trend", "wide_rr")
        self._insert_trade("SPY", 0.5, "trend", "wide_rr")

        # base: expectancy 0.5, win_rate 0.5, PF 2.0
        # wide_rr: expectancy 1.75, win_rate 1.0, PF 999.0

        chosen_config, candidate_id, _ = self.learner.select_candidate("SPY", "trend", self.config)
        self.assertEqual(candidate_id, "wide_rr")
        self.assertEqual(chosen_config["stop_loss_cents"], 15) # From wide_rr

    @patch('random.random', return_value=0.05) # Force exploration
    def test_select_candidate_with_exploration(self, mock_random):
        self.learner.exploration_rate = 0.1
        self._insert_trade("SPY", 2.0, "trend", "base")
        self._insert_trade("SPY", -1.0, "trend", "base")
        self._insert_trade("SPY", 3.0, "trend", "wide_rr")
        self._insert_trade("SPY", 0.5, "trend", "wide_rr")

        # With exploration, it might pick a random one even if another has a better score
        chosen_config, candidate_id, _ = self.learner.select_candidate("SPY", "trend", self.config)
        # Since random.random() < exploration_rate, it should pick a random candidate
        # We can't assert a specific candidate, but we can assert it's one of the defaults
        self.assertIn(candidate_id, [c.candidate_id for c in DEFAULT_CANDIDATES])

    def test_select_candidate_low_sample_penalty(self):
        self.learner.exploration_rate = 0.0 # No random exploration
        self.learner.min_trades_before_promote = 5

        # base has 2 trades, wide_rr has 1 trade
        self._insert_trade("SPY", 2.0, "trend", "base")
        self._insert_trade("SPY", -1.0, "trend", "base")
        self._insert_trade("SPY", 3.0, "trend", "wide_rr")

        # Even if wide_rr has a higher raw score, it has fewer than min_trades_before_promote
        # so it should be penalized, potentially leading to 'base' being chosen.
        chosen_config, candidate_id, _ = self.learner.select_candidate("SPY", "trend", self.config)
        self.assertEqual(candidate_id, "base") # Base should be chosen due to penalty on wide_rr

# =============================================================================
# Test: WebSocket Reconnection
# =============================================================================
class TestWebSocketReconnect(unittest.TestCase):
    """Test WebSocket connection and reconnection logic."""

    @patch('upgainpulse.StockDataStream')
    async def test_connect_success(self, MockStockDataStream):
        mock_stream_instance = MockStockDataStream.return_value
        ws = RobustWebSocketManager("key", "secret", max_retries=3)
        result = await ws.connect_with_retry()
        self.assertEqual(result, mock_stream_instance)
        MockStockDataStream.assert_called_once_with("key", "secret")

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_connect_eventually_succeeds(self, mock_sleep, MockStockDataStream):
        MockStockDataStream.side_effect = [Exception("fail1"), Exception("fail2"), MockStockDataStream.return_value]
        ws = RobustWebSocketManager("key", "secret", max_retries=3)
        result = await ws.connect_with_retry()
        self.assertIsNotNone(result)
        self.assertEqual(MockStockDataStream.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_connect_exhausted_retries(self, mock_sleep, MockStockDataStream):
        MockStockDataStream.side_effect = Exception("always fails")
        ws = RobustWebSocketManager("key", "secret", max_retries=2)
        with self.assertRaises(ConnectionError):
            await ws.connect_with_retry()
        self.assertEqual(MockStockDataStream.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('upgainpulse.StockDataStream')
    @patch('asyncio.sleep', new_callable=MagicMock)
    async def test_run_with_reconnect_reapplies_subscriptions(self, mock_sleep, MockStockDataStream):
        mock_stream_instance_1 = MagicMock()
        mock_stream_instance_2 = MagicMock()
        
        # Simulate stream failing once, then succeeding
        MockStockDataStream.side_effect = [
            mock_stream_instance_1, # Initial connect
            Exception("Stream disconnected"), # First run_forever fails
            mock_stream_instance_2, # Reconnect attempt
            MagicMock() # Second run_forever succeeds
        ]

        ws = RobustWebSocketManager("key", "secret", max_retries=2)
        
        # Initial connection and subscription
        stream = await ws.connect_with_retry()
        mock_handler = MagicMock()
        ws.subscribe_bars(mock_handler, "SPY", "QQQ")
        
        # Simulate run_forever loop
        mock_stream_instance_1._run_forever.side_effect = Exception("Simulated disconnect")
        mock_stream_instance_2._run_forever.side_effect = asyncio.CancelledError # To stop the loop

        with self.assertRaises(asyncio.CancelledError): # Expecting the loop to be cancelled
            await ws.run_with_reconnect()

        # Verify initial subscription
        mock_stream_instance_1.subscribe_bars.assert_called_once_with(mock_handler, "SPY", "QQQ")
        
        # Verify reconnection and re-subscription
        mock_stream_instance_2.subscribe_bars.assert_called_once_with(mock_handler, "SPY", "QQQ")
        self.assertEqual(mock_sleep.call_count, 1) # One sleep for the reconnect

# =============================================================================
# Test: Integration Scenarios
# =============================================================================
class TestIntegrationScenarios(unittest.TestCase):
    """Higher-level integration-style tests."""

    def setUp(self):
        self.mock_adaptive_learner = MagicMock(spec=AdaptiveLearner) # NEW: Mock AdaptiveLearner for integration tests
        self.mock_adaptive_learner.classify_regime.return_value = "trend"
        self.mock_adaptive_learner.select_candidate.return_value = (
            {"stop_loss_cents": 10, "risk_reward_ratio": 2.0, "position_size_multiplier": 1.0},
            "base",
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
                        "stop_loss_cents": 10, "position_size_multiplier": 1.0,
                        "risk_reward_ratio": 2.0}
            )
            self.assertEqual(len(engine.state_machines), 2)
            self.assertIn("SPY", engine.state_machines)
            self.assertIn("QQQ", engine.state_machines)
            self.assertIsNotNone(engine.adaptive_learner) # NEW: Check adaptive learner init

    @patch('upgainpulse.TradingClient')
    @patch('upgainpulse.AccountValidator')
    @patch('upgainpulse.TradeLogger')
    @patch('upgainpulse.RobustWebSocketManager')
    async def test_full_day_scenario(self, MockWSManager, MockTradeLogger, MockAccountValidator, MockTradingClient):
        # Mock dependencies
        mock_stream = MagicMock()
        MockWSManager.return_value.connect_with_retry.return_value = mock_stream
        MockWSManager.return_value.run_with_reconnect.side_effect = asyncio.CancelledError # To stop the engine loop
        
        mock_trader_instance = MagicMock(spec=AlpacaPaperTrader)
        mock_trader_instance.logger_obj = MockTradeLogger.return_value # Ensure logger is mocked correctly
        MockTradingClient.return_value = MagicMock() # For AlpacaPaperTrader init
        MockAccountValidator.return_value.get_buying_power.return_value = 5000.0 # Sufficient BP

        # Mock execute_orb_setup to return order details
        mock_trader_instance.execute_orb_setup.return_value = {
            "order_id": "test_order_SPY", "entry_price": 196.30, "quantity": 1,
            "stop_loss_price": 196.20, "take_profit_price": 196.50
        }

        # Patch AlpacaPaperTrader and AdaptiveLearner to return our mock instances
        with patch('upgainpulse.AlpacaPaperTrader', return_value=mock_trader_instance), \
             patch('upgainpulse.AdaptiveLearner', return_value=self.mock_adaptive_learner): # NEW: Patch AdaptiveLearner
            sm = ORBStateMachine("AAPL", mock_trader_instance, {
                "risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0
            }, self.mock_adaptive_learner) # NEW: Pass adaptive_learner

            def create_bar(h, m, hi, lo, cl):
                d = ET.localize(datetime(2026, 6, 15, h, m, 0))
                b = MagicMock()
                b.symbol, b.timestamp, b.high, b.low, b.close = "AAPL", d.astimezone(UTC), hi, lo, cl
                return b

            # Simulate ORB range building
            await sm.process_minute_bar(create_bar(9, 31, 195.5, 194.8, 195.2))
            self.assertEqual(sm.orb_high, 195.5)
            self.assertEqual(sm.orb_low, 194.8)

            await sm.process_minute_bar(create_bar(9, 38, 196.0, 195.3, 195.8))
            self.assertEqual(sm.orb_high, 196.0)
            self.assertEqual(sm.orb_low, 194.8)

            await sm.process_minute_bar(create_bar(9, 44, 195.5, 194.5, 194.5))
            self.assertEqual(sm.orb_high, 196.0)
            self.assertEqual(sm.orb_low, 194.5)

            # Lock range at 9:45
            await sm.process_minute_bar(create_bar(9, 45, 195.0, 194.0, 194.5))
            self.assertTrue(sm.range_established)
            self.assertFalse(sm.position_taken)

            # Breakout and order execution
            breakout_bar = create_bar(9, 46, 196.5, 195.0, 196.3)
            await sm.process_minute_bar(breakout_bar)
            self.assertTrue(sm.position_taken)
            self.mock_adaptive_learner.classify_regime.assert_called_once() # NEW: Verify adaptive calls
            self.mock_adaptive_learner.select_candidate.assert_called_once() # NEW: Verify adaptive calls
            mock_trader_instance.execute_orb_setup.assert_called_once_with("AAPL", 196.3, {
                "risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0
            }) # NEW: Config passed to trader is the adaptive one
            self.assertEqual(sm.active_trade_order_id, "test_order_SPY")

            # Simulate TP hit
            tp_hit_bar = create_bar(9, 47, 196.60, 196.40, 196.50) # High hits TP (196.50)
            await sm.process_minute_bar(tp_hit_bar)
            self.assertFalse(sm.position_taken)
            MockTradeLogger.return_value.update_exit_details.assert_called_once_with(
                "test_order_SPY", 196.50, round((196.50 - 196.30) * 1, 2), tp_hit_bar.timestamp
            )
            self.mock_adaptive_learner.log_trade_features.assert_called_once() # NEW: Verify features logged

            # Simulate a new day reset and another trade
            sm.last_reset_date = datetime.now().date() - timedelta(days=1)
            sm.position_taken = False # Reset for new day
            sm.range_established = False
            sm.orb_high = 0.0
            sm.orb_low = float('inf')

            await sm.process_minute_bar(create_bar(datetime.now().day, 9, 31, 200.0, 199.0, 199.5))
            await sm.process_minute_bar(create_bar(datetime.now().day, 9, 45, 201.0, 198.5, 200.0))
            sm.orb_high = 201.0
            sm.orb_low = 198.5

            mock_trader_instance.execute_orb_setup.reset_mock()
            self.mock_adaptive_learner.classify_regime.reset_mock()
            self.mock_adaptive_learner.select_candidate.reset_mock()
            mock_trader_instance.execute_orb_setup.return_value = {
                "order_id": "test_order_AAPL_day2", "entry_price": 201.50, "quantity": 2,
                "stop_loss_price": 201.30, "take_profit_price": 201.90
            }
            breakout_bar_day2 = create_bar(datetime.now().day, 9, 46, 202.0, 201.0, 201.50)
            await sm.process_minute_bar(breakout_bar_day2)
            self.assertTrue(sm.position_taken)
            self.mock_adaptive_learner.classify_regime.assert_called_once()
            self.mock_adaptive_learner.select_candidate.assert_called_once()
            mock_trader_instance.execute_orb_setup.assert_called_once_with("AAPL", 201.50, {
                "risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0
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
             patch('upgainpulse.AdaptiveLearner', return_value=self.mock_adaptive_learner): # NEW: Patch AdaptiveLearner
            sm = ORBStateMachine("SPY", mock_trader_instance, self.config, self.mock_adaptive_learner) # NEW: Pass adaptive_learner
            sm.position_taken = True
            sm.range_established = True
            sm.orb_high = 510.0
            sm.orb_low = 490.0
            sm.last_reset_date = datetime.now().date() - timedelta(days=1) # Simulate yesterday
            sm.active_trade_id = 5 # NEW: Set internal trade ID
            sm.active_trade_order_id = "test_order_reset_day"
            sm.current_regime = "high_vol_range"
            sm.current_candidate_id = "defensive"

            # Process a bar on the "new" day
            bar_new_day = self.create_bar(datetime.now().day, 9, 31, 500.0, 498.0, 499.0)
            await sm.process_minute_bar(bar_new_day)

            self.assertFalse(sm.position_taken)
            self.assertFalse(sm.range_established)
            self.assertEqual(sm.orb_high, 500.0) # Should be reset and updated by the new bar
            self.assertEqual(sm.orb_low, 498.0) # Should be reset and updated by the new bar
            self.assertEqual(sm.last_reset_date, datetime.now().date())
            self.assertIsNone(sm.active_trade_id) # NEW: Check reset of internal trade ID
            self.assertIsNone(sm.active_trade_order_id)
            self.assertEqual(sm.current_regime, "unknown") # NEW: Check reset of regime
            self.assertEqual(sm.current_candidate_id, "default") # NEW: Check reset of candidate ID


if __name__ == "__main__":
    unittest.main(argv=['first-arg-is-ignored'], exit=False, verbosity=2)
