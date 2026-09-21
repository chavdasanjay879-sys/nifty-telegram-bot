import os
import csv
import time
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd
from flask import Flask

# ============================================================
# RENDER + FLASK
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running live 24x7!"

@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot_active": bot_active,
        "trades_today": trades_count,
        "daily_limit": MAX_DAILY_TRADES
    }

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )

# ============================================================
# SETTINGS
# ============================================================

TIMEZONE = ZoneInfo("Asia/Kolkata")

MAX_DAILY_TRADES = 2

# Render Env Variable + Fallback (Zero downtime)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8797667594:AAFZRzm0KISq8z5_TLZupMMw0b3qGGREn-g").strip()
CHAT_ID = os.environ.get("CHAT_ID", "1944447859").strip()

CSV_FILE = "trade_log.csv"

RSI_PERIOD = 14
RSI_BUY_LEVEL = 60
SIGNAL_COOLDOWN_MINUTES = 15
SCAN_INTERVAL_SECONDS = 30

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

WATCHLIST = {
    "NIFTY": {
        "ticker": "^NSEI",
        "lot_size": 75,
        "step": 50
    },
    "BANKNIFTY": {
        "ticker": "^NSEBANK",
        "lot_size": 30,
        "step": 100
    },
    "SENSEX": {
        "ticker": "^BSESN",
        "lot_size": 20,
        "step": 100
    }
}

# ============================================================
# GLOBAL STATE
# ============================================================

bot_active = True
last_update_id = 0
trades_count = 0
trade_date = None
last_signal_time = {}
state_lock = threading.Lock()

# ============================================================
# TELEGRAM
# ============================================================

def send_alert(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        res = requests.post(url, data=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False

# ============================================================
# HELPERS
# ============================================================

def now_ist():
    return datetime.now(TIMEZONE)

def today_string():
    return now_ist().strftime("%Y-%m-%d")

def is_market_open():
    now = now_ist()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE

def reset_daily_counter_if_needed():
    global trades_count, trade_date, last_signal_time
    today = today_string()
    with state_lock:
        if trade_date != today:
            trade_date = today
            trades_count = 0
            last_signal_time = {}
            send_alert(
                f"🌅 New Trading Day\nDate: {today}\n"
                f"Daily limit: {MAX_DAILY_TRADES}\n"
                f"Status: {'🟢 ACTIVE' if bot_active else '🔴 PAUSED'}"
            )

def calculate_rsi(data, window=14):
    data = pd.Series(data).dropna()
    if len(data) < window + 1:
        return None
    delta = data.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1/window, adjust=False, min_periods=window).mean()
    if avg_loss.iloc[-1] == 0:
        return 100.0 if avg_gain.iloc[-1] != 0 else 50.0
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
    return float(100 - (100 / (1 + rs)))

def calculate_vwap(df):
    if df.empty or "Volume" not in df.columns:
        return None
    v_sum = df["Volume"].sum()
    if v_sum <= 0:
        return None
    return float((df["Close"] * df["Volume"]).sum() / v_sum)

def get_market_data(ticker):
    try:
        data = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=False)
        if data is None or data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        data = data.dropna(subset=["Close"])
        if len(data) < RSI_PERIOD + 2:
            return None
        return data
    except Exception as e:
        print(f"Data error for {ticker}: {e}")
        return None

def get_atm_strike(spot, step):
    return int(round(spot / step) * step)

def signal_allowed(index_name):
    now = now_ist()
    prev = last_signal_time.get(index_name)
    if prev is None:
        return True
    return (now - prev).total_seconds() / 60 >= SIGNAL_COOLDOWN_MINUTES

def analyze_index(index_name, info):
    global trades_count
    data = get_market_data(info["ticker"])
    if data is None:
        return

    try:
        current_spot = round(float(data["Close"].iloc[-1]), 2)
        vwap = calculate_vwap(data)
        rsi = calculate_rsi(data["Close"], RSI_PERIOD)
        if vwap is None or rsi is None:
            return

        vwap = round(vwap, 2)
        rsi = round(rsi, 2)

        bullish = (current_spot > vwap and rsi >= RSI_BUY_LEVEL)
        bearish = (current_spot < vwap and rsi <= 40)

        if not bullish and not bearish:
            return
        if not signal_allowed(index_name):
            return

        with state_lock:
            if trades_count >= MAX_DAILY_TRADES:
                return
            trades_count += 1
            last_signal_time[index_name] = now_ist()
            current_num = trades_count

        direction = "BULLISH" if bullish else "BEARISH"
        option_type = "CE" if bullish else "PE"
        strike = get_atm_strike(current_spot, info["step"])
        opt_title = f"{index_name} {strike} {option_type}"

        msg = (
            f"🚨 {index_name} SIGNAL\n━━━━━━━━━━━━━━━━━━\n"
            f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
            f"Spot: ₹{current_spot:,.2f}\nVWAP: ₹{vwap:,.2f}\nRSI: {rsi:.2f}\n"
            f"Direction: {direction}\nSignal: BUY {opt_title}\n"
            f"Lot Size: {info['lot_size']}\n"
            f"Trade #{current_num}/{MAX_DAILY_TRADES}\n"
            f"━━━━━━━━━━━━━━━━━━\n⚠️ PAPER SIGNAL ONLY"
        )
        send_alert(msg)
    except Exception as e:
        print(f"Analyze err: {e}")

# ============================================================
# TELEGRAM WORKER (RUNS 24x7 INDEPENDENTLY)
# ============================================================

def check_telegram_commands():
    global bot_active, last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 1}
        res = requests.get(url, params=params, timeout=5)
        if res.status_code != 200:
            return

        for update in res.json().get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            sender = str(msg.get("chat", {}).get("id", ""))

            if sender != CHAT_ID:
                continue

            if text == "/start":
                bot_active = True
                send_alert("🟢 BOT STARTED\nScanner is ACTIVE.")
            elif text == "/stop":
                bot_active = False
                send_alert("🔴 BOT STOPPED\nScanner is PAUSED.")
            elif text == "/status":
                status = "🟢 ACTIVE" if bot_active else "🔴 PAUSED"
                mkt = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
                send_alert(
                    f"📊 BOT STATUS\n━━━━━━━━━━━━━━\n"
                    f"Bot: {status}\nMarket: {mkt}\nDate: {today_string()}\n"
                    f"Signals: {trades_count}/{MAX_DAILY_TRADES}\n"
                    f"Watchlist: NIFTY | BANKNIFTY | SENSEX"
                )
            elif text == "/help":
                send_alert("🤖 COMMANDS:\n/status - Check status\n/start - Start scanner\n/stop - Pause\n/help - Help")
    except Exception:
        pass

def telegram_loop():
    while True:
        check_telegram_commands()
        time.sleep(2)

def main_trading_loop():
    global trade_date
    trade_date = today_string()
    send_alert("🚀 MULTI-INDEX SCANNER ONLINE\nCommands: /status, /start, /stop")

    while True:
        try:
            reset_daily_counter_if_needed()
            if not bot_active or not is_market_open() or trades_count >= MAX_DAILY_TRADES:
                time.sleep(30)
                continue

            for name, info in WATCHLIST.items():
                if trades_count >= MAX_DAILY_TRADES or not bot_active or not is_market_open():
                    break
                analyze_index(name, info)
                time.sleep(3)

            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Main loop err: {e}")
            time.sleep(10)

# ============================================================
# MAIN EXECUTION
# ============================================================

if __name__ == "__main__":
    # 1. Telegram Listener Thread (આ ક્યારેય ઊંઘશે નહીં)
    t_thread = threading.Thread(target=telegram_loop, daemon=True)
    t_thread.start()

    # 2. Market Scanner Thread (માર્કેટ સમયે સ્કેન કરશે)
    scanner_thread = threading.Thread(target=main_trading_loop, daemon=True)
    scanner_thread.start()

    # 3. Web keep-alive server (UptimeRobot માટે)
    run_web()
