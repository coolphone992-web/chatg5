# UpGainPulse v2.0

**Multi-Ticker Headless ORB Bot for Paper Trading**

## Overview
- 5 tickers (SPY, QQQ, IWM, AAPL, MSFT) running in parallel
- $500 account capital | $50 per trade (10% risk)
- 10¢ stop loss | 2:1 risk/reward ratio
- Runs headless overnight (Melbourne time)
- Bracket orders (TP + SL automatically)

## Installation

```bash
git clone https://github.com/coolphone992-web/Test-4.git
cd Test-4
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

## Run Bot

```bash
python upgainpulse.py
```

## Check Results

```bash
python analytics.py
```

## Time Zones

- **US Market**: 9:30 AM - 4:00 PM ET
- **ORB Range**: 9:30-9:45 AM ET (locked)
- **Breakout Hunt**: 9:45 AM - 4:00 PM ET
- **Melbourne Time**: 12:30 AM - 7:00 AM AEST (when bot trades)

## Fixed Issues

✅ Timezone handling (UTC → ET conversion)
✅ Position sizing formula
✅ Capital validation before orders
✅ Daily state reset (multi-day trading)
✅ Exit price tracking
✅ WebSocket reconnection
✅ Thread-safe database

## Strategy

1. **9:30-9:45 AM ET**: Build range (high/low)
2. **After 9:45 AM ET**: Lock range, hunt breakout
3. **On breakout**: Send bracket order (1 per ticker max/day)
4. **Exit**: SL or TP (auto-managed by Alpaca)

## Paper Account Only

This is for testing/validation only. No real money is at risk.
