"""
UpGainPulse v2.0 - Comprehensive Unit Tests

Run with: python test_core.py
"""
import unittest
from unittest.mock import Mock, patch, MagicMock, PropertyMock
import threading
import sqlite3
import os
import tempfile
import asyncio
from datetime import datetime, time, timedelta
from pytz import timezone

ET = timezone('US/Eastern')
UTC = timezone('UTC')

# ==========================================
# Mock external dependencies before importing upgainpulse
# ==========================================
import sys

# Create a real Exception subclass for APIError so except clauses work
class MockAPIError(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.message = msg

# Set env vars FIRST (before any import of upgainpulse)
os.environ['ALPACA_PAPER_API_KEY'] = 'test_key'
os.environ['ALPACA_PAPER_SECRET_KEY'] = 'test_secret'

# Mock the alpaca modules and dotenv
for mod_name in ['alpaca', 'alpaca.trading', 'alpaca.trading.client',
                 'alpaca.trading.requests', 'alpaca.trading.enums',
                 'alpaca.trading.models', 'alpaca.data',
                 'alpaca.data.live', 'alpaca.data.models', 'dotenv']:
    sys.modules[mod_name] = MagicMock()

# Set alpaca.trading.errors so upgainpulse can import APIError from it
sys.modules['alpaca.trading.errors'] = MagicMock()
sys.modules['alpaca.trading.errors'].APIError = MockAPIError

# Import upgainpulse (will use env vars and mocked modules)
import upgainpulse

# Patch APIError in the module to be a real Exception subclass
upgainpulse.APIError = MockAPIError

from upgainpulse import (
    convert_utc_to_et,
    validate_config, ConfigError,
    TradeLogger,
    AccountValidator,
    AlpacaPaperTrader,
    ORBStateMachine,
    RobustWebSocketManager,
    UpGainPulseEngine
)

# ==========================================
# Helper: create trade table (for analytics tests)
# ==========================================
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)'''

# ==========================================
# Test: Timezone Conversion
# ==========================================
class TestTimezoneConversion(unittest.TestCase):
    """Test UTC to ET conversion for market hours."""

    def test_convert_utc_to_et_during_market(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 13, 35, 0))
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 35)

    def test_convert_utc_to_et_naive_input(self):
        naive_dt = datetime(2026, 6, 15, 13, 35, 0)
        et_dt = convert_utc_to_et(naive_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 35)

    def test_convert_utc_to_et_premarket(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 8, 0, 0))
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 4)

    def test_convert_utc_to_et_afterhours(self):
        utc_dt = UTC.localize(datetime(2026, 6, 15, 23, 0, 0))
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 19)

    def test_convert_utc_to_et_winter(self):
        utc_dt = UTC.localize(datetime(2026, 1, 15, 14, 30, 0))
        et_dt = convert_utc_to_et(utc_dt)
        self.assertEqual(et_dt.hour, 9)
        self.assertEqual(et_dt.minute, 30)


# ==========================================
# Test: Configuration Validation
# ==========================================
class TestConfigValidation(unittest.TestCase):
    """Test configuration parameter validation."""

    def test_valid_config(self):
        validate_config({"risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                         "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0})

    def test_risk_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 0, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0})

    def test_risk_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": -10, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0})

    def test_sl_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 0,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0})

    def test_multiplier_zero(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 0, "risk_reward_ratio": 2.0})

    def test_rr_negative(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": -1})

    def test_rr_unrealistic(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": 50, "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 100})

    def test_missing_keys_use_defaults(self):
        validate_config({"risk_per_trade_usd": 50.0})

    def test_config_type_error(self):
        with self.assertRaises(ConfigError):
            validate_config({"risk_per_trade_usd": "abc", "stop_loss_cents": 10,
                             "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0})


# ==========================================
# Test: TradeLogger (Database)
# ==========================================
class TestTradeLogger(unittest.TestCase):
    """Test SQLite trade logging with thread safety."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.logger = TradeLogger(db_path=self.db_path)

    def tearDown(self):
        self.logger._get_connection().close()
        import gc; gc.collect()
        try:
            os.close(self.db_fd)
        except OSError:
            pass
        try:
            os.unlink(self.db_path)
        except PermissionError:
            pass

    def test_create_table(self):
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        self.assertIsNotNone(cursor.fetchone())

    def test_log_entry(self):
        self.logger.log_entry("SPY", "ORB_LONG", 500.0, 1, 499.90, 502.00, "order_123")
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE ticker='SPY'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[3], "SPY")
        self.assertEqual(row[4], "ORB_LONG")

    def test_log_entry_without_order_id(self):
        self.logger.log_entry("AAPL", "ORB_LONG", 180.0, 2, 179.90, 182.00, None)
        conn = self.logger._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE ticker='AAPL'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        # Column order: id(0), ts_entry(1), ts_exit(2), ticker(3), setup(4),
        # entry_price(5), exit_price(6), qty(7), sl(8), tp(9), status(10), order_id(11), pnl(12), created_at(13)
        self.assertIsNone(row[11])  # order_id at index 11

    def test_thread_safety(self):
        import time
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


# ==========================================
# Test: AccountValidator
# ==========================================
class TestAccountValidator(unittest.TestCase):
    """Test account buying power validation."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.patcher = patch('upgainpulse.APIError', MockAPIError)
        self.patcher.start()
        self.validator = AccountValidator(self.mock_client)

    def tearDown(self):
        self.patcher.stop()

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


# ==========================================
# Test: Position Sizing
# ==========================================
class TestPositionSizing(unittest.TestCase):
    """Test the position sizing formula."""

    def test_spy_at_500(self):
        self.assertEqual(int((50.0 / 0.10) / 500.0 * 1.0), 1)

    def test_spy_at_250(self):
        self.assertEqual(int((50.0 / 0.10) / 250.0 * 1.0), 2)

    def test_qqq_at_400(self):
        self.assertEqual(int((50.0 / 0.10) / 400.0 * 1.0), 1)

    def test_max_shares(self):
        self.assertEqual(int((50.0 / 0.10) / 10.0 * 1.0), 50)

    def test_small_risk_large_stop(self):
        self.assertEqual(int((50.0 / 1.00) / 500.0 * 1.0), 0)

    def test_double_multiplier(self):
        self.assertEqual(int((50.0 / 0.10) / 500.0 * 2.0), 2)


# ==========================================
# Test: TP/SL Calculation
# ==========================================
class TestTPSLCalculation(unittest.TestCase):
    """Test take-profit and stop-loss price calculations."""

    def test_basic(self):
        self.assertEqual(round(500.0 - 0.10, 2), 499.90)
        self.assertEqual(round(500.0 + (0.10 * 2.0), 2), 500.20)

    def test_larger_sl(self):
        self.assertEqual(round(500.0 - 0.50, 2), 499.50)
        self.assertEqual(round(500.0 + (0.50 * 2.0), 2), 501.00)

    def test_high_rr(self):
        self.assertEqual(round(500.0 - 0.10, 2), 499.90)
        self.assertEqual(round(500.0 + (0.10 * 3.0), 2), 500.30)

    def test_low_price(self):
        self.assertEqual(round(100.0 - 0.10, 2), 99.90)
        self.assertEqual(round(100.0 + (0.10 * 2.0), 2), 100.20)


# ==========================================
# Test: AlpacaPaperTrader execute_orb_setup
# ==========================================
class TestAlpacaPaperTrader(unittest.TestCase):
    """Test order execution logic."""

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_logger = MagicMock(spec=TradeLogger)
        self.mock_validator = MagicMock(spec=AccountValidator)

        with patch('upgainpulse.TradingClient', return_value=self.mock_client), \
             patch('upgainpulse.APIError', MockAPIError):
            self.trader = AlpacaPaperTrader(
                api_key='test', secret_key='test',
                logger_obj=self.mock_logger,
                account_validator=self.mock_validator
            )

        self.config = {
            "risk_per_trade_usd": 50.0,
            "stop_loss_cents": 10,
            "position_size_multiplier": 1.0,
            "risk_reward_ratio": 2.0
        }

    def test_execute_order_success(self):
        self.mock_validator.check_buying_power.return_value = True
        self.mock_client.submit_order.return_value = MagicMock(id="order_abc123")
        self.assertTrue(self.trader.execute_orb_setup("SPY", 500.0, self.config))
        self.mock_client.submit_order.assert_called_once()

    def test_execute_order_insufficient_bp(self):
        self.mock_validator.check_buying_power.return_value = False
        self.assertFalse(self.trader.execute_orb_setup("SPY", 500.0, self.config))
        self.mock_client.submit_order.assert_not_called()

    def test_execute_order_cooldown(self):
        self.mock_validator.check_buying_power.return_value = True
        self.mock_client.submit_order.return_value = MagicMock(id="order_001")
        self.assertTrue(self.trader.execute_orb_setup("SPY", 500.0, self.config))
        self.assertFalse(self.trader.execute_orb_setup("SPY", 500.0, self.config))
        self.mock_client.submit_order.return_value = MagicMock(id="order_002")
        self.assertTrue(self.trader.execute_orb_setup("QQQ", 400.0, self.config))

    def test_execute_order_qty_zero(self):
        config = {"risk_per_trade_usd": 1.0, "stop_loss_cents": 100,
                  "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
        self.assertFalse(self.trader.execute_orb_setup("SPY", 500.0, config))

    def test_execute_order_api_error(self):
        self.mock_validator.check_buying_power.return_value = True
        with patch('upgainpulse.APIError', MockAPIError):
            self.mock_client.submit_order.side_effect = MockAPIError("API error")
            self.assertFalse(self.trader.execute_orb_setup("SPY", 500.0, self.config))


# ==========================================
# Test: ORB State Machine
# ==========================================
class TestORBStateMachine(unittest.TestCase):
    """Test the Opening Range Breakout state machine."""

    def setUp(self):
        self.mock_trader = MagicMock(spec=AlpacaPaperTrader)
        self.mock_trader.execute_orb_setup.return_value = True
        self.config = {"risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                       "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
        self.sm = ORBStateMachine("SPY", self.mock_trader, self.config)

    def create_bar(self, ts, h, l, c, sym="SPY"):
        b = MagicMock()
        b.symbol, b.timestamp, b.high, b.low, b.close = sym, ts, h, l, c
        return b

    def test_initial_state(self):
        self.assertEqual(self.sm.orb_high, 0.0)
        self.assertEqual(self.sm.orb_low, float('inf'))
        self.assertFalse(self.sm.range_established)
        self.assertFalse(self.sm.position_taken)

    def test_reset_daily(self):
        self.sm.orb_high, self.sm.orb_low = 510.0, 490.0
        self.sm.range_established = True
        self.sm.position_taken = True
        self.sm.reset_daily()
        self.assertEqual(self.sm.orb_high, 0.0)
        self.assertEqual(self.sm.orb_low, float('inf'))
        self.assertFalse(self.sm.range_established)
        self.assertFalse(self.sm.position_taken)

    def test_build_range_during_orb_period(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        self.assertEqual(self.sm.orb_high, 501.0)
        self.assertEqual(self.sm.orb_low, 499.0)

    def test_range_updates_within_period(self):
        et1 = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        et2 = ET.localize(datetime(2026, 6, 15, 9, 36, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et1.astimezone(UTC), 501.0, 499.0, 500.0)))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et2.astimezone(UTC), 502.0, 500.5, 501.0)))
        self.assertEqual(self.sm.orb_high, 502.0)
        self.assertEqual(self.sm.orb_low, 499.0)

    def test_lock_range_at_945(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 502.0, 500.0, 501.0)))
        self.assertTrue(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 501.0)
        self.assertEqual(self.sm.orb_low, 499.0)

    def test_breakout_executes_order(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_b = ET.localize(datetime(2026, 6, 15, 9, 46, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_b.astimezone(UTC), 503.0, 501.0, 502.50)))
        self.assertTrue(self.sm.position_taken)
        self.mock_trader.execute_orb_setup.assert_called_once_with("SPY", 502.50, self.config)

    def test_no_breakout_below_range(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_n = ET.localize(datetime(2026, 6, 15, 9, 46, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_n.astimezone(UTC), 499.50, 498.0, 498.50)))
        self.assertFalse(self.sm.position_taken)

    def test_no_double_order(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_b = ET.localize(datetime(2026, 6, 15, 9, 46, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_b.astimezone(UTC), 503.0, 501.0, 502.0)))
        self.assertEqual(self.mock_trader.execute_orb_setup.call_count, 1)
        et_b2 = ET.localize(datetime(2026, 6, 15, 9, 47, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_b2.astimezone(UTC), 504.0, 502.0, 503.0)))
        self.assertEqual(self.mock_trader.execute_orb_setup.call_count, 1)

    def test_retry_on_failed_order(self):
        self.mock_trader.execute_orb_setup.return_value = False
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_b = ET.localize(datetime(2026, 6, 15, 9, 46, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_b.astimezone(UTC), 503.0, 501.0, 502.0)))
        self.assertFalse(self.sm.position_taken)

    def test_premarket_no_action(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 0, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 500.0, 498.0, 499.0)))
        self.assertEqual(self.sm.orb_high, 0.0)

    def test_afterhours_no_double_order(self):
        et = ET.localize(datetime(2026, 6, 15, 9, 35, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_l = ET.localize(datetime(2026, 6, 15, 9, 45, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_l.astimezone(UTC), 501.0, 499.0, 500.0)))
        et_t = ET.localize(datetime(2026, 6, 15, 9, 46, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_t.astimezone(UTC), 503.0, 501.0, 502.0)))
        et_late = ET.localize(datetime(2026, 6, 15, 15, 0, 0))
        asyncio.run(self.sm.process_minute_bar(self.create_bar(et_late.astimezone(UTC), 510.0, 505.0, 508.0)))
        self.assertEqual(self.mock_trader.execute_orb_setup.call_count, 1)

    def test_check_daily_reset_triggers(self):
        self.sm.position_taken = True
        self.sm.range_established = True
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)
        self.sm.check_daily_reset()
        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)

    def test_check_daily_reset_noop(self):
        self.sm.position_taken = True
        self.sm.check_daily_reset()
        self.assertTrue(self.sm.position_taken)


# ==========================================
# Test: Multi-Day Reset (via process_minute_bar)
# ==========================================
class TestMultiDayReset(unittest.TestCase):
    """Test state reset via process_minute_bar."""

    def setUp(self):
        self.mock_trader = MagicMock(spec=AlpacaPaperTrader)
        self.mock_trader.execute_orb_setup.return_value = True
        self.config = {"risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
                       "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0}
        self.sm = ORBStateMachine("SPY", self.mock_trader, self.config)

    def test_force_reset_via_check_daily(self):
        """Simulate multi-day by manipulating last_reset_date."""
        self.sm.last_reset_date = datetime.now().date() - timedelta(days=1)
        self.sm.position_taken = True
        self.sm.range_established = True
        self.sm.orb_high, self.sm.orb_low = 505.0, 503.0
        self.sm.check_daily_reset()
        self.assertFalse(self.sm.position_taken)
        self.assertFalse(self.sm.range_established)
        self.assertEqual(self.sm.orb_high, 0.0)
        self.assertEqual(self.sm.orb_low, float('inf'))


# ==========================================
# Test: Analytics
# ==========================================
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

    def _insert(self, status, pnl, ticker="SPY"):
        self.analytics.conn.execute('''INSERT INTO trades
            (timestamp_entry, timestamp_exit, ticker, setup_type, entry_price, exit_price, quantity, stop_loss, take_profit, status, pnl)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            ("2026-06-15 10:00:00", "2026-06-15 11:00:00", ticker, "ORB_LONG",
             500.0, 502.0, 1, 499.90, 502.0, status, pnl))
        self.analytics.conn.commit()

    def test_fetch_empty(self):
        self.assertEqual(self.analytics.fetch_closed_trades(), [])

    def test_fetch_one(self):
        self._insert("CLOSED", 2.0)
        trades = self.analytics.fetch_closed_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['pnl'], 2.0)

    def test_fetch_filters_open(self):
        self._insert("OPEN", None)
        self._insert("CLOSED", 2.0)
        self.assertEqual(len(self.analytics.fetch_closed_trades()), 1)

    def test_fetch_by_ticker(self):
        self._insert("CLOSED", 2.0)
        self._insert("CLOSED", 1.5, ticker="QQQ")
        self.assertEqual(len(self.analytics.fetch_closed_trades(ticker="SPY")), 1)
        self.assertEqual(len(self.analytics.fetch_closed_trades(ticker="QQQ")), 1)

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


# ==========================================
# Test: WebSocket Reconnection
# ==========================================
class TestWebSocketReconnect(unittest.TestCase):
    """Test WebSocket connection and reconnection logic."""

    def test_connect_success(self):
        with patch('upgainpulse.StockDataStream') as cls:
            mock = MagicMock()
            cls.return_value = mock
            ws = RobustWebSocketManager("key", "secret", max_retries=3)
            result = asyncio.run(ws.connect_with_retry())
            self.assertEqual(result, mock)

    def test_connect_eventually_succeeds(self):
        with patch('upgainpulse.StockDataStream') as cls:
            cls.side_effect = [Exception("fail1"), Exception("fail2"), MagicMock()]
            ws = RobustWebSocketManager("key", "secret", max_retries=3)
            self.assertIsNotNone(asyncio.run(ws.connect_with_retry()))

    def test_connect_exhausted(self):
        with patch('upgainpulse.StockDataStream') as cls:
            cls.side_effect = Exception("always fails")
            ws = RobustWebSocketManager("key", "secret", max_retries=2)
            with self.assertRaises(ConnectionError):
                asyncio.run(ws.connect_with_retry())


# ==========================================
# Test: Integration Scenarios
# ==========================================
class TestIntegrationScenarios(unittest.TestCase):
    """Higher-level integration-style tests."""

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

    def test_full_day_scenario(self):
        sm = ORBStateMachine("AAPL", MagicMock(), {
            "risk_per_trade_usd": 50.0, "stop_loss_cents": 10,
            "position_size_multiplier": 1.0, "risk_reward_ratio": 2.0
        })

        def bar(h, m, hi, lo, cl):
            d = ET.localize(datetime(2026, 6, 15, h, m, 0))
            b = MagicMock()
            b.symbol, b.timestamp, b.high, b.low, b.close = "AAPL", d.astimezone(UTC), hi, lo, cl
            return b

        asyncio.run(sm.process_minute_bar(bar(9, 31, 195.5, 194.8, 195.2)))
        self.assertEqual(sm.orb_high, 195.5)
        self.assertEqual(sm.orb_low, 194.8)

        asyncio.run(sm.process_minute_bar(bar(9, 38, 196.0, 195.3, 195.8)))
        self.assertEqual(sm.orb_high, 196.0)
        self.assertEqual(sm.orb_low, 194.8)

        asyncio.run(sm.process_minute_bar(bar(9, 44, 195.5, 194.5, 194.5)))
        self.assertEqual(sm.orb_high, 196.0)
        self.assertEqual(sm.orb_low, 194.5)

        asyncio.run(sm.process_minute_bar(bar(9, 45, 195.0, 194.0, 194.5)))
        self.assertTrue(sm.range_established)
        self.assertFalse(sm.position_taken)

        asyncio.run(sm.process_minute_bar(bar(9, 46, 196.5, 195.0, 196.3)))
        self.assertTrue(sm.position_taken)


# ==========================================
# Run Tests
# ==========================================
if __name__ == "__main__":
    unittest.main(verbosity=2)