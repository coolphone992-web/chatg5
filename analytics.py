import sqlite3
import pandas as pd
import logging
from datetime import datetime
from typing import Optional, Dict, List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TradeAnalytics:
    def __init__(self, db_path="upgainpulse_paper.db"):
        self.db_path = db_path
        self.conn = None
        self.connect()

    def connect(self):
        try:
            self.conn = sqlite3.connect(self.db_path, timeout=10.0)
            self.conn.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            logger.error(f"Connection failed: {e}")
            raise

    def close(self):
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def fetch_closed_trades(self, ticker: Optional[str] = None) -> List[Dict]:
        if not self.conn:
            return []
        try:
            cursor = self.conn.cursor()
            if ticker:
                cursor.execute('SELECT id, timestamp_entry, timestamp_exit, ticker, entry_price, exit_price, quantity, pnl FROM trades WHERE status = "CLOSED" AND exit_price IS NOT NULL AND ticker = ? ORDER BY timestamp_entry DESC', (ticker,))
            else:
                cursor.execute('SELECT id, timestamp_entry, timestamp_exit, ticker, entry_price, exit_price, quantity, pnl FROM trades WHERE status = "CLOSED" AND exit_price IS NOT NULL ORDER BY timestamp_entry DESC')
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Query failed: {e}")
            return []

    def generate_performance_report(self, ticker: Optional[str] = None) -> Optional[Dict]:
        trades = self.fetch_closed_trades(ticker=ticker)
        if not trades:
            return None
        winning_trades = []
        losing_trades = []
        gross_profit = 0.0
        gross_loss = 0.0
        for trade in trades:
            pnl = float(trade['pnl']) if trade['pnl'] else 0.0
            if pnl > 0:
                winning_trades.append(pnl)
                gross_profit += pnl
            else:
                losing_trades.append(pnl)
                gross_loss += abs(pnl)
        total_trades = len(trades)
        winning_count = len(winning_trades)
        losing_count = len(losing_trades)
        win_rate = (winning_count / total_trades * 100) if total_trades > 0 else 0
        avg_win = sum(winning_trades) / winning_count if winning_trades else 0
        avg_loss = sum(losing_trades) / losing_count if losing_trades else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        return {"ticker": ticker or "ALL", "total_trades": total_trades, "winning_trades": winning_count, "losing_trades": losing_count, "win_rate_pct": round(win_rate, 2), "gross_profit": round(gross_profit, 2), "gross_loss": round(gross_loss, 2), "net_pnl": round(gross_profit - gross_loss, 2), "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2), "profit_factor": round(profit_factor, 2)}

    def print_report(self, ticker: Optional[str] = None):
        report = self.generate_performance_report(ticker=ticker)
        if not report:
            print(f"\n[WARNING] No closed trades\n")
            return
        print("\n" + "="*60)
        print(f"  {report['ticker']} PERFORMANCE")
        print("="*60)
        print(f"  Total: {report['total_trades']} | Wins: {report['winning_trades']} | Loss: {report['losing_trades']}")
        print(f"  Win Rate: {report['win_rate_pct']}%")
        print(f"  Net P&L: ${report['net_pnl']:+.2f}")
        print(f"  Profit Factor: {report['profit_factor']:.2f}x")
        print(f"  Avg Win: ${report['avg_win']:.2f} | Avg Loss: ${report['avg_loss']:.2f}")
        print("\n")

if __name__ == "__main__":
    with TradeAnalytics() as analytics:
        analytics.print_report()