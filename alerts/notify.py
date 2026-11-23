"""
Live signal journal + Telegram alerts.

Runs every 5 minutes from GitHub Actions (.github/workflows/signals.yml).
Uses the backend's data functions (main.get_candles / main.get_news) and the
same rules as the website chart in "active" mode:

  signal  : EMA20/EMA50 trend + MACD(12,26,9) + RSI(14) agree, on the
            closed candle where all three first agree
  filter  : H1 EMA20/EMA50 trend must point the same way
  levels  : SL just beyond the last swing (10 candles) +-0.25 ATR, 0.75-3.5 ATR
            TP1 = 1R: close half, move SL to entry; TP2 = 3R: close the rest

Every signal is written to a journal (forward test) and its result is
followed until TP2 (+2R in total), breakeven after TP1 (+0.5R), SL (-1R) or
48 hours. Spread is subtracted.
The journal lives on the `data` branch so it never redeploys the site.

Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional; without them only the journal runs)
  JOURNAL        path of journal.json   (default data/journal.json)
  ALERT_DRY_RUN  "1" to print messages instead of sending
"""
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

WATCH = [("btc", "5m"), ("btc", "15m"), ("xau", "5m"), ("xau", "15m")]
TF_SECONDS = {"5m": 300, "15m": 900}
COST = {"btc": 25.0, "xau": 0.35}           # approx. spread per trade, in price units
LOOKBACK = 90 * 60                           # scan this far back (GitHub may delay runs)
ALERT_MAX_AGE = 30 * 60                      # do not alert signals older than this
EXPIRE = 48 * 3600
SITE = "https://sanchirz.github.io/forex-ai-analyzer/"
JOURNAL = os.getenv("JOURNAL", "data/journal.json")
NEWS_FILE = os.path.join(os.path.dirname(JOURNAL) or ".", "news.json")


def load_news():
    """High-impact USD events, refreshed at most once an hour into data/news.json."""
    try:
        with open(NEWS_FILE, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached.get("updated", 0) < 3600 and cached.get("events"):
            return cached["events"]
    except (OSError, ValueError):
        cached = {"events": []}
    events = main.get_news()
    if events:
        os.makedirs(os.path.dirname(NEWS_FILE) or ".", exist_ok=True)
        with open(NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": int(time.time()), "source": "Forex Factory (High, USD)",
                       "events": events}, f, ensure_ascii=False, indent=1)
        return events
    return cached.get("events", [])


def news_near(events, ts, window_min=30):
    for ev in events:
        if abs(ev["time"] - ts) <= window_min * 60:
            return ev
    return None


# ---------------------------------------------------------------- indicators
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


def structure_stop(points, i, side, atr_value):
    lows = [c["l"] for c in points[max(0, i - 9):i + 1]]
    highs = [c["h"] for c in points[max(0, i - 9):i + 1]]
    entry = points[i]["c"]
    a = atr_value or abs(points[i]["h"] - points[i]["l"]) or 1.0
    ext = min(lows) if side > 0 else max(highs)
    dist = (entry - (ext - side * 0.25 * a)) * side
    dist = min(max(dist, 0.75 * a), 3.5 * a)
    return entry - side * dist


def h1_trend_fn(h1):
    closes = [c["c"] for c in h1]
    a, b = ema(closes, 20), ema(closes, 50)

    def trend(at_time):
        k = -1
        for i, c in enumerate(h1):
            if c["t"] + 3600 <= at_time:
                k = i
        if k < 50:
            return 0
        return 1 if a[k] > b[k] else -1
    return trend


# ---------------------------------------------------------------- signals
def fresh_signals(points, h1, tf, since):
    """All fresh signals on closed candles that closed after `since`."""
    sec = TF_SECONDS[tf]
    now = time.time()
    if points and points[-1]["t"] + sec > now:
        points = points[:-1]                 # drop the forming candle
    if len(points) < 60:
        return []

    closes = [c["c"] for c in points]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    e26 = ema(closes, 26)
    macd = [x - y for x, y in zip(ema(closes, 12), e26)]
    sig = ema(macd, 9)
    r = rsi_series(closes)
    atr = atr_series(points)
    trend = h1_trend_fn(h1)

    def cond(i):
        if r[i] is None:
            return 0
        if e20[i] > e50[i] and macd[i] > sig[i] and 50 < r[i] < 75:
            return 1
        if e20[i] < e50[i] and macd[i] < sig[i] and 25 < r[i] < 50:
            return -1
        return 0

    out = []
    for i in range(60, len(points)):
        close_time = points[i]["t"] + sec
        if close_time <= since:
            continue
        side = cond(i)
        if side == 0 or side == cond(i - 1):
            continue
        h = trend(close_time)
        if h == 0 or h != side:
            continue
        entry = points[i]["c"]
        sl = structure_stop(points, i, side, atr[i])
        risk = abs(entry - sl)
        out.append({
            "t": points[i]["t"],
            "close_t": close_time,
            "dir": side,
            "side": "BUY" if side > 0 else "SELL",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tps": [round(entry + side * risk * k, 2) for k in (1, 2, 3)],
            "risk": risk,
            "rsi": r[i],
        })
    return out


def follow(sig, points, sec, cost):
    """Update an open journal entry: half at TP1 (1R) then stop at entry, rest at TP2 (3R)."""
    d = 1 if sig["side"] == "BUY" else -1
    entry, sl, tp1, tp2 = sig["entry"], sig["sl"], sig["tps"][0], sig["tps"][2]
    risk = abs(entry - sl) or 1e-9
    after = [c for c in points if c["t"] >= sig["t"] + sec and c["t"] + sec <= time.time()]
    half = False                                  # replayed from the candles every run
    for c in after:
        stop = entry if half else sl
        if (d > 0 and c["l"] <= stop) or (d < 0 and c["h"] >= stop):   # stop first (conservative)
            if half:
                sig.update(status="BE", closed_t=c["t"] + sec, r=round(0.5 - cost / risk, 2), tp1_hit=True)
            else:
                sig.update(status="SL", closed_t=c["t"] + sec, r=round(-1 - cost / risk, 2), tp1_hit=False)
            return
        if not half and ((d > 0 and c["h"] >= tp1) or (d < 0 and c["l"] <= tp1)):
            half = True
        if half and ((d > 0 and c["h"] >= tp2) or (d < 0 and c["l"] <= tp2)):
            sig.update(status="TP2", closed_t=c["t"] + sec, r=round(2 - cost / risk, 2), tp1_hit=True)
            return
    sig["tp1_hit"] = half
    if after and time.time() - sig["close_t"] > EXPIRE:
        last = after[-1]["c"]
        open_r = (last - entry) * d / risk
        r = 0.5 + 0.5 * open_r if half else open_r
        sig.update(status="EXPIRED", closed_t=after[-1]["t"] + sec, r=round(r - cost / risk, 2))


# ---------------------------------------------------------------- telegram
def fmt(v):
    return f"{v:,.2f}"


def message(symbol, tf, s, news):
    name = main.SYMBOLS[symbol]["name"]
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    lines = [
        f"{icon} <b>{name} {tf.upper()} {s['side']}</b>",
        f"Entry: {fmt(s['entry'])}",
        f"Stop loss: {fmt(s['sl'])}  (сүүлийн swing-ийн цаана, {fmt(s['risk'])}$)",
        f"TP1: {fmt(s['tps'][0])}  → хагасыг хааж, SL-ээ {fmt(s['entry'])} (entry) рүү зөө",
        f"TP2: {fmt(s['tps'][2])}  → үлдсэнийг хаа (3R)",
        f"H1 чиглэл: {'өсөх ↑' if s['side'] == 'BUY' else 'буурах ↓'} · RSI {s['rsi']:.0f}",
    ]
    if news:
        when = time.strftime("%H:%M UTC", time.gmtime(news["time"]))
        lines.append(f"\n⚠️ <b>Мэдээ ойрхон:</b> {news['title']} ({when}). Энэ сигналыг алгасахыг зөвлөж байна.")
    if time.time() - s["close_t"] > 10 * 60:
        lines.append(f"\n⏱ Хоцорсон мэдэгдэл ({int((time.time() - s['close_t']) / 60)} мин). Үнэ entry-ээс холдсон эсэхийг шалга.")
    lines.append(f"\nЛотоо сайтын тооцоолуураар (1% эрсдэл):\n{SITE}")
    lines.append("<i>Сигнал баталгаа биш. Stop loss-оо заавал тавина.</i>")
    return "\n".join(lines)


def send(text):
    if os.getenv("ALERT_DRY_RUN") == "1":
        print("DRY RUN:\n" + text)
        return True
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
    except requests.RequestException as exc:
        print("Telegram error:", exc)
        return False
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:300])
    return r.ok


# ---------------------------------------------------------------- journal
def load_journal():
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data
    except (OSError, ValueError):
        pass
    return {"started": int(time.time()), "signals": []}


def save_journal(data):
    os.makedirs(os.path.dirname(JOURNAL) or ".", exist_ok=True)
    data["updated"] = int(time.time())
    data["signals"] = data["signals"][-500:]
    with open(JOURNAL, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def run():
    journal = load_journal()
    known = {s["id"]: s for s in journal["signals"]}
    try:
        events = load_news()
    except Exception as exc:
        print("news failed:", exc)
        events = []
    print(f"news: {len(events)} high-impact USD events")
    h1_cache = {}
    added = alerted = 0
    now = time.time()

    for symbol, tf in WATCH:
        sec = TF_SECONDS[tf]
        try:
            if symbol not in h1_cache:
                h1_cache[symbol] = main.get_candles("1h", symbol)[:-1]   # closed H1 candles
            points = main.get_candles(tf, symbol)
        except Exception as exc:
            print(f"{symbol} {tf}: data error {type(exc).__name__}: {exc}")
            continue

        # 1) follow open signals of this market
        for s in journal["signals"]:
            if s["sym"] == symbol and s["tf"] == tf and s["status"] == "open":
                follow(s, points, sec, COST[symbol])

        # 2) record new signals
        for s in fresh_signals(points, h1_cache[symbol], tf, now - LOOKBACK):
            sid = f"{symbol}-{tf}-{s['t']}"
            if sid in known:
                continue
            news = news_near(events, s["close_t"])
            entry = {
                "id": sid, "sym": symbol, "name": main.SYMBOLS[symbol]["name"], "tf": tf,
                "side": s["side"], "t": s["t"], "close_t": s["close_t"],
                "entry": s["entry"], "sl": s["sl"], "tps": s["tps"],
                "status": "open", "r": None, "news": news["title"] if news else None,
                "alerted": False,
            }
            follow(entry, points, sec, COST[symbol])
            if now - s["close_t"] <= ALERT_MAX_AGE and entry["status"] == "open":
                entry["alerted"] = send(message(symbol, tf, s, news))
                alerted += entry["alerted"]
            journal["signals"].append(entry)
            known[sid] = entry
            added += 1
            print(f"{sid}: {s['side']} recorded{' + alert' if entry['alerted'] else ''}")

    journal["signals"].sort(key=lambda s: s["close_t"])
    save_journal(journal)
    done = [s for s in journal["signals"] if s["status"] != "open"]
    total = sum(s["r"] or 0 for s in done)
    print(f"done: {added} new, {alerted} alerted, {len(done)} closed, total {total:+.1f}R")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        ok = send("✅ Forex AI Analyzer: Telegram мэдэгдэл ажиллаж байна.\n"
                  "Шинэ BUY/SELL сигнал гарахад энд мессеж ирнэ (BTCUSD, XAUUSD · M5, M15).")
        sys.exit(0 if ok else 1)
    run()
