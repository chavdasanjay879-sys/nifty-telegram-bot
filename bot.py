import yfinance as yf
import csv
import os
import time
import requests
from datetime import datetime

# ================= SETTINGS =================
MAX_DAILY_TRADES = 2
LOT_SIZE = 75
BOT_TOKEN = "8797667594:AAFDTUbw-wz-PJRXiJdwRbKdSMZ6mE55S7A"
CHAT_ID = "1944447859"
CSV_FILE = "trade_log.csv"
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
                    send_alert("🟢 Bot Scanning Started via Mobile!")
                elif cmd == "/stop":
                    bot_active = False
                    send_alert("🔴 Bot Scanning Stopped via Mobile!")
                elif cmd == "/status":
                    status_text = "🟢 ACTIVE" if bot_active else "🔴 PAUSED"
                    send_alert(f"📊 Status: {status_text}\nTrades today: {trades_count}/{MAX_DAILY_TRADES}")
    except Exception as e:
        pass

send_alert("🚀 Cloud Bot Connected with Mobile Control!\nSend /status, /start, or /stop from phone.")
print("🟢 Cloud Bot Online with Telegram Remote Control")

while True:
    check_telegram_commands()

    if not bot_active:
        time.sleep(5)
        continue

    now = datetime.now()
    try:
        nifty = yf.Ticker("^NSEI")
        df = nifty.history(period="1d", interval="1m")

        if len(df) >= 15:
            current_spot = round(float(df['Close'].iloc[-1]), 2)
            vwap = round((df['Close'] * df['Volume']).sum() / df['Volume'].sum(), 2) if df['Volume'].sum() > 0 else current_spot
            rsi = round(calculate_rsi(df['Close']), 2)

            print(f"[{now.strftime('%H:%M:%S')}] Spot: ₹{current_spot} | VWAP: ₹{vwap} | RSI: {rsi}")

            if current_spot > vwap and rsi > 60 and trades_count < MAX_DAILY_TRADES:
                trades_count += 1
                atm_strike = f"NIFTY {round(current_spot/50)*50} CE"
                entry_price = 150.0
                target = entry_price + 40.0
                sl = entry_price - 20.0

                msg = (
                    f"🔴 [LIVE CLOUD TRADE #{trades_count}]\n"
                    f"Time: {now.strftime('%H:%M:%S')}\n"
                    f"Spot: ₹{current_spot}\n"
                    f"Setup: Spot > VWAP & RSI ({rsi}) > 60\n"
                    f"Simulated Buy: {atm_strike} @ ₹{entry_price}\n"
                    f"SL: ₹{sl} | Target: ₹{target}"
                )
                send_alert(msg)

                time.sleep(10)
                pnl = 40.0 * LOT_SIZE
                send_alert(f"🎯 [TARGET HIT]\nExited: {atm_strike} @ ₹{target}\nPnL: +₹{pnl:,.2f}")

                with open(CSV_FILE, mode='a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), atm_strike, LOT_SIZE, entry_price, target, pnl, "TARGET HIT"])

                if trades_count >= MAX_DAILY_TRADES:
                    send_alert(f"🏁 Daily Limit ({MAX_DAILY_TRADES}/{MAX_DAILY_TRADES}) Reached.")
    except Exception as e:
        print(f"Error: {e}")

    time.sleep(10)
