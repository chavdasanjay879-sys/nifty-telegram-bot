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
# RENDER + FLASK WEB SERVER
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running live 24x7 with ADX Sideways Filter & Two-Direction Hedging Engine!"

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
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )

# ============================================================
# CONFIGURATION & PARAMETERS
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
ADX_PERIOD = 14
MIN_ADX_TREND = 20.0  # ADX < 20 means Choppy / Sideways market (No Trade Zone)

SIGNAL_COOLDOWN_MINUTES = 15
SCAN_INTERVAL_SECONDS = 15

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

WATCHLIST = {
    "NIFTY": {
        "symbol": "NIFTY 50",
        "lot_size": 75,
        "step": 50,
        "target_pts": 45,
        "initial_sl_pts": 18,
        "trail_trigger_pts": 20,
        "hedge_offset": 300
    },
    "BANKNIFTY": {
        "symbol": "NIFTY BANK",
        "lot_size": 30,
        "step": 100,
        "target_pts": 90,
        "initial_sl_pts": 35,
        "trail_trigger_pts": 40,
        "hedge_offset": 600
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
total_pnl = 0.0

active_positions = {}
pending_confirmations = {}
price_history = {"NIFTY": [], "BANKNIFTY": []}

state_lock = threading.Lock()

# ============================================================
# TELEGRAM NOTIFICATIONS
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
        print(f"Live data fetch error: {e}")
    return None

# ============================================================
# HELPERS & TECHNICAL FORMULAS
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

def initialize_csv():
    if not os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Date", "Entry_Time", "Exit_Time", "Asset", "Option", "Entry_Spot", "Exit_Spot", "Result", "PnL"])
        except Exception as e:
            print(f"CSV init error: {e}")

def log_trade_to_csv(pos, exit_spot, result, pnl):
    try:
        with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                pos["date"],
                pos["entry_time"],
                now_ist().strftime("%H:%M:%S"),
                pos["asset"],
                pos["option_name"],
                pos["entry_spot"],
                exit_spot,
                result,
                f"{pnl:.2f}"
            ])
    except Exception as e:
        print(f"CSV log error: {e}")

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
            send_alert(
                f"🌅 New Trading Day: {today}\n"
                f"Daily Limit: {MAX_DAILY_TRADES}\n"
                f"Engine: Two-Direction + Trailing SL + ADX Sideways Filter 🛡️"
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

def calculate_adx(series, period=14):
    """Calculates Trend Strength (ADX) to avoid choppy sideways markets."""
    if len(series) < period * 2:
        return 25.0  # Default neutral
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

def signal_allowed(index_name):
    now = now_ist()
    prev = last_signal_time.get(index_name)
    if prev is None:
        return True
    return (now - prev).total_seconds() / 60 >= SIGNAL_COOLDOWN_MINUTES

# ============================================================
# LIVE POSITION & TRAILING SL ENGINE
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

                # Trailing SL Trigger: Shift SL to Entry Spot or higher
                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(pos["entry_spot"] + (gain_pts - pos["trail_trigger_pts"]) * 0.5, 2)
                    if new_sl > pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True
                        send_alert(f"🛡️ TRAILING SL SHIFTED\nAsset: {index_name} CE\nNew Trailing SL: ₹{pos['sl_price']:,.2f} (Profits Protected!)")

                if current_spot >= pos["target_price"]:
                    hit_target = True
                elif current_spot <= pos["sl_price"]:
                    hit_sl = True

            else:  # PE
                gain_pts = pos["entry_spot"] - current_spot

                # Trailing SL Trigger for PE
                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(pos["entry_spot"] - (gain_pts - pos["trail_trigger_pts"]) * 0.5, 2)
                    if new_sl < pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True
                        send_alert(f"🛡️ TRAILING SL SHIFTED\nAsset: {index_name} PE\nNew Trailing SL: ₹{pos['sl_price']:,.2f} (Profits Protected!)")

                if current_spot <= pos["target_price"]:
                    hit_target = True
                elif current_spot >= pos["sl_price"]:
                    hit_sl = True

            if hit_target:
                pnl = pos["target_pts"] * pos["lot_size"]
                total_pnl += pnl
                msg = (
                    f"🎯 **TARGET HIT!** 🚀\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Asset: {index_name}\n"
                    f"Trade: {pos['option_name']}\n"
                    f"Entry Spot: ₹{pos['entry_spot']:,.2f}\n"
                    f"Exit Spot: ₹{current_spot:,.2f}\n"
                    f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
                    f"Gain: +{pos['target_pts']} pts\n"
                    f"💵 **Trade Profit: +₹{pnl:,.2f}**\n"
                    f"📊 **Day Total P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}**\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_alert(msg)
                log_trade_to_csv(pos, current_spot, "TARGET_HIT", pnl)
                closed_indices.append(index_name)

            elif hit_sl:
                exit_diff = (current_spot - pos["entry_spot"]) if pos["direction"] == "BULLISH" else (pos["entry_spot"] - current_spot)
                pnl = exit_diff * pos["lot_size"]
                total_pnl += pnl
                res_type = "TRAILED_EXIT" if pos.get("is_trailed") and pnl >= 0 else "STOPLOSS_HIT"

                msg = (
                    f"{'🛡️ PROTECTED TRAILED EXIT' if res_type == 'TRAILED_EXIT' else '🛑 STOPLOSS HIT! ⚠️'}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Asset: {index_name}\n"
                    f"Trade: {pos['option_name']}\n"
                    f"Entry Spot: ₹{pos['entry_spot']:,.2f}\n"
                    f"Exit Spot: ₹{current_spot:,.2f}\n"
                    f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
                    f"Diff: {exit_diff:+.2f} pts\n"
                    f"{'💵 Trade Gain: +' if pnl >= 0 else '💸 Trade Loss: -'}₹{abs(pnl):,.2f}\n"
                    f"📊 **Day Total P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}**\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_alert(msg)
                log_trade_to_csv(pos, current_spot, res_type, pnl)
                closed_indices.append(index_name)

        for idx in closed_indices:
            del active_positions[idx]

# ============================================================
# TWO-DIRECTION STRATEGY WITH ADX FILTER & CONFIRMATION
# ============================================================

def process_pending_confirmations(rates):
    global trades_count
    with state_lock:
        confirmed = []
        for index_name, p in pending_confirmations.items():
            spot = rates.get(index_name)
            if not spot or trades_count >= MAX_DAILY_TRADES:
                continue

            is_bull_confirmed = (p["direction"] == "BULLISH" and spot > p["confirm_level"])
            is_bear_confirmed = (p["direction"] == "BEARISH" and spot < p["confirm_level"])

            if is_bull_confirmed or is_bear_confirmed:
                trades_count += 1
                last_signal_time[index_name] = now_ist()
                current_num = trades_count

                target_pts = p["target_pts"]
                sl_pts = p["sl_pts"]
                target_price = round(spot + target_pts if p["direction"] == "BULLISH" else spot - target_pts, 2)
                sl_price = round(spot - sl_pts if p["direction"] == "BULLISH" else spot + sl_pts, 2)

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
                    f"⚡ **ORDER EXECUTED (CONFIRMED)**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Pattern: {p['pattern']} Activated!\n"
                    f"Asset: {index_name}\n"
                    f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
                    f"Action: **BUY {p['option_name']}**\n"
                    f"Entry Spot: ₹{spot:,.2f}\n"
                    f"ADX Strength: {p['adx']:.1f} (Trend Confirmed)\n"
                    f"🎯 Target: ₹{target_price:,.2f} (+{target_pts} pts)\n"
                    f"🛑 Initial SL: ₹{sl_price:,.2f} (-{sl_pts} pts)\n"
                    f"🛡️ **Hedging Shield:** Active ({p['hedge_symbol']})\n"
                    f"Trade #{current_num}/{MAX_DAILY_TRADES}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📌 Trailing SL activates at +{p['trail_trigger_pts']} pts"
                )
                send_alert(alert)
                confirmed.append(index_name)

        for idx in confirmed:
            del pending_confirmations[idx]

def analyze_index(index_name, current_spot, info):
    history = price_history[index_name]
    history.append(current_spot)
    if len(history) > 100:
        history.pop(0)

    if len(history) < EMA_SLOW + 2:
        return

    with state_lock:
        if index_name in active_positions or index_name in pending_confirmations or trades_count >= MAX_DAILY_TRADES:
            return

    # 1. ADX CHOPPY / SIDEWAYS FILTER
    adx_val = calculate_adx(history, ADX_PERIOD)
    if adx_val < MIN_ADX_TREND:
        # Market is choppy/sideways - Skip trades to avoid whipsaws
        return

    s = pd.Series(history)
    ema_fast = round(float(s.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]), 2)
    ema_slow = round(float(s.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]), 2)
    rsi_val = calculate_rsi(history, RSI_PERIOD)
    rsi = round(rsi_val, 2) if rsi_val is not None else 50.0

    recent_high = max(history[-5:])
    recent_low = min(history[-5:])

    # Directional setups
    bullish_setup = (current_spot > ema_fast > ema_slow and rsi >= RSI_BULLISH)
    bearish_setup = (current_spot < ema_fast < ema_slow and rsi <= RSI_BEARISH)

    if not bullish_setup and not bearish_setup:
        return

    if not signal_allowed(index_name):
        return

    step = info["step"]
    atm_strike = get_atm_strike(current_spot, step)

    if bullish_setup:
        direction = "BULLISH"
        pattern = "📈 Bullish Momentum / Hammer"
        opt_name = f"{index_name} {atm_strike} CE"
        confirm_level = round(recent_high + 2.0, 2)
        hedge_strike = atm_strike + info["hedge_offset"]
        hedge_symbol = f"{index_name} {hedge_strike} PE (Shield)"
    else:
        direction = "BEARISH"
        pattern = "📉 Bearish Hanging Man / Breakdown"
        opt_name = f"{index_name} {atm_strike} PE"
        confirm_level = round(recent_low - 2.0, 2)
        hedge_strike = atm_strike - info["hedge_offset"]
        hedge_symbol = f"{index_name} {hedge_strike} CE (Shield)"

    with state_lock:
        pending_confirmations[index_name] = {
            "direction": direction,
            "pattern": pattern,
            "option_name": opt_name,
            "confirm_level": confirm_level,
            "target_pts": info["target_pts"],
            "sl_pts": info["initial_sl_pts"],
            "trail_trigger_pts": info["trail_trigger_pts"],
            "lot_size": info["lot_size"],
            "hedge_symbol": hedge_symbol,
            "adx": adx_val
        }

    setup_msg = (
        f"🔍 **SETUP DETECTED (ADX {adx_val:.1f} TRENDING)**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: {index_name}\n"
        f"Pattern: {pattern}\n"
        f"Target Strike: {opt_name}\n"
        f"Current Spot: ₹{current_spot:,.2f}\n"
        f"Trigger Level: {'Break Above' if direction == 'BULLISH' else 'Break Below'} ₹{confirm_level:,.2f}\n"
        f"🛡️ Hedging Shield: {hedge_symbol}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bot will execute trade once trigger level breaks!"
    )
    send_alert(setup_msg)

# ============================================================
# TELEGRAM COMMANDS THREAD
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
                send_alert("🟢 BOT STARTED\nADX Filter + Two-Direction Engine Active.")
            elif text == "/stop":
                bot_active = False
                send_alert("🔴 BOT STOPPED\nScanner Paused.")
            elif text == "/status":
                status = "🟢 ACTIVE" if bot_active else "🔴 PAUSED"
                mkt = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
                open_str = "\n".join([f"• {k}: {v['option_name']} @ ₹{v['entry_spot']} (SL: ₹{v['sl_price']})" for k, v in active_positions.items()]) or "None"
                pending_str = "\n".join([f"• {k}: {v['option_name']} waiting @ ₹{v['confirm_level']}" for k, v in pending_confirmations.items()]) or "None"
                send_alert(
                    f"📊 **BOT STATUS**\n━━━━━━━━━━━━━━\n"
                    f"Bot: {status}\nMarket: {mkt}\nDate: {today_string()}\n"
                    f"Signals: {trades_count}/{MAX_DAILY_TRADES}\n"
                    f"Active Trades:\n{open_str}\n"
                    f"Pending Confirmations:\n{pending_str}\n"
                    f"Total P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}"
                )
            elif text == "/pnl":
                send_alert(
                    f"💰 **TODAY'S P&L REPORT**\n━━━━━━━━━━━━━━\n"
                    f"Date: {today_string()}\n"
                    f"Trades Taken: {trades_count}/{MAX_DAILY_TRADES}\n"
                    f"Realized P&L: {'+' if total_pnl >= 0 else ''}₹{total_pnl:,.2f}\n"
                    f"Open Positions: {len(active_positions)}"
                )
            elif text == "/test":
                rates = get_nse_live_prices()
                n_price = rates.get("NIFTY", "N/A") if rates else "N/A"
                send_alert(f"⚡ LIVE NSE FEED\nNIFTY 50: ₹{n_price}\nADX Sideways Filter Active!")
            elif text == "/help":
                send_alert("🤖 COMMANDS:\n/status - Open trades & engine status\n/pnl - Net realized profit/loss\n/test - Live NSE tick check\n/start - Resume\n/stop - Pause")
    except Exception:
        pass

def telegram_loop():
    while True:
        check_telegram_commands()
        time.sleep(2)

def main_trading_loop():
    global trade_date
    trade_date = today_string()
    initialize_csv()
    send_alert(
        "🛡️ SIDEWAYS FILTER & TWO-DIRECTION ENGINE ACTIVE\n"
        "• ADX Filter: Choppy / Sideways Markets Blocked (ADX < 20)\n"
        "• Hammer & Hanging Man Setups Activated\n"
        "• Trailing SL & Hedging Protection Enabled"
    )

    while True:
        try:
            reset_daily_counter_if_needed()
            if not bot_active or not is_market_open():
                time.sleep(30)
                continue

            rates = get_nse_live_prices()
            if rates:
                # 1. Track Targets & Trailing SL for existing positions
                track_open_positions(rates)

                # 2. Check pending setups for confirmation breakout
                process_pending_confirmations(rates)

                # 3. Scan for new setups if under daily limit
                if trades_count < MAX_DAILY_TRADES:
                    for name, info in WATCHLIST.items():
                        if name in rates:
                            analyze_index(name, rates[name], info)

            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            print(f"Main loop err: {e}")
            time.sleep(10)

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    t_thread = threading.Thread(target=telegram_loop, daemon=True)
    t_thread.start()

    scanner_thread = threading.Thread(target=main_trading_loop, daemon=True)
    scanner_thread.start()

    run_web()
