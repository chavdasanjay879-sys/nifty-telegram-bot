import os
import time
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd
from flask import Flask, jsonify

# ============================================================
# MULTI-LAYER ADAPTIVE HIGH-CONFIDENCE ENGINE
# NSE + TELEGRAM + RENDER 24x7
#
# IMPORTANT:
# - Set BOT_TOKEN and CHAT_ID in Render Environment Variables.
# - This program sends Telegram alerts only. It does NOT place
#   real broker orders.
# - Targets/SL/P&L are calculated from INDEX SPOT points, not
#   option premium.
# ============================================================

app = Flask(__name__)

# -------------------- WEB SERVER -----------------------------

@app.route("/")
def home():
    return "Multi-Layer Adaptive High-Confidence Engine Live 24x7!"

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_active": bot_active,
        "trades_today": trades_count,
        "daily_limit": MAX_DAILY_TRADES,
        "total_pnl": round(total_pnl, 2),
        "open_positions": list(active_positions.keys()),
        "pending_setups": list(pending_confirmations.keys())
    })

def run_web():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ============================================================
# CONFIGURATION
# ============================================================

TIMEZONE = ZoneInfo("Asia/Kolkata")

MAX_DAILY_TRADES = 2
MIN_CONFIDENCE = 85.0

EMA_TREND = 50
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ADX_PERIOD = 14

MIN_ADX_TREND = 25.0
SIGNAL_COOLDOWN_MINUTES = 30
SETUP_EXPIRY_MINUTES = 5
SCAN_INTERVAL_SECONDS = 15

TRADE_START_TIME = dt_time(9, 45)
TRADE_END_TIME = dt_time(14, 45)

# Read secrets only from environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

CSV_FILE = "trade_log.csv"

WATCHLIST = {
    "NIFTY": {
        "symbol": "NIFTY 50",
        "lot_size": 75,
        "step": 50,
        "target_pts": 50,
        "initial_sl_pts": 16,
        "trail_trigger_pts": 20,
    },
    "BANKNIFTY": {
        "symbol": "NIFTY BANK",
        "lot_size": 30,
        "step": 100,
        "target_pts": 100,
        "initial_sl_pts": 35,
        "trail_trigger_pts": 40,
    },
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

price_history = {
    "NIFTY": [],
    "BANKNIFTY": [],
}

state_lock = threading.RLock()


# ============================================================
# TELEGRAM
# ============================================================

def telegram_ready():
    return bool(BOT_TOKEN and CHAT_ID)


def send_alert(message):
    if not telegram_ready():
        print("Telegram is not configured. Set BOT_TOKEN and CHAT_ID.")
        return False

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message
        }
        res = requests.post(url, data=payload, timeout=10)
        if res.status_code != 200:
            print(f"Telegram HTTP {res.status_code}: {res.text[:300]}")
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ============================================================
# NSE LIVE DATA
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/"
})


def init_nse_session():
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass


def get_nse_live_prices():
    url = "https://www.nseindia.com/api/allIndices"

    try:
        res = session.get(url, timeout=10)

        if res.status_code in (401, 403):
            init_nse_session()
            res = session.get(url, timeout=10)

        if res.status_code != 200:
            print(f"NSE HTTP {res.status_code}")
            return None

        data = res.json().get("data", [])
        rates = {}

        for item in data:
            name = item.get("index")
            last = item.get("last")

            try:
                value = float(str(last).replace(",", ""))
            except Exception:
                continue

            if name == "NIFTY 50":
                rates["NIFTY"] = value
            elif name == "NIFTY BANK":
                rates["BANKNIFTY"] = value

        return rates or None

    except Exception as e:
        print(f"NSE feed error: {e}")
        return None


# ============================================================
# TIME / MARKET HELPERS
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


def get_atm_strike(spot, step):
    return int(round(spot / step) * step)


# ============================================================
# INDICATORS
# ============================================================

def calculate_rsi(series, window=14):
    if len(series) < window + 1:
        return None

    s = pd.Series(series, dtype=float)
    delta = s.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / window,
        adjust=False,
        min_periods=window
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / window,
        adjust=False,
        min_periods=window
    ).mean()

    last_loss = float(avg_loss.iloc[-1])
    last_gain = float(avg_gain.iloc[-1])

    if last_loss == 0:
        return 100.0 if last_gain != 0 else 50.0

    rs = last_gain / last_loss
    return float(100 - (100 / (1 + rs)))


def calculate_adx(series, period=14):
    if len(series) < period * 2 + 2:
        return 0.0

    s = pd.Series(series, dtype=float)

    diff = s.diff()
    pos_dm = diff.clip(lower=0)
    neg_dm = (-diff).clip(lower=0)

    tr = diff.abs()

    atr = tr.ewm(
        span=period,
        adjust=False,
        min_periods=period
    ).mean()

    pos_di = 100 * (
        pos_dm.ewm(span=period, adjust=False).mean()
        / (atr + 1e-9)
    )

    neg_di = 100 * (
        neg_dm.ewm(span=period, adjust=False).mean()
        / (atr + 1e-9)
    )

    dx = 100 * (
        (pos_di - neg_di).abs()
        / (pos_di + neg_di + 1e-9)
    )

    adx = dx.ewm(
        span=period,
        adjust=False
    ).mean().iloc[-1]

    return float(adx) if np.isfinite(adx) else 0.0


def calculate_tick_volatility(series, lookback=20):
    if len(series) < lookback + 1:
        return 0.0

    s = np.asarray(series[-lookback:], dtype=float)
    diffs = np.diff(s)

    if len(diffs) == 0:
        return 0.0

    return float(np.std(diffs))


def calculate_momentum(series, lookback=8):
    if len(series) <= lookback:
        return 0.0

    return float(series[-1] - series[-1 - lookback])


def calculate_slope(series, lookback=10):
    if len(series) < lookback:
        return 0.0

    y = np.asarray(series[-lookback:], dtype=float)
    x = np.arange(len(y), dtype=float)

    slope = np.polyfit(x, y, 1)[0]
    return float(slope)


# ============================================================
# ADAPTIVE CONFIDENCE ENGINE
# ============================================================

def build_signal(index_name, current_spot, info):
    history = price_history[index_name]

    if len(history) < EMA_TREND + 10:
        return None

    s = pd.Series(history, dtype=float)

    ema_trend = float(
        s.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1]
    )
    ema_fast = float(
        s.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    )
    ema_slow = float(
        s.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    )

    rsi = calculate_rsi(history, RSI_PERIOD)
    adx = calculate_adx(history, ADX_PERIOD)

    if rsi is None:
        return None

    momentum = calculate_momentum(history, 8)
    slope = calculate_slope(history, 10)
    volatility = calculate_tick_volatility(history, 20)

    recent = history[-8:]
    recent_high = max(recent)
    recent_low = min(recent)

    # --------------------------------------------------------
    # Direction scoring
    # --------------------------------------------------------

    bull_score = 0.0
    bear_score = 0.0

    # 1. Macro trend: 20 points
    if current_spot > ema_trend:
        bull_score += 20
    elif current_spot < ema_trend:
        bear_score += 20

    # 2. Fast/slow EMA alignment: 15 points
    if ema_fast > ema_slow:
        bull_score += 15
    elif ema_fast < ema_slow:
        bear_score += 15

    # 3. RSI confirmation: 15 points
    if 55 <= rsi <= 70:
        bull_score += 15
    elif 30 <= rsi <= 45:
        bear_score += 15

    # 4. ADX trend strength: 15 points
    if adx >= MIN_ADX_TREND:
        if bull_score >= bear_score:
            bull_score += 15
        else:
            bear_score += 15

    # 5. Momentum: 10 points
    if momentum > 0:
        bull_score += 10
    elif momentum < 0:
        bear_score += 10

    # 6. Short-term slope: 10 points
    if slope > 0:
        bull_score += 10
    elif slope < 0:
        bear_score += 10

    # 7. Price position in recent range: 10 points
    range_size = recent_high - recent_low

    if range_size > 0:
        position = (current_spot - recent_low) / range_size

        if position >= 0.70:
            bull_score += 10
        elif position <= 0.30:
            bear_score += 10

    # --------------------------------------------------------
    # Regime / quality adjustments
    # --------------------------------------------------------

    # Very low movement = avoid dead/flat market.
    if volatility > 0:
        if abs(momentum) < volatility * 0.8:
            bull_score -= 5
            bear_score -= 5

    bull_score = max(0.0, min(100.0, bull_score))
    bear_score = max(0.0, min(100.0, bear_score))

    if bull_score >= bear_score:
        direction = "BULLISH"
        confidence = bull_score
    else:
        direction = "BEARISH"
        confidence = bear_score

    # Need strong trend + strong confidence.
    if adx < MIN_ADX_TREND:
        return None

    if confidence < MIN_CONFIDENCE:
        return None

    # RSI extremes are avoided because chasing can be risky.
    if direction == "BULLISH" and not (55 <= rsi <= 70):
        return None

    if direction == "BEARISH" and not (30 <= rsi <= 45):
        return None

    # Require directional EMA alignment.
    if direction == "BULLISH" and not (
        current_spot > ema_trend and ema_fast > ema_slow
    ):
        return None

    if direction == "BEARISH" and not (
        current_spot < ema_trend and ema_fast < ema_slow
    ):
        return None

    atm_strike = get_atm_strike(current_spot, info["step"])

    if direction == "BULLISH":
        option_name = f"{index_name} {atm_strike} CE"
        sentiment = "STRONG BULLISH REGIME"
        confirm_level = round(recent_high + 2.5, 2)
    else:
        option_name = f"{index_name} {atm_strike} PE"
        sentiment = "STRONG BEARISH REGIME"
        confirm_level = round(recent_low - 2.5, 2)

    return {
        "direction": direction,
        "confidence": confidence,
        "sentiment": sentiment,
        "option_name": option_name,
        "confirm_level": confirm_level,
        "target_pts": info["target_pts"],
        "sl_pts": info["initial_sl_pts"],
        "trail_trigger_pts": info["trail_trigger_pts"],
        "lot_size": info["lot_size"],
        "adx": adx,
        "rsi": rsi,
        "momentum": momentum,
        "slope": slope,
        "volatility": volatility,
    }


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_counter_if_needed():
    global trades_count
    global trade_date
    global last_signal_time
    global price_history
    global total_pnl
    global active_positions
    global pending_confirmations

    today = today_string()

    with state_lock:
        if trade_date != today:
            trade_date = today
            trades_count = 0
            total_pnl = 0.0
            last_signal_time = {}
            active_positions = {}
            pending_confirmations = {}
            price_history = {
                "NIFTY": [],
                "BANKNIFTY": []
            }

            print(f"New trading day: {today}")

    send_alert(
        f"🌅 NEW TRADING DAY\n"
        f"Date: {today}\n"
        f"Engine: Multi-Layer Adaptive High Confidence\n"
        f"Limit: {MAX_DAILY_TRADES} quality trades\n"
        f"Mode: Single Direction Only"
    )


# ============================================================
# TRADE LOG
# ============================================================

def log_trade(index_name, pos, exit_spot, pnl, result):
    try:
        exists = os.path.exists(CSV_FILE)

        row = {
            "date": today_string(),
            "time": now_ist().strftime("%H:%M:%S"),
            "asset": index_name,
            "option": pos.get("option_name", ""),
            "direction": pos.get("direction", ""),
            "entry_spot": pos.get("entry_spot", ""),
            "exit_spot": exit_spot,
            "confidence": pos.get("confidence", ""),
            "result": result,
            "pnl": round(pnl, 2),
        }

        df = pd.DataFrame([row])
        df.to_csv(
            CSV_FILE,
            mode="a",
            header=not exists,
            index=False
        )
    except Exception as e:
        print(f"Trade log error: {e}")


# ============================================================
# POSITION TRACKER
# ============================================================

def track_open_positions(rates):
    global total_pnl

    notifications = []

    with state_lock:
        closed = []

        for index_name, pos in list(active_positions.items()):
            current_spot = rates.get(index_name)

            if not current_spot:
                continue

            hit_target = False
            hit_sl = False

            if pos["direction"] == "BULLISH":
                gain_pts = current_spot - pos["entry_spot"]

                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(
                        pos["entry_spot"]
                        + (gain_pts - pos["trail_trigger_pts"]) * 0.6,
                        2
                    )

                    if new_sl > pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True

                        notifications.append(
                            f"🛡️ TRAILING SL SHIFTED\n"
                            f"{index_name} CE\n"
                            f"New Spot Lock: ₹{new_sl:,.2f}"
                        )

                if current_spot >= pos["target_price"]:
                    hit_target = True
                elif current_spot <= pos["sl_price"]:
                    hit_sl = True

            else:
                gain_pts = pos["entry_spot"] - current_spot

                if gain_pts >= pos["trail_trigger_pts"]:
                    new_sl = round(
                        pos["entry_spot"]
                        - (gain_pts - pos["trail_trigger_pts"]) * 0.6,
                        2
                    )

                    if new_sl < pos["sl_price"]:
                        pos["sl_price"] = new_sl
                        pos["is_trailed"] = True

                        notifications.append(
                            f"🛡️ TRAILING SL SHIFTED\n"
                            f"{index_name} PE\n"
                            f"New Spot Lock: ₹{new_sl:,.2f}"
                        )

                if current_spot <= pos["target_price"]:
                    hit_target = True
                elif current_spot >= pos["sl_price"]:
                    hit_sl = True

            if hit_target:
                pnl = pos["target_pts"] * pos["lot_size"]
                total_pnl += pnl

                log_trade(
                    index_name,
                    pos,
                    current_spot,
                    pnl,
                    "TARGET"
                )

                notifications.append(
                    f"🎯 TARGET ACHIEVED\n"
                    f"Asset: {index_name}\n"
                    f"Option: {pos['option_name']}\n"
                    f"Entry Spot: ₹{pos['entry_spot']:,.2f}\n"
                    f"Exit Spot: ₹{current_spot:,.2f}\n"
                    f"Gain: +{pos['target_pts']} pts\n"
                    f"Model P&L: +₹{pnl:,.2f}\n"
                    f"Total P&L: ₹{total_pnl:,.2f}"
                )

                closed.append(index_name)

            elif hit_sl:
                exit_diff = (
                    current_spot - pos["entry_spot"]
                    if pos["direction"] == "BULLISH"
                    else pos["entry_spot"] - current_spot
                )

                pnl = exit_diff * pos["lot_size"]
                total_pnl += pnl

                result = "TRAIL_EXIT" if (
                    pos.get("is_trailed") and pnl >= 0
                ) else "STOPLOSS"

                log_trade(
                    index_name,
                    pos,
                    current_spot,
                    pnl,
                    result
                )

                notifications.append(
                    f"{'🛡️ PROFIT PROTECTED EXIT' if result == 'TRAIL_EXIT' else '🛑 STOPLOSS EXIT'}\n"
                    f"Asset: {index_name}\n"
                    f"Option: {pos['option_name']}\n"
                    f"Entry Spot: ₹{pos['entry_spot']:,.2f}\n"
                    f"Exit Spot: ₹{current_spot:,.2f}\n"
                    f"Points: {exit_diff:+.2f}\n"
                    f"Model P&L: {'+' if pnl >= 0 else '-'}₹{abs(pnl):,.2f}\n"
                    f"Total P&L: {'+' if total_pnl >= 0 else '-'}₹{abs(total_pnl):,.2f}"
                )

                closed.append(index_name)

        for index_name in closed:
            active_positions.pop(index_name, None)

    for message in notifications:
        send_alert(message)


# ============================================================
# BREAKOUT CONFIRMATION
# ============================================================

def process_pending_confirmations(rates):
    global trades_count

    notifications = []

    with state_lock:
        confirmed = []

        for index_name, setup in list(pending_confirmations.items()):
            spot = rates.get(index_name)

            if not spot:
                continue

            created = setup.get("created_at")
            if created:
                age = (
                    now_ist() - created
                ).total_seconds() / 60

                if age > SETUP_EXPIRY_MINUTES:
                    notifications.append(
                        f"⌛ SETUP EXPIRED\n"
                        f"Asset: {index_name}\n"
                        f"Confidence: {setup['confidence']:.1f}%"
                    )
                    confirmed.append(index_name)
                    continue

            if trades_count >= MAX_DAILY_TRADES:
                continue

            direction = setup["direction"]

            is_confirmed = (
                direction == "BULLISH"
                and spot >= setup["confirm_level"]
            ) or (
                direction == "BEARISH"
                and spot <= setup["confirm_level"]
            )

            if not is_confirmed:
                continue

            trades_count += 1

            last_signal_time[index_name] = now_ist()

            target_pts = setup["target_pts"]
            sl_pts = setup["sl_pts"]

            if direction == "BULLISH":
                target_price = round(spot + target_pts, 2)
                sl_price = round(spot - sl_pts, 2)
            else:
                target_price = round(spot - target_pts, 2)
                sl_price = round(spot + sl_pts, 2)

            active_positions[index_name] = {
                "asset": index_name,
                "option_name": setup["option_name"],
                "direction": direction,
                "entry_spot": spot,
                "target_price": target_price,
                "sl_price": sl_price,
                "target_pts": target_pts,
                "sl_pts": sl_pts,
                "trail_trigger_pts": setup["trail_trigger_pts"],
                "lot_size": setup["lot_size"],
                "confidence": setup["confidence"],
                "date": today_string(),
                "entry_time": now_ist().strftime("%H:%M:%S"),
                "is_trailed": False,
            }

            notifications.append(
                f"⚡ HIGH-CONFIDENCE CONFIRMED\n"
                f"Asset: {index_name}\n"
                f"Action: BUY {setup['option_name']} ONLY\n"
                f"Direction: {direction}\n"
                f"Confidence: {setup['confidence']:.1f}%\n"
                f"Entry Spot: ₹{spot:,.2f}\n"
                f"ADX: {setup['adx']:.1f}\n"
                f"RSI: {setup['rsi']:.1f}\n"
                f"🎯 Target Spot: ₹{target_price:,.2f}\n"
                f"🛑 Stop Spot: ₹{sl_price:,.2f}\n"
                f"Trade #{trades_count}/{MAX_DAILY_TRADES}\n"
                f"⚠️ Alert/model only — no broker order placed."
            )

            confirmed.append(index_name)

        for index_name in confirmed:
            pending_confirmations.pop(index_name, None)

    for message in notifications:
        send_alert(message)


# ============================================================
# INDEX ANALYSIS
# ============================================================

def analyze_index(index_name, current_spot, info):
    history = price_history[index_name]

    history.append(float(current_spot))

    if len(history) > 180:
        history.pop(0)

    if len(history) < EMA_TREND + 10:
        return

    with state_lock:
        if (
            index_name in active_positions
            or index_name in pending_confirmations
            or trades_count >= MAX_DAILY_TRADES
        ):
            return

    previous = last_signal_time.get(index_name)

    if previous:
        cooldown = (
            now_ist() - previous
        ).total_seconds() / 60

        if cooldown < SIGNAL_COOLDOWN_MINUTES:
            return

    setup = build_signal(
        index_name,
        current_spot,
        info
    )

    if not setup:
        return

    # One-direction rule: never create both CE and PE for
    # the same index at the same time.
    with state_lock:
        if index_name in active_positions:
            return

        if index_name in pending_confirmations:
            return

        pending_confirmations[index_name] = {
            **setup,
            "created_at": now_ist()
        }

    send_alert(
        f"🔍 HIGH-CONFIDENCE SETUP DETECTED\n"
        f"Asset: {index_name}\n"
        f"Candidate: BUY {setup['option_name']} ONLY\n"
        f"Direction: {setup['direction']}\n"
        f"Confidence: {setup['confidence']:.1f}%\n"
        f"Sentiment: {setup['sentiment']}\n"
        f"ADX: {setup['adx']:.1f}\n"
        f"RSI: {setup['rsi']:.1f}\n"
        f"Momentum: {setup['momentum']:+.2f}\n"
        f"Trigger Level: ₹{setup['confirm_level']:,.2f}\n"
        f"⏳ Waiting for breakout confirmation..."
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def check_telegram_commands():
    global bot_active
    global last_update_id

    if not telegram_ready():
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

        res = requests.get(
            url,
            params={
                "offset": last_update_id + 1,
                "timeout": 1
            },
            timeout=5
        )

        if res.status_code != 200:
            return

        for update in res.json().get("result", []):
            last_update_id = update.get(
                "update_id",
                last_update_id
            )

            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            sender = str(
                msg.get("chat", {}).get("id", "")
            )

            if sender != CHAT_ID:
                continue

            if text == "/start":
                send_alert(
                    "🤖 Engine Online\n"
                    "/status - current status\n"
                    "/pnl - today's model P&L\n"
                    "/pause - pause new setups\n"
                    "/resume - resume new setups"
                )

            elif text == "/pnl":
                send_alert(
                    f"💰 P&L SUMMARY\n"
                    f"Trades: {trades_count}/{MAX_DAILY_TRADES}\n"
                    f"Realized model P&L: "
                    f"{'+' if total_pnl >= 0 else '-'}"
                    f"₹{abs(total_pnl):,.2f}"
                )

            elif text == "/status":
                with state_lock:
                    open_items = [
                        f"• {k}: {v['option_name']} "
                        f"(SL ₹{v['sl_price']:,.2f})"
                        for k, v in active_positions.items()
                    ]

                    pending_items = [
                        f"• {k}: {v['option_name']} "
                        f"({v['confidence']:.1f}%)"
                        for k, v in pending_confirmations.items()
                    ]

                open_str = "\n".join(open_items) or "None"
                pending_str = "\n".join(pending_items) or "None"

                send_alert(
                    f"📊 BOT STATUS\n"
                    f"State: {'ACTIVE' if bot_active else 'PAUSED'}\n"
                    f"Trades: {trades_count}/{MAX_DAILY_TRADES}\n"
                    f"Open Positions:\n{open_str}\n"
                    f"Pending Setups:\n{pending_str}\n"
                    f"Model P&L: "
                    f"{'+' if total_pnl >= 0 else '-'}"
                    f"₹{abs(total_pnl):,.2f}"
                )

            elif text == "/pause":
                bot_active = False
                send_alert("⏸️ NEW SIGNALS PAUSED.")

            elif text == "/resume":
                bot_active = True
                send_alert("▶️ NEW SIGNALS RESUMED.")

    except Exception as e:
        print(f"Telegram command error: {e}")


def telegram_loop():
    while True:
        try:
            check_telegram_commands()
        except Exception as e:
            print(f"Telegram loop error: {e}")

        time.sleep(2)


# ============================================================
# MAIN TRADING LOOP
# ============================================================

def main_trading_loop():
    global trade_date

    trade_date = today_string()

    if not telegram_ready():
        print(
            "WARNING: BOT_TOKEN/CHAT_ID are not configured. "
            "Set them in Render Environment Variables."
        )

    send_alert(
        "🚀 MULTI-LAYER ADAPTIVE HIGH-CONFIDENCE ENGINE ONLINE\n"
        "• 50 EMA macro trend\n"
        "• 9/21 EMA alignment\n"
        "• RSI confirmation\n"
        "• ADX trend-strength filter\n"
        "• Momentum + slope confirmation\n"
        "• Recent-range breakout confirmation\n"
        "• Adaptive confidence threshold\n"
        "• Single-direction CE OR PE only\n"
        f"• Minimum confidence: {MIN_CONFIDENCE:.0f}%\n"
        f"• Daily limit: {MAX_DAILY_TRADES}\n"
        "⚠️ Alerts/model only — no broker orders."
    )

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

                with state_lock:
                    can_scan = trades_count < MAX_DAILY_TRADES

                if can_scan:
                    for name, info in WATCHLIST.items():
                        spot = rates.get(name)

                        if spot is not None:
                            analyze_index(
                                name,
                                spot,
                                info
                            )

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(10)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    threading.Thread(
        target=telegram_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=main_trading_loop,
        daemon=True
    ).start()

    run_web()
