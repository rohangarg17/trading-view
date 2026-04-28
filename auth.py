"""
One-time script to generate an access token from Zerodha Kite Connect.

Usage:
  1. Run this script: python auth.py
  2. It will print a login URL — open it in your browser
  3. Log in with your Zerodha credentials
  4. After login, you'll be redirected to your redirect URL with a `request_token` param
  5. Copy that request_token and paste it when prompted
  6. The script will print your access_token — put it in .env
"""

import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

api_key = os.getenv("KITE_API_KEY")
api_secret = os.getenv("KITE_API_SECRET")

if not api_key or not api_secret or api_key == "your_api_key_here":
    print("ERROR: Set KITE_API_KEY and KITE_API_SECRET in .env first.")
    exit(1)

kite = KiteConnect(api_key=api_key)

print(f"\nOpen this URL in your browser to log in:\n")
print(f"  {kite.login_url()}\n")
print("After logging in, you'll be redirected. Copy the 'request_token' from the URL.\n")

request_token = input("Paste request_token here: ").strip()

data = kite.generate_session(request_token, api_secret=api_secret)
access_token = data["access_token"]

print(f"\nYour access token:\n  {access_token}\n")
print("Add this to your .env file as KITE_ACCESS_TOKEN=<token>")
print("Note: This token expires daily. Re-run this script each trading day.")
