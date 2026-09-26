import os
import csv
import time
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd
from flask import Flask

# ============================================================
# WEB SERVER FOR RENDER 24x7
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Single-Direction Sentiment Buying Engine Live 24x7!"

@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot_active": bot_active,
        "trades_today": trades_count,
        "daily_limit": MAX_DAILY_TRADES,
        "total_pnl": total_pnl
    }

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============================================================
# CONFIGURATION
# ============================================================

TIMEZONE = ZoneInfo("Asia/Kolkata")
MAX_DAILY_TRADES = 2  # Best 1 or 2 high probability quality setups only

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8797667594:AAFZRzm0KISq8z5_TLZupMMw0b3qGGREn-g").strip()
CHAT_ID = os.environ.get("CHAT_ID", "1944447859").strip()
CSV_FILE = "trade_log.csv"

# Sentiment Parameters
EMA_TREND = 50
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ADX_PERIOD = 14
MIN_ADX_TREND = 25.0  # Strict: Flat market completely blocked

SIGNAL_COOLDOWN_MINUTES = 30
SCAN_INTERVAL_SECONDS = 15

# Safe Trading Timing (Opening 30 min traps avoided)
TRADE_START_TIME = dt_time(9, 45)
TRADE_END_TIME = dt_time(14, 45)

WATCHLIST = {
    "NIFTY": {
        "symbol": "NIFTY 50",
        "lot_size": 75,
        "step": 50,
        "target_pts": 50,
        "initial_sl_pts": 16,
        "trail_trigger_pts": 20
    },
    "BANKNIFTY": {
        "symbol": "NIFTY BANK",
        "lot_size": 30,
        "step": 100,
        "target_pts": 100,
        "initial_sl_pts": 35,
        "trail_trigger_pts": 40
    }
}

# ============================================================
# GLOBAL ENGINE STATE
# ============================================================

bot_active = True
last_update_id = 0
trades_count = 0
trade_date = None
last_signal_time = {}
total_pnl = 0.0

active_positions = {}
pending_confirmations = {}
price_history = {"NIFTY": [], "BANKNIFTY": []}

state_lock = threading.Lock()

# ============================================================
# TELEGRAM ALERTS
# ============================================================

def send_alert(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        res = requests.post(url, data=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ============================================================
# NSE REAL-TIME DATA
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/"
})

def init_nse_session():
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass

init_nse_session()

def get_nse_live_prices():
    url = "https://www.nseindia.com/api/allIndices"
    try:
        res = session.get(url, timeout=10)
        if res.status_code in (401, 403):
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
        print(f"NSE feed err: {e}")
    return None

# ============================================================
# HELPERS & FORMULAS
# ============================================================

def now_ist():
    return datetime.now(TIMEZONE)

def today_string():
    return now_ist().strftime("%Y-%m-%d")

def is_market_open():
    now = now_ist()
    if now.weekday() >= 5:
        return False
    return TRADE_START_TIME <= now.time() <= TRADE_END_TIME

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

def calculate_adx(series, period=14):
    if len(series) < period * 2:
        return 0.0
    s = pd.Series(series)
    diff = s.diff()
    pos_dm = diff.clip(lower=0)
    neg_dm = (-diff).clip(lower=0)
    tr = diff.abs()
    atr = tr.ewm(span=period, adjust=False).mean()
    pos_di = 100 * (pos_dm.ewm(span=period, adjust=False).mean() / (atr + 1e-6))
    neg_di = 100 * (neg_dm.ewm(span=period, adjust=False).mean() / (atr + 1e-6))
    dx = 100 * (pos_di - neg_di).abs() / (pos_di + neg_di + 1e-6)
    adx = dx.ewm(span=period, adjust=False).mean().iloc[-1]
    return float(adx)

def get_atm_strike(spot, step):
    return int(round(spot / step) * step)

def reset_daily_counter_if_needed():
    global trades_count, trade_date, last_signal_time, price_history, total_pnl, active_positions, pending_confirmations
    today = today_string()
    with state_lock:
        if trade_date != today:
            trade_date = today
            trades_count = 0
            total_pnl = 0.0
            last_signal_time = {}
            active_positions = {}
            pending_confirmations = {}
            price_history = {"NIFTY": [], "BANKNIFTY": []}
            send_alert(f"🌅 New Trading Day: {today}\nSystem: Single-Direction Sentiment Engine\nLimit: {MAX_DAILY_TRADES} High-Probability Trades")

# ============================================================
# LIVE POSITION TRACKER & TRAILING SL ENGINE
# ============================================================

def track_open_positions(rates):
    global total_pnl
    with state_lock:
        closed_indices = []
        for index_name, pos in active_positions.items():
            current_spot = rates.get(index_name)
            if not current_spot:
                continue

            hit_target = False
            hit_sl = False

            if pos["direction"] == "BULLISH":  # CE
                gain_pts = current_spot - pos["entry_spot"]
                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(pos["entry_spot"] + (gain_pts - pos["trail_trigger_pts"]) * 0.6, 2)
                    if new_sl > pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True
                        send_alert(f"🛡️ TRAILING SL SHIFTED\n{index_name} CE Lock Level: ₹{pos['sl_price']:,.2f}")

                if current_spot >= pos["target_price"]:
                    hit_target = True
                elif current_spot <= pos["sl_price"]:
                    hit_sl = True

            else:  # PE
                gain_pts = pos["entry_spot"] - current_spot
                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(pos["entry_spot"] - (gain_pts - pos["trail_trigger_pts"]) * 0.6, 2)
                    if new_sl < pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True
                        send_alert(f"🛡️ TRAILING SL SHIFTED\n{index_name} PE Lock Level: ₹{pos['sl_price']:,.2f}")

                if current_spot <= pos["target_price"]:
                    hit_target = True
                elif current_spot >= pos["sl_price"]:
                    hit_sl = True

            if hit_target:
                pnl = pos["target_pts"] * pos["lot_size"]
                total_pnl += pnl
                msg = (
                    f"🎯 **TARGET ACHIEVED!** 🏆\n"
                    f"Asset: {index_name} | {pos['option_name']}\n"
                    f"Entry: ₹{pos['entry_spot']:,.2f} ➔ Exit: ₹{current_spot:,.2f}\n"
                    f"Gain: +{pos['target_pts']} pts\n"
                    f"💵 Trade Profit: +₹{pnl:,.2f}\n"
                    f"Total P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}"
                )
                send_alert(msg)
                closed_indices.append(index_name)

            elif hit_sl:
                exit_diff = (current_spot - pos["entry_spot"]) if pos["direction"] == "BULLISH" else (pos["entry_spot"] - current_spot)
                pnl = exit_diff * pos["lot_size"]
                total_pnl += pnl
                is_lock = pos.get("is_trailed") and pnl >= 0
                msg = (
                    f"{'🛡️ PROFIT PROTECTED EXIT' if is_lock else '🛑 STRICT STOPLOSS EXIT'}\n"
                    f"Asset: {index_name} | {pos['option_name']}\n"
                    f"Entry: ₹{pos['entry_spot']:,.2f} ➔ Exit: ₹{current_spot:,.2f}\n"
                    f"Points: {exit_diff:+.2f} pts\n"
                    f"P&L: {'+' if pnl >= 0 else '-'}₹{abs(pnl):,.2f}\n"
                    f"Total P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}"
                )
                send_alert(msg)
                closed_indices.append(index_name)

        for idx in closed_indices:
            del active_positions[idx]

# ============================================================
# SENTIMENT PREDICTION & CONFIRMATION ENGINE
# ============================================================

def process_pending_confirmations(rates):
    global trades_count
    with state_lock:
        confirmed = []
        for index_name, p in pending_confirmations.items():
            spot = rates.get(index_name)
            if not spot or trades_count >= MAX_DAILY_TRADES:
                continue

            is_bull = (p["direction"] == "BULLISH" and spot >= p["confirm_level"])
            is_bear = (p["direction"] == "BEARISH" and spot <= p["confirm_level"])

            if is_bull or is_bear:
                trades_count += 1
                last_signal_time[index_name] = now_ist()

                target_pts = p["target_pts"]
                sl_pts = p["sl_pts"]
                target_price = round(spot + target_pts if is_bull else spot - target_pts, 2)
                sl_price = round(spot - sl_pts if is_bull else spot + sl_pts, 2)

                active_positions[index_name] = {
                    "asset": index_name,
                    "option_name": p["option_name"],
                    "direction": p["direction"],
                    "entry_spot": spot,
                    "target_price": target_price,
                    "sl_price": sl_price,
                    "target_pts": target_pts,
                    "sl_pts": sl_pts,
                    "trail_trigger_pts": p["trail_trigger_pts"],
                    "lot_size": p["lot_size"],
                    "date": today_string(),
                    "entry_time": now_ist().strftime("%H:%M:%S"),
                    "is_trailed": False
                }

                alert = (
                    f"⚡ **HIGH PROBABILITY ORDER EXECUTED**\n"
                    f"Asset: {index_name}\n"
                    f"Action: **BUY {p['option_name']}** (Single Direction)\n"
                    f"Entry Spot: ₹{spot:,.2f}\n"
                    f"Sentiment: {p['sentiment']} | ADX: {p['adx']:.1f}\n"
                    f"🎯 Target: ₹{target_price:,.2f} (+{target_pts} pts)\n"
                    f"🛑 Stoploss: ₹{sl_price:,.2f} (-{sl_pts} pts)\n"
                    f"Trade #{trades_count}/{MAX_DAILY_TRADES}"
                )
                send_alert(alert)
                confirmed.append(index_name)

        for idx in confirmed:
            del pending_confirmations[idx]

def analyze_index(index_name, current_spot, info):
    history = price_history[index_name]
    history.append(current_spot)
    if len(history) > 120:
        history.pop(0)

    if len(history) < EMA_TREND + 5:
        return

    with state_lock:
        if index_name in active_positions or index_name in pending_confirmations or trades_count >= MAX_DAILY_TRADES:
            return

    # 1. ADX Strict Trend Filter (Must be > 25)
    adx_val = calculate_adx(history, ADX_PERIOD)
    if adx_val < MIN_ADX_TREND:
        return

    s = pd.Series(history)
    ema_trend = float(s.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
    ema_fast = float(s.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1])
    ema_slow = float(s.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])
    rsi_val = calculate_rsi(history, RSI_PERIOD)
    if rsi_val is None:
        return

    recent_high = max(history[-6:])
    recent_low = min(history[-6:])

    # STRICT SENTIMENT ALIGNMENT (No Both-Side Buying)
    # ONLY CE: Price > 50 EMA AND 9 EMA > 21 EMA AND RSI Healthy (58 - 68)
    bullish_condition = (current_spot > ema_trend) and (ema_fast > ema_slow) and (58 <= rsi_val <= 68)

    # ONLY PE: Price < 50 EMA AND 9 EMA < 21 EMA AND RSI Healthy (32 - 42)
    bearish_condition = (current_spot < ema_trend) and (ema_fast < ema_slow) and (32 <= rsi_val <= 42)

    if not bullish_condition and not bearish_condition:
        return

    prev = last_signal_time.get(index_name)
    if prev and (now_ist() - prev).total_seconds() / 60 < SIGNAL_COOLDOWN_MINUTES:
        return

    atm_strike = get_atm_strike(current_spot, info["step"])

    if bullish_condition:
        direction = "BULLISH"
        sentiment = "STRONG BULL REGIME"
        opt_name = f"{index_name} {atm_strike} CE"
        confirm_level = round(recent_high + 2.5, 2)
    else:
        direction = "BEARISH"
        sentiment = "STRONG BEAR REGIME"
        opt_name = f"{index_name} {atm_strike} PE"
        confirm_level = round(recent_low - 2.5, 2)

    with state_lock:
        pending_confirmations[index_name] = {
            "direction": direction,
            "sentiment": sentiment,
            "option_name": opt_name,
            "confirm_level": confirm_level,
            "target_pts": info["target_pts"],
            "sl_pts": info["initial_sl_pts"],
            "trail_trigger_pts": info["trail_trigger_pts"],
            "lot_size": info["lot_size"],
            "adx": adx_val
        }

    send_alert(
        f"🔍 **HIGH PROBABILITY SETUP DETECTED**\n"
        f"Asset: {index_name}\n"
        f"Candidate: **BUY {opt_name} ONLY**\n"
        f"Sentiment: {sentiment}\n"
        f"Trigger Level: ₹{confirm_level:,.2f}\n"
        f"ADX: {adx_val:.1f} | RSI: {rsi_val:.1f}\n"
        f"Waiting for breakout confirmation..."
    )

# ============================================================
# COMMANDS & MAIN LOOP
# ============================================================

def check_telegram_commands():
    global bot_active, last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        res = requests.get(url, params={"offset": last_update_id + 1, "timeout": 1}, timeout=5)
        if res.status_code != 200:
            return

        for update in res.json().get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            sender = str(msg.get("chat", {}).get("id", ""))
            if sender != CHAT_ID:
                continue

            if text == "/pnl":
                send_alert(f"💰 **P&L SUMMARY**\nTrades: {trades_count}/{MAX_DAILY_TRADES}\nRealized P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}")
            elif text == "/status":
                open_str = "\n".join([f"• {k}: {v['option_name']} (SL: ₹{v['sl_price']})" for k, v in active_positions.items()]) or "None"
                send_alert(f"📊 BOT: {'ACTIVE' if bot_active else 'PAUSED'}\nTrades: {trades_count}/{MAX_DAILY_TRADES}\nOpen: {open_str}\nP&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}")
    except Exception:
        pass

def telegram_loop():
    while True:
        check_telegram_commands()
        time.sleep(2)

def main_trading_loop():
    global trade_date
    trade_date = today_string()
    send_alert("🚀 SINGLE-DIRECTION HIGH CONFIDENCE ENGINE ONLINE\n• Macro 50 EMA Alignment\n• ADX 25+ Trend Filter\n• Single Side Buying Only (No Straddles)")

    while True:
        try:
            reset_daily_counter_if_needed()
            if not bot_active or not is_market_open():
                time.sleep(20)
                continue

            rates = get_nse_live_prices()
            if rates:
                track_open_positions(rates)
                process_pending_confirmations(rates)
                if trades_count < MAX_DAILY_TRADES:
                    for name, info in WATCHLIST.items():
                        if name in rates:
                            analyze_index(name, rates[name], info)

            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Main loop err: {e}")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=main_trading_loop, daemon=True).start()
    run_web()
