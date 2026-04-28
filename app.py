import os
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DATA_SOURCE = os.getenv("DATA_SOURCE", "nse").lower()


# --- Live Dashboard ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def get_data():
    try:
        if DATA_SOURCE == "kite":
            from kite_provider import fetch_data
        else:
            from nse_provider import fetch_data
        return jsonify(fetch_data())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/source")
def get_source():
    return jsonify({"source": DATA_SOURCE})


# --- Guide ---

@app.route("/guide")
def guide_page():
    return render_template("guide.html")


# --- Stock details ---

@app.route("/stock/<symbol>")
def stock_detail_page(symbol):
    return render_template("stock_detail.html", symbol=symbol.upper())


@app.route("/api/stock/<symbol>")
def stock_detail_api(symbol):
    try:
        from stock_detail import fetch_details
        return jsonify(fetch_details(symbol.upper()))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# --- Backtesting ---

@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/strategies")
def list_strategies():
    from backtester import get_strategy_info
    return jsonify(get_strategy_info())


@app.route("/api/backtest", methods=["POST"])
def run_backtest_api():
    try:
        from backtester import run_backtest
        body = request.get_json()
        symbol = body.get("symbol", "RELIANCE")
        strategy_name = body.get("strategy", "collar")
        params = body.get("params", {})
        period = body.get("period", "2y")

        result = run_backtest(symbol, strategy_name, params, period)
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _maybe_start_nse_refresh():
    """Kick off the NSE Playwright refresh thread once per process.

    Flask's debug reloader runs this file twice (parent watches, child serves).
    We only want the background browser in the child, so check WERKZEUG_RUN_MAIN.
    """
    if DATA_SOURCE != "nse":
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    from nse_provider import start_background_refresh
    start_background_refresh()


if __name__ == "__main__":
    _maybe_start_nse_refresh()
    app.run(debug=True, port=5050)
