"""Quick test to verify Alpaca API connection with credentials from .env"""
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_PAPER_API_KEY")
SECRET_KEY = os.getenv("ALPACA_PAPER_SECRET_KEY")
API_ENDPOINT = os.getenv("ALPACA_PAPER_API_ENDPOINT", "https://paper-api.alpaca.markets")

print(f"API Key found: {'Yes' if API_KEY else 'No'}")
print(f"Secret Key found: {'Yes' if SECRET_KEY else 'No'}")
print(f"Endpoint: {API_ENDPOINT}")

try:
    from alpaca.trading.client import TradingClient
    # The raw=True parameter is needed for the newer alpaca-py versions with paper endpoint
    client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    account = client.get_account()
    print(f"\nSUCCESS - Connected to Alpaca (Paper Trading)")
    print(f"   Account ID: {account.id}")
    print(f"   Status: {account.status}")
    print(f"   Buying Power: ${float(account.buying_power):,.2f}")
    print(f"   Portfolio Value: ${float(account.portfolio_value):,.2f}")
    print(f"   Cash: ${float(account.cash):,.2f}")
except Exception as e:
    print(f"\nFAILED - Connection error: {e}")