# UpGainPulse Configuration

## Account Setup

- **Platform**: Alpaca Paper Trading
- **Capital**: $500.00
- **Risk per Trade**: $50.00 (10% of account)
- **Stop Loss**: 10 cents
- **Risk/Reward Ratio**: 2.0 (for every $1 risk, aim for $2 gain)

## Multi-Ticker Portfolio

| Ticker | Type | Rationale |
|--------|------|----------|
| SPY | Large-cap ETF | Broad market |
| QQQ | Tech ETF | Tech exposure |
| IWM | Mid-cap ETF | Mid-cap exposure |
| AAPL | Mega-cap tech | High liquidity |
| MSFT | Mega-cap cloud | High liquidity |

## Position Sizing Formula

```
Qty = (Risk $ / Stop Loss $) / Current Price * Multiplier
Qty = (50 / 0.10) / Price * 1.0
Qty = 500 / Price

Example: SPY @ $500
  Qty = 500 / 500 = 1 share
  TP = $502.00 (1c gain × 2.0 RR)
  SL = $499.90 (10c below entry)
  P&L if TP hit = $2.00 (1 share × $2)
  P&L if SL hit = -$0.10 (1 share × -10c)
```

## Melbourne Time Schedule

When you wake up in Melbourne:
- **4:00-7:00 AM**: Last 3 hours of US market (live monitoring possible)
- **7:00+ AM**: Market closed, trades filled/pending
- **Check analytics at 7:00 AM** to see overnight fills

## Data to Track

After 1-2 weeks:
- Win rate %
- Average win/loss
- Profit factor
- Max drawdown
- Total P&L

Use this data to optimize:
- Stop loss size
- Risk per trade
- Which tickers to trade
- Best time of day for ORB
