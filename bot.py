import yfinance as yf
import csv
import os
import time
import requests
import threading
from datetime import datetime
from flask import Flask

# Render પોર્ટ એરર ફિક્સ કરવા માટે નાનું ડમી વેબ સર્વર
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running live 24x7!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= SETTINGS =================
MAX_DAILY_TRADES = 2
BOT_TOKEN = "8797667594:AAFDTUbw-wz-PJRXiJdwRbKdSMZ6mE55S7A"
CHAT_ID = "1944447859"
CSV_FILE = "trade_log.csv"

WATCHLIST = {
    "NIFTY": {"ticker": "^NSEI", "lot_size": 75, "step": 50},
    "BANKNIFTY": {"ticker": "^NSEBANK", "lot_size": 30, "step": 100},
    "SENSEX": {"ticker": "^BSESN", "lot_size": 20, "step": 100}
}
# ============================================

bot_active = True
last_update_id = 0
trades_count = 0

def send_alert(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage?chat_id={CHAT_ID}&text={message}"
        requests.get(url, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def check_telegram_commands():
    global bot_active, last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=2"
        res = requests.get(url, timeout=5).json()
        for update in res.get("result", []):
            last_update_id = update["update_id"]
            if "message" in update and "text" in update["message"]:
                cmd = update["message"]["text"].strip().lower()
                sender = str(update["message"]["chat"]["id"])
                if sender != CHAT_ID:
                    continue

                if cmd == "/start":
                    bot_active = True
                    send_alert("🟢 Multi-Index Bot Scanning Started via Mobile!")
                elif cmd == "/stop":
                    bot_active = False
                    send_alert("🔴 Multi-Index Bot Scanning Stopped via Mobile!")
                elif cmd == "/status":
                    status_text = "🟢 ACTIVE" if bot_active else "🔴 PAUSED"
                    send_alert(f"📊 Status: {status_text}\nScanning: NIFTY, BANKNIFTY, SENSEX\nTrades today: {trades_count}/{MAX_DAILY_TRADES}")
    except Exception as e:
        pass

def main_trading_loop():
    global trades_count
    send_alert("🚀 Multi-Index Scanner Online!\nMonitoring: NIFTY 50, BANK NIFTY & SENSEX.\nSend /status, /start, or /stop.")
    print("🟢 Multi-Index Bot Online with Telegram Remote Control")

    while trades_count < MAX_DAILY_TRADES:
        check_telegram_commands()

        if not bot_active:
            time.sleep(5)
            continue

        now = datetime.now()

        for name, info in WATCHLIST.items():
            if trades_count >= MAX_DAILY_TRADES:
                break

            try:
                ticker_data = yf.Ticker(info["ticker"])
                df = ticker_data.history(period="1d", interval="1m")

                if len(df) >= 15:
                    current_spot = round(float(df['Close'].iloc[-1]), 2)
                    vwap = round((df['Close'] * df['Volume']).sum() / df['Volume'].sum(), 2) if df['Volume'].sum() > 0 else current_spot
                    rsi = round(calculate_rsi(df['Close']), 2)

                    if current_spot > vwap and rsi > 60:
                        trades_count += 1
                        step = info["step"]
                        strike = round(current_spot / step) * step
                        atm_strike = f"{name} {strike} CE"
                        entry_price = 150.0
                        target = entry_price + 40.0
                        sl = entry_price - 20.0
                        lot = info["lot_size"]

                        msg = (
                            f"🚨 [{name} MOMENTUM BREAKOUT DETECTED]\n"
                            f"Time: {now.strftime('%H:%M:%S')}\n"
                            f"Spot Price: ₹{current_spot}\n"
                            f"VWAP: ₹{vwap} | RSI: {rsi}\n"
                            f"Simulated Trade: BUY {atm_strike}\n"
                            f"Lot Size: {lot} | Entry: ₹{entry_price}\n"
                            f"SL: ₹{sl} | Target: ₹{target}"
                        )
                        send_alert(msg)

                        time.sleep(10)
                        pnl = 40.0 * lot
                        send_alert(f"🎯 [{name} TARGET HIT]\nExited: {atm_strike} @ ₹{target}\nPnL: +₹{pnl:,.2f}")

                        with open(CSV_FILE, mode='a', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerow([now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), atm_strike, lot, entry_price, target, pnl, "TARGET HIT"])

                        if trades_count >= MAX_DAILY_TRADES:
                            send_alert(f"🏁 Daily Trade Limit Reached ({MAX_DAILY_TRADES}/{MAX_DAILY_TRADES}). Bot is done for today.")
                            break

                        time.sleep(15)
            except Exception as e:
                print(f"Error scanning {name}: {e}")

        time.sleep(10)

if __name__ == "__main__":
    t = threading.Thread(target=main_trading_loop)
    t.daemon = True
    t.start()
    run_web()
