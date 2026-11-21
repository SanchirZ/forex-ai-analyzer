"""
Telegram alerts for new BUY/SELL signals.

Runs every 5 minutes from GitHub Actions (.github/workflows/signals.yml).
Uses the same data sources (main.get_candles) and the same rules as the
website chart:

  signal  : EMA20/EMA50 trend + MACD(12,26,9) + RSI(14) agree, on the
            closed candle where all three first agree
  filter  : H1 EMA20/EMA50 trend must point the same way
  levels  : SL = ATR(14) x 1.5, TP1/TP2/TP3 = 1R/2R/3R

Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (GitHub repository secrets)
  ALERT_STATE   path of the JSON file that remembers sent alerts
  ALERT_DRY_RUN "1" to print instead of sending
"""
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402  (reuses the backend's data functions)

WATCH = [("btc", "5m"), ("btc", "15m"), ("xau", "5m"), ("xau", "15m")]
TF_SECONDS = {"5m": 300, "15m": 900}
SITE = "https://sanchirz.github.io/forex-ai-analyzer/"
STATE_PATH = os.getenv("ALERT_STATE", "alert_state.json")


def ema(values, period):
    a = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def rsi_series(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    gain /= period
    loss /= period

    def value():
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        return 100 - 100 / (1 + gain / loss)

    out[period] = value()
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
        out[i] = value()
    return out


def atr_series(p, period=14):
    tr = []
    for i, c in enumerate(p):
        if i == 0:
            tr.append(c["h"] - c["l"])
        else:
            prev = p[i - 1]["c"]
            tr.append(max(c["h"] - c["l"], abs(c["h"] - prev), abs(c["l"] - prev)))
    out = [None] * len(p)
    s = 0.0
    for i in range(len(p)):
        s += tr[i]
        if i >= period:
            s -= tr[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def conditions(e20, e50, macd, sig, r, i):
    if r[i] is None:
        return 0
    if e20[i] > e50[i] and macd[i] > sig[i] and 50 < r[i] < 75:
        return 1
    if e20[i] < e50[i] and macd[i] < sig[i] and 25 < r[i] < 50:
        return -1
    return 0


def h1_trend(h1, at_time):
    """Trend of the last H1 candle that had closed by `at_time`."""
    closes = [c["c"] for c in h1]
    a, b = ema(closes, 20), ema(closes, 50)
    k = -1
    for i, c in enumerate(h1):
        if c["t"] + 3600 <= at_time:
            k = i
    if k < 50:
        return 0
    return 1 if a[k] > b[k] else -1


def latest_signal(points, h1, tf):
    """Signal on the last closed candle, or None."""
    sec = TF_SECONDS[tf]
    now = time.time()
    # drop the candle that is still forming
    if points and points[-1]["t"] + sec > now:
        points = points[:-1]
    if len(points) < 60:
        return None

    closes = [c["c"] for c in points]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    e26 = ema(closes, 26)
    macd = [x - y for x, y in zip(ema(closes, 12), e26)]
    sig = ema(macd, 9)
    r = rsi_series(closes)
    atr = atr_series(points)

    i = len(points) - 1
    side = conditions(e20, e50, macd, sig, r, i)
    if side == 0 or side == conditions(e20, e50, macd, sig, r, i - 1):
        return None                       # not a fresh signal
    candle = points[i]
    if now - (candle["t"] + sec) > 2 * sec:
        return None                       # too old (e.g. after a pause)

    trend = h1_trend(h1, candle["t"] + sec)
    if trend == 0 or trend != side:
        return None                       # against (or unknown) H1 trend

    entry = candle["c"]
    risk = max(atr[i] * 1.5, 1e-9)
    return {
        "side": "BUY" if side > 0 else "SELL",
        "time": candle["t"],
        "entry": entry,
        "sl": entry - side * risk,
        "tps": [entry + side * risk * k for k in (1, 2, 3)],
        "risk": risk,
        "rsi": r[i],
    }


def fmt(v):
    return f"{v:,.2f}"


def message(symbol, tf, s):
    name = main.SYMBOLS[symbol]["name"]
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    return (
        f"{icon} <b>{name} {tf.upper()} {s['side']}</b>\n"
        f"Entry: {fmt(s['entry'])}\n"
        f"Stop loss: {fmt(s['sl'])} ({fmt(s['risk'])}$ зай)\n"
        f"TP1: {fmt(s['tps'][0])}\n"
        f"TP2: {fmt(s['tps'][1])}  ← манай дүрмийн зорилт (2R)\n"
        f"TP3: {fmt(s['tps'][2])}\n"
        f"H1 чиглэл: {'өсөх ↑' if s['side'] == 'BUY' else 'буурах ↓'} · RSI {s['rsi']:.0f}\n\n"
        f"Лотоо сайтын тооцоолуураар тогтооно уу (1% эрсдэл):\n{SITE}\n"
        f"<i>Сигнал баталгаа биш. Stop loss-оо заавал тавина.</i>"
    )


def send(text):
    if os.getenv("ALERT_DRY_RUN") == "1":
        print("DRY RUN:\n" + text)
        return True
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are not set")
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:300])
    return r.ok


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def run():
    state = load_state()
    h1_cache = {}
    sent = 0
    for symbol, tf in WATCH:
        try:
            if symbol not in h1_cache:
                h1 = main.get_candles("1h", symbol)
                h1_cache[symbol] = h1[:-1]       # closed H1 candles only
            points = main.get_candles(tf, symbol)
            s = latest_signal(points, h1_cache[symbol], tf)
        except Exception as exc:                  # one market failing must not stop the others
            print(f"{symbol} {tf}: data error {type(exc).__name__}: {exc}")
            continue

        key = f"{symbol}-{tf}"
        if not s:
            print(f"{key}: no new signal")
            continue
        if state.get(key) == s["time"]:
            print(f"{key}: already sent")
            continue
        if send(message(symbol, tf, s)):
            state[key] = s["time"]
            sent += 1
            print(f"{key}: sent {s['side']}")
    save_state(state)
    print(f"done, {sent} alert(s) sent")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        ok = send("✅ Forex AI Analyzer: Telegram мэдэгдэл ажиллаж байна.\n"
                  "Шинэ BUY/SELL сигнал гарахад энд мессеж ирнэ (BTCUSD, XAUUSD · M5, M15).")
        sys.exit(0 if ok else 1)
    run()
