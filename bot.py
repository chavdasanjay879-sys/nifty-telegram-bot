import os
import csv
import time
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from flask import Flask

# ============================================================
# RENDER + FLASK
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running live 24x7 with Real-time NSE Data!"

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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8797667594:AAFZRzm0KISq8z5_TLZupMMw0b3qGGREn-g").strip()
CHAT_ID = os.environ.get("CHAT_ID", "1944447859").strip()

CSV_FILE = "trade_log.csv"

RSI_PERIOD = 14
RSI_BULLISH = 55
RSI_BEARISH = 45
EMA_FAST = 9
EMA_SLOW = 21

SIGNAL_COOLDOWN_MINUTES = 15
SCAN_INTERVAL_SECONDS = 15

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

WATCHLIST = {
    "NIFTY": {
        "symbol": "NIFTY 50",
        "lot_size": 75,
        "step": 50
    },
    "BANKNIFTY": {
        "symbol": "NIFTY BANK",
        "lot_size": 30,
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
price_history = {"NIFTY": [], "BANKNIFTY": []}

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
# REAL-TIME NSE LIVE DATA FETCHER
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
})

def init_nse_session():
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"NSE session init error: {e}")

init_nse_session()

def get_nse_live_prices():
    url = "https://www.nseindia.com/api/allIndices"
    try:
        res = session.get(url, timeout=10)
        if res.status_code == 401 or res.status_code == 403:
            init_nse_session()
            res = session.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json().get("data", [])
            rates = {}
            for item in data:
                if item.get("index") == "NIFTY 50":
                    rates["NIFTY"] = float(str(item.get("last", "0")).replace(",", ""))
                elif item.get("index") == "NIFTY BANK":
                    rates["BANKNIFTY"] = float(str(item.get("last", "0")).replace(",", ""))
            return rates
    except Exception as e:
        print(f"Live data fetch error: {e}")
    return None

# ============================================================
# INDICATORS & HELPERS
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
    global trades_count, trade_date, last_signal_time, price_history
    today = today_string()
    with state_lock:
        if trade_date != today:
            trade_date = today
            trades_count = 0
            last_signal_time = {}
            price_history = {"NIFTY": [], "BANKNIFTY": []}
            send_alert(
                f"🌅 New Trading Day: {today}\n"
                f"Daily Limit: {MAX_DAILY_TRADES}\n"
                f"Engine: 100% Free NSE Real-Time Feed"
            )

def calculate_rsi(series, window=14):
    if len(series) < window + 1:
        return None
    s = pd.Series(series)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1/window, adjust=False, min_periods=window).mean()
    if avg_loss.iloc[-1] == 0:
        return 100.0 if avg_gain.iloc[-1] != 0 else 50.0
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
    return float(100 - (100 / (1 + rs)))

def get_atm_strike(spot, step):
    return int(round(spot / step) * step)

def signal_allowed(index_name):
    now = now_ist()
    prev = last_signal_time.get(index_name)
    if prev is None:
        return True
    return (now - prev).total_seconds() / 60 >= SIGNAL_COOLDOWN_MINUTES

# ============================================================
# ANALYSIS LOGIC
# ============================================================

def analyze_index(index_name, current_spot, info):
    global trades_count
    history = price_history[index_name]
    history.append(current_spot)
    if len(history) > 100:
        history.pop(0)

    if len(history) < EMA_SLOW:
        return

    s = pd.Series(history)
    ema_fast = round(float(s.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]), 2)
    ema_slow = round(float(s.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]), 2)
    rsi_val = calculate_rsi(history, RSI_PERIOD)
    rsi = round(rsi_val, 2) if rsi_val is not None else 50.0

    bullish = (current_spot > ema_fast > ema_slow and rsi >= RSI_BULLISH)
    bearish = (current_spot < ema_fast < ema_slow and rsi <= RSI_BEARISH)

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

    direction = "BULLISH 🚀" if bullish else "BEARISH 🔻"
    option_type = "CE" if bullish else "PE"
    strike = get_atm_strike(current_spot, info["step"])
    opt_title = f"{index_name} {strike} {option_type}"

    target_pts = 40 if index_name == "NIFTY" else 80
    sl_pts = 20 if index_name == "NIFTY" else 40

    msg = (
        f"🎯 REAL-TIME TRADE ALERT\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {index_name}\n"
        f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
        f"Action: BUY {opt_title}\n"
        f"Live Spot: ₹{current_spot:,.2f}\n"
        f"9 EMA: ₹{ema_fast:,.2f} | 21 EMA: ₹{ema_slow:,.2f}\n"
        f"RSI (14): {rsi}\n\n"
        f"🎯 Target Est: +{target_pts} pts\n"
        f"🛑 Stoploss Est: -{sl_pts} pts\n"
        f"Trade #{current_num}/{MAX_DAILY_TRADES}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Source: Live NSE Feed"
    )
    send_alert(msg)

# ============================================================
# TELEGRAM WORKER THREAD
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
                    f"Feed: Live NSE Real-Time Feed"
                )
            elif text == "/test":
                rates = get_nse_live_prices()
                n_price = rates.get("NIFTY", "N/A") if rates else "N/A"
                send_alert(f"⚡ LIVE NSE TEST\nNIFTY 50: ₹{n_price}\nEngine is live & ready!")
            elif text == "/help":
                send_alert("🤖 COMMANDS:\n/status - Check status\n/test - Test live price\n/start - Resume\n/stop - Pause")
    except Exception:
        pass

def telegram_loop():
    while True:
        check_telegram_commands()
        time.sleep(2)

def main_trading_loop():
    global trade_date
    trade_date = today_string()
    send_alert("🚀 LIVE NSE SCANNER ACTIVATED\nFetching real-time ticks directly from NSE.")

    while True:
        try:
            reset_daily_counter_if_needed()
            if not bot_active or not is_market_open() or trades_count >= MAX_DAILY_TRADES:
                time.sleep(30)
                continue

            rates = get_nse_live_prices()
            if rates:
                for name, info in WATCHLIST.items():
                    if name in rates:
                        analyze_index(name, rates[name], info)

            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Main loop err: {e}")
            time.sleep(10)

# ============================================================
# MAIN EXECUTION
# ============================================================

if __name__ == "__main__":
    t_thread = threading.Thread(target=telegram_loop, daemon=True)
    t_thread.start()

    scanner_thread = threading.Thread(target=main_trading_loop, daemon=True)
    scanner_thread.start()

    run_web()
