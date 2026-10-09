import os
import time
import csv
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd
from flask import Flask, jsonify

TZ = ZoneInfo("Asia/Kolkata")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))

WATCHLIST = {
    "NIFTY": {
        "index_name": "NIFTY 50",
        "lot_size": int(os.getenv("NIFTY_LOT_SIZE", "65")),
        "strike_step": 50,
    },
    "BANKNIFTY": {
        "index_name": "NIFTY BANK",
        "lot_size": int(os.getenv("BANKNIFTY_LOT_SIZE", "30")),
        "strike_step": 100,
    },
}

LOTS_PER_TRADE = 3
MAX_DAILY_TRADES = 5
MIN_CONFIDENCE = 80.0
EMA_TREND = 50
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MIN_TREND_STRENGTH = 18.0
SCAN_INTERVAL_SECONDS = 20

TRADE_START = dt_time(9, 30)
TRADE_END = dt_time(14, 45)
FORCE_EXIT_TIME = dt_time(15, 15)

TARGET_PCT = 0.10
STOPLOSS_PCT = 0.08
TRAIL_START_PCT = 0.06
TRAIL_GIVEBACK_PCT = 0.03
MAX_DAILY_LOSS_RUPEES = 3000.0
MIN_OPTION_PREMIUM = 10.0
CSV_FILE = "paper_trade_log.csv"

app = Flask(__name__)
lock = threading.RLock()
http = requests.Session()
http.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
})

bot_active = True
trade_date = None
trades_today = 0
realized_pnl = 0.0
price_history = {key: [] for key in WATCHLIST}
open_positions = {}
last_signal_at = {}
last_alert_keys = set()
last_update_id = 0
market_data_last_ok = None


@app.route("/")
def home():
    return "Option Buying Paper Bot Running — No Real Orders"


@app.route("/health")
def health():
    with lock:
        return jsonify({
            "status": "ok",
            "mode": "paper_only",
            "active": bot_active,
            "trades_today": trades_today,
            "daily_limit": MAX_DAILY_TRADES,
            "realized_pnl": round(realized_pnl, 2),
            "daily_loss_limit": MAX_DAILY_LOSS_RUPEES,
            "open_positions": list(open_positions.keys()),
            "last_market_data": market_data_last_ok,
        })


def now():
    return datetime.now(TZ)


def telegram_ready():
    return bool(BOT_TOKEN and CHAT_ID)


def send_alert(message, dedupe_key=None):
    if dedupe_key:
        with lock:
            if dedupe_key in last_alert_keys:
                return False
            last_alert_keys.add(dedupe_key)

    if not telegram_ready():
        print(message)
        return False

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
        return response.status_code == 200
    except requests.RequestException as exc:
        print("Telegram error:", exc)
        return False


def fetch_index_spots():
    global market_data_last_ok

    try:
        response = http.get(
            "https://www.nseindia.com/api/allIndices",
            timeout=10,
        )
        if response.status_code in (401, 403):
            http.get("https://www.nseindia.com", timeout=8)
            response = http.get(
                "https://www.nseindia.com/api/allIndices",
                timeout=10,
            )

        response.raise_for_status()
        items = response.json().get("data", [])
        result = {}

        for item in items:
            name = item.get("index")
            if name not in ("NIFTY 50", "NIFTY BANK"):
                continue

            try:
                value = float(str(item.get("last")).replace(",", ""))
            except (TypeError, ValueError):
                continue

            key = "NIFTY" if name == "NIFTY 50" else "BANKNIFTY"
            result[key] = value

        if result:
            market_data_last_ok = now().isoformat(timespec="seconds")
            return result

    except (requests.RequestException, ValueError) as exc:
        print("NSE index feed error:", exc)

    return None


def fetch_option_chain(index_key):
    symbol = "NIFTY" if index_key == "NIFTY" else "BANKNIFTY"
    url = (
        "https://www.nseindia.com/api/"
        f"option-chain-indices?symbol={symbol}"
    )

    try:
        response = http.get(url, timeout=10)

        if response.status_code in (401, 403):
            http.get("https://www.nseindia.com", timeout=8)
            response = http.get(url, timeout=10)

        response.raise_for_status()
        payload = response.json().get("records", {})
        today = now().date()
        expiries = []

        for expiry in payload.get("expiryDates", []):
            try:
                expiry_date = datetime.strptime(
                    expiry, "%d-%b-%Y"
                ).date()
                if expiry_date >= today:
                    expiries.append((expiry_date, expiry))
            except ValueError:
                continue

        if not expiries:
            return None

        expiry_date, expiry_text = min(expiries)
        rows = []

        for row in payload.get("data", []):
            if row.get("expiryDate") != expiry_text:
                continue

            try:
                strike = float(row["strikePrice"])
            except (KeyError, TypeError, ValueError):
                continue

            rows.append({
                "strike": strike,
                "expiry": expiry_text,
                "CE": parse_leg(row.get("CE")),
                "PE": parse_leg(row.get("PE")),
            })

        return {
            "expiry": expiry_text,
            "expiry_date": expiry_date,
            "rows": rows,
        }

    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"Option chain error {symbol}:", exc)
        return None


def parse_leg(leg):
    if not isinstance(leg, dict):
        return None

    try:
        premium = float(leg.get("lastPrice", 0) or 0)
        if premium <= 0:
            return None

        return {
            "premium": premium,
            "volume": float(leg.get("totalTradedVolume", 0) or 0),
            "oi": float(leg.get("openInterest", 0) or 0),
            "change_oi": float(
                leg.get("changeinOpenInterest", 0) or 0
            ),
            "iv": float(leg.get("impliedVolatility", 0) or 0),
        }

    except (TypeError, ValueError):
        return None


def calc_rsi(values, period=RSI_PERIOD):
    if len(values) < period + 1:
        return None

    series = pd.Series(values, dtype=float)
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    avg_gain = float(gain.iloc[-1])
    avg_loss = float(loss.iloc[-1])

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def calc_strength(values, period=14):
    if len(values) < period * 2:
        return 0.0

    changes = np.diff(np.asarray(values[-period * 2:], dtype=float))
    denominator = float(np.sum(np.abs(changes)))

    if denominator <= 1e-9:
        return 0.0

    return min(
        100.0,
        100.0 * abs(float(np.sum(changes))) / denominator,
    )


def build_sentiment(index_key, spot):
    history = price_history[index_key]
    history.append(float(spot))

    if len(history) > 500:
        del history[:-500]

    if len(history) < EMA_TREND:
        return None

    series = pd.Series(history, dtype=float)
    ema_trend = float(
        series.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1]
    )
    ema_fast = float(
        series.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    )
    ema_slow = float(
        series.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    )

    rsi = calc_rsi(history)
    strength = calc_strength(history)

    if rsi is None:
        return None

    momentum = history[-1] - history[-min(9, len(history))]
    bull = 0.0
    bear = 0.0

    if spot > ema_trend:
        bull += 25
    elif spot < ema_trend:
        bear += 25

    if ema_fast > ema_slow:
        bull += 20
    elif ema_fast < ema_slow:
        bear += 20

    if 52 <= rsi <= 68:
        bull += 20
    elif 32 <= rsi <= 48:
        bear += 20

    if momentum > 0:
        bull += 15
    elif momentum < 0:
        bear += 15

    if strength >= MIN_TREND_STRENGTH:
        if bull >= bear:
            bull += 20
        else:
            bear += 20
    else:
        bull -= 10
        bear -= 10

    bull = max(0, min(100, bull))
    bear = max(0, min(100, bear))

    direction = "CE" if bull > bear else "PE" if bear > bull else None
    confidence = max(bull, bear)

    if not direction or confidence < MIN_CONFIDENCE:
        return None

    if strength < MIN_TREND_STRENGTH:
        return None

    if direction == "CE" and not (
        spot > ema_trend
        and ema_fast > ema_slow
        and 52 <= rsi <= 68
    ):
        return None

    if direction == "PE" and not (
        spot < ema_trend
        and ema_fast < ema_slow
        and 32 <= rsi <= 48
    ):
        return None

    return {
        "direction": direction,
        "confidence": confidence,
        "rsi": rsi,
        "strength": strength,
        "momentum": momentum,
    }


def nearest_atm_row(chain, spot, step):
    rows = chain.get("rows", [])
    if not rows:
        return None

    atm = round(spot / step) * step
    return min(rows, key=lambda row: abs(row["strike"] - atm))


def log_trade(position, exit_premium, reason, pnl):
    exists = os.path.exists(CSV_FILE)

    row = {
        "date": now().strftime("%Y-%m-%d"),
        "time": now().strftime("%H:%M:%S"),
        "index": position["index"],
        "option": position["option_name"],
        "expiry": position["expiry"],
        "strike": position["strike"],
        "lots": position["lots"],
        "lot_size": position["lot_size"],
        "entry_premium": round(position["entry_premium"], 2),
        "exit_premium": round(exit_premium, 2),
        "reason": reason,
        "pnl_rupees": round(pnl, 2),
        "confidence": round(position["confidence"], 1),
    }

    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=row.keys())
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        print("Trade log error:", exc)


def close_position(index_key, premium, reason):
    global realized_pnl

    with lock:
        position = open_positions.pop(index_key, None)
        if not position:
            return

        pnl = (
            float(premium) - position["entry_premium"]
        ) * position["lot_size"] * position["lots"]
        realized_pnl += pnl
        daily_pnl = realized_pnl

    log_trade(position, premium, reason, pnl)

    send_alert(
        f"📘 PAPER EXIT: {reason}\n"
        f"Option: {position['option_name']}\n"
        f"Entry premium: ₹{position['entry_premium']:.2f}\n"
        f"Exit premium: ₹{premium:.2f}\n"
        f"Lots: {position['lots']}\n"
        f"Estimated trade P&L: ₹{pnl:,.2f}\n"
        f"Today's realized P&L: ₹{daily_pnl:,.2f}\n"
        f"No real order was placed.",
        dedupe_key=f"exit:{position['trade_id']}",
    )


def track_positions(chains):
    for index_key, position in list(open_positions.items()):
        chain = chains.get(index_key)
        if not chain:
            continue

        row = next(
            (
                item for item in chain["rows"]
                if item["strike"] == position["strike"]
                and item["expiry"] == position["expiry"]
            ),
            None,
        )

        leg = row.get(position["direction"]) if row else None
        if not leg:
            continue

        premium = leg["premium"]

        with lock:
            current = open_positions.get(index_key)
            if not current:
                continue

            current["peak_premium"] = max(
                current["peak_premium"], premium
            )
            peak = current["peak_premium"]
            entry = current["entry_premium"]

        if premium >= entry * (1 + TARGET_PCT):
            close_position(index_key, premium, "TARGET")
        elif premium <= entry * (1 - STOPLOSS_PCT):
            close_position(index_key, premium, "STOPLOSS")
        elif (
            peak >= entry * (1 + TRAIL_START_PCT)
            and premium <= peak * (1 - TRAIL_GIVEBACK_PCT)
        ):
            close_position(index_key, premium, "TRAILING_STOP")


def can_open_new_trade():
    return (
        trades_today < MAX_DAILY_TRADES
        and realized_pnl > -MAX_DAILY_LOSS_RUPEES
    )


def try_open_position(index_key, spot, sentiment, chain):
    global trades_today

    config = WATCHLIST[index_key]
    row = nearest_atm_row(chain, spot, config["strike_step"])

    if not row:
        return

    direction = sentiment["direction"]
    leg = row.get(direction)

    if not leg or leg["premium"] < MIN_OPTION_PREMIUM:
        return

    if leg["volume"] <= 0:
        return

    now_ts = time.time()
    if now_ts - last_signal_at.get(index_key, 0) < 600:
        return

    trade_id = (
        f"{now().strftime('%Y%m%d%H%M%S')}-"
        f"{index_key}-{direction}-{int(row['strike'])}"
    )

    position = {
        "trade_id": trade_id,
        "index": index_key,
        "option_name": f"{index_key} {int(row['strike'])} {direction}",
        "direction": direction,
        "strike": row["strike"],
        "expiry": row["expiry"],
        "entry_premium": leg["premium"],
        "peak_premium": leg["premium"],
        "lots": LOTS_PER_TRADE,
        "lot_size": config["lot_size"],
        "confidence": sentiment["confidence"],
        "entry_time": now().isoformat(timespec="seconds"),
    }

    with lock:
        if index_key in open_positions or not can_open_new_trade():
            return

        open_positions[index_key] = position
        trades_today += 1
        last_signal_at[index_key] = now_ts
        count = trades_today

    send_alert(
        f"🟢 PAPER ENTRY — OPTION BUYING\n"
        f"No real order placed\n"
        f"BUY {position['option_name']}\n"
        f"Expiry: {position['expiry']}\n"
        f"Sentiment: {direction}\n"
        f"Confidence score: {sentiment['confidence']:.0f}/100\n"
        f"RSI: {sentiment['rsi']:.1f}\n"
        f"Entry premium: ₹{position['entry_premium']:.2f}\n"
        f"Quantity: {LOTS_PER_TRADE} lots × "
        f"{config['lot_size']} units\n"
        f"Target premium: ₹"
        f"{position['entry_premium'] * (1 + TARGET_PCT):.2f}\n"
        f"Stop premium: ₹"
        f"{position['entry_premium'] * (1 - STOPLOSS_PCT):.2f}\n"
        f"Trade {count}/{MAX_DAILY_TRADES}\n"
        f"Daily loss limit: ₹{MAX_DAILY_LOSS_RUPEES:,.0f}",
        dedupe_key=f"entry:{trade_id}",
    )


def reset_daily_state_if_needed():
    global trade_date, trades_today, realized_pnl
    global price_history, last_signal_at, last_alert_keys

    today = now().strftime("%Y-%m-%d")

    with lock:
        if trade_date == today:
            return

        leftovers = list(open_positions.items())
        open_positions.clear()
        trade_date = today
        trades_today = 0
        realized_pnl = 0.0
        price_history = {key: [] for key in WATCHLIST}
        last_signal_at = {}
        last_alert_keys = set()

    if leftovers:
        send_alert(
            "⚠️ New date reset: leftover paper positions were cleared. "
            "Review the previous session log."
        )

    send_alert(
        f"🌅 NEW TRADING DAY\nDate: {today}\n"
        f"Strategy: CE/PE option buying\n"
        f"Mode: Paper trading only\n"
        f"Maximum trades: {MAX_DAILY_TRADES}\n"
        f"Lots per trade: {LOTS_PER_TRADE}\n"
        f"Premium target: +{TARGET_PCT * 100:.1f}%\n"
        f"Premium stop-loss: -{STOPLOSS_PCT * 100:.1f}%\n"
        f"Daily loss limit: ₹{MAX_DAILY_LOSS_RUPEES:,.0f}",
        dedupe_key=f"newday:{today}",
    )


def telegram_commands_loop():
    global last_update_id, bot_active

    while True:
        if not telegram_ready():
            time.sleep(5)
            continue

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 1},
                timeout=5,
            )

            if response.status_code == 200:
                for update in response.json().get("result", []):
                    last_update_id = max(
                        last_update_id,
                        update.get("update_id", last_update_id),
                    )

                    message = update.get("message", {})
                    sender = str(message.get("chat", {}).get("id", ""))
                    command = message.get("text", "").strip().lower()

                    if sender != CHAT_ID:
                        continue

                    if command == "/status":
                        with lock:
                            lines = [
                                f"{key}: {value['option_name']} "
                                f"entry ₹{value['entry_premium']:.2f}"
                                for key, value in open_positions.items()
                            ]
                            status = (
                                f"📊 PAPER BOT STATUS\n"
                                f"Active: {bot_active}\n"
                                f"Trades: {trades_today}/{MAX_DAILY_TRADES}\n"
                                f"Realized P&L: ₹{realized_pnl:,.2f}\n"
                                f"Open positions:\n"
                                + ("\n".join(lines) or "None")
                            )
                        send_alert(status)

                    elif command == "/pnl":
                        send_alert(
                            f"💰 PAPER P&L\n"
                            f"Trades: {trades_today}/{MAX_DAILY_TRADES}\n"
                            f"Realized P&L: ₹{realized_pnl:,.2f}\n"
                            f"Daily loss limit: ₹{MAX_DAILY_LOSS_RUPEES:,.0f}"
                        )

                    elif command == "/pause":
                        bot_active = False
                        send_alert("⏸️ New paper entries paused.")

                    elif command == "/resume":
                        bot_active = True
                        send_alert("▶️ New paper entries resumed.")

                    elif command == "/start":
                        send_alert(
                            "Commands: /status /pnl /pause /resume\n"
                            "Paper trading only; no broker orders."
                        )

        except (requests.RequestException, ValueError) as exc:
            print("Telegram command error:", exc)

        time.sleep(2)


def market_open():
    current = now()
    return (
        current.weekday() < 5
        and TRADE_START <= current.time() <= TRADE_END
    )


def main_loop():
    reset_daily_state_if_needed()

    send_alert(
        "🚀 CE/PE Paper Bot online\n"
        "Sentiment filter + option premium target/SL\n"
        "Paper trading only — no real orders.",
        dedupe_key=f"startup:{now().strftime('%Y-%m-%d')}",
    )

    while True:
        try:
            reset_daily_state_if_needed()

            current = now()

            if (
                current.weekday() >= 5
                or current.time() < TRADE_START
                or current.time() > FORCE_EXIT_TIME
            ):
                time.sleep(20)
                continue

            spots = fetch_index_spots()
            if not spots:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            chains = {}
            for key in WATCHLIST:
                chains[key] = fetch_option_chain(key)
                time.sleep(1)

            track_positions(chains)

            if current.time() >= FORCE_EXIT_TIME:
                for key, position in list(open_positions.items()):
                    chain = chains.get(key)
                    if not chain:
                        continue

                    row = next(
                        (
                            item for item in chain["rows"]
                            if item["strike"] == position["strike"]
                            and item["expiry"] == position["expiry"]
                        ),
                        None,
                    )
                    leg = row.get(position["direction"]) if row else None

                    if leg and leg["premium"] > 0:
                        close_position(key, leg["premium"], "TIME_EXIT")

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if not market_open() or not bot_active:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            with lock:
                if not can_open_new_trade():
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue

            for key, spot in spots.items():
                with lock:
                    if key in open_positions or not can_open_new_trade():
                        continue

                sentiment = build_sentiment(key, spot)
                chain = chains.get(key)

                if sentiment and chain:
                    try_open_position(key, spot, sentiment, chain)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as exc:
            print("Main loop error:", exc)
            time.sleep(10)


if __name__ == "__main__":
    threading.Thread(
        target=telegram_commands_loop,
        daemon=True,
    ).start()

    threading.Thread(
        target=main_loop,
        daemon=True,
    ).start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
