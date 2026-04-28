#!/bin/bash
set -e

echo "Setting up Nifty Options Dashboard..."

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo ""
echo "Installing Chromium for Playwright (needed for NSE option-chain scraping)..."
playwright install chromium

echo ""
echo "Setup complete! Next steps:"
echo "  1. Run: source venv/bin/activate"
echo "  2. Run: python app.py"
echo "  3. Open: http://localhost:5050"
echo ""
echo "Data source is set to 'nse' (free) by default."
echo "Option chain data is only available during market hours (Mon-Fri, 9 AM - 3:30 PM IST)."
echo "To switch to Zerodha Kite, edit .env and set DATA_SOURCE=kite with your API credentials."
