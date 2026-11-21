
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import json
import math
import time
import threading
from datetime import datetime, timezone
app = FastAPI(title="Forex AI Analyzer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sanchirz.github.io"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
CHART_URL = "https://xaus.com/api/v1/chart"
PRICE_URL = "https://xaus.com/api/v1/spot?compact=1"
COINBASE_URL = "https://api.exchange.coinbase.com/products/BTC-USD"
HTTP_HEADERS = {"User-Agent": "forex-ai-analyzer/1.0", "Accept": "application/json"}
# XAUS sometimes refuses unknown clients; fall back to a browser-like and a plain request
XAUS_HEADER_SETS = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
     "Accept": "application/json,text/plain,*/*", "Referer": "https://xaus.com/"},
    None,
    HTTP_HEADERS,
]
CACHE_TTL = {"1m": 15, "5m": 30, "15m": 60, "30m": 60, "1h": 120, "4h": 300, "1d": 600}
_candle_cache = {}
_price_cache = {}
_backoff = {}          # symbol -> time until which we do not call the source
_cache_lock = threading.Lock()
PRICE_TTL = {"xau": 5, "btc": 2}

APP_VERSION = "2026.11.22-ratelimit"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
# M1 was dropped: backtests showed spread costs turn its signals into losses
ANALYZE_TFS = ["5m", "15m", "1h"]
SYMBOLS = {
    "xau": {"name": "XAUUSD", "source": "XAUS"},
    "btc": {"name": "BTCUSD", "source": "Coinbase"},
}


def pages_needed(interval, limit):
    """How many 300-candle Coinbase pages are needed for `limit` candles."""
    factor = {"30m": 2, "4h": 4}.get(interval, 1)
    return math.ceil(limit * factor / 300)


def check_symbol(symbol):
    symbol = (symbol or "xau").lower()
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=400, detail="Use symbol=xau or symbol=btc")
    return symbol


def aggregate(points, seconds):
    """Merge smaller candles into bigger ones (e.g. 1h -> 4h)."""
    buckets = {}
    for p in points:
        bucket_time = (int(p["t"]) // seconds) * seconds
        buckets.setdefault(bucket_time, []).append(p)

    result = []
    for timestamp, group in sorted(buckets.items()):
        group.sort(key=lambda x: x["t"])
        result.append({
            "t": timestamp,
            "o": float(group[0]["o"]),
            "h": max(float(x["h"]) for x in group),
            "l": min(float(x["l"]) for x in group),
            "c": float(group[-1]["c"]),
            "v": sum(float(x.get("v", 0) or 0) for x in group)
        })
    return result


def get_xau_candles(interval):
    ranges = {
        "1m": "5d", "5m": "5d", "15m": "5d", "30m": "1mo",
        "1h": "3mo", "4h": "3mo", "1d": "1y"
    }
    source_interval = "1h" if interval == "4h" else interval

    params = {"symbol": "xau", "range": ranges[interval], "interval": source_interval}
    last_error = None
    points = None
    for headers in XAUS_HEADER_SETS:
        try:
            response = requests.get(CHART_URL, params=params, headers=headers, timeout=20)
            response.raise_for_status()
            points = response.json().get("points", [])
            if points:
                break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    if not points:
        raise ValueError(f"XAUS chart unavailable: {last_error}")

    if interval == "4h":
        points = aggregate(points, 14400)
    return points


def get_btc_candles(interval, pages=1):
    # Coinbase supports 60, 300, 900, 3600, 21600, 86400 seconds (max 300 candles)
    granularity = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 900,
        "1h": 3600, "4h": 3600, "1d": 86400
    }[interval]

    rows = []
    end = int(time.time())
    for _ in range(max(1, min(pages, 6))):
        start = end - granularity * 300
        params = {"granularity": granularity}
        if pages > 1:
            params["start"] = datetime.fromtimestamp(start, timezone.utc).isoformat()
            params["end"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
        response = requests.get(
            f"{COINBASE_URL}/candles",
            params=params,
            headers=HTTP_HEADERS,
            timeout=20
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        end = start

    # drop duplicates that can appear at page edges
    rows = list({int(r[0]): r for r in rows}.values())

    # each row: [time, low, high, open, close, volume], newest first
    points = sorted(
        [
            {"t": int(r[0]), "l": float(r[1]), "h": float(r[2]),
             "o": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in rows
        ],
        key=lambda x: x["t"]
    )

    if interval == "30m":
        points = aggregate(points, 1800)
    elif interval == "4h":
        points = aggregate(points, 14400)
    return points


def get_candles(interval="5m", symbol="xau", pages=1):
    if interval not in INTERVALS:
        raise ValueError("Unsupported timeframe")

    # short cache: many visitors and the alert job ask for the same candles
    key = (symbol, interval, pages)
    now = time.time()
    with _cache_lock:
        hit = _candle_cache.get(key)
        fresh = hit and now - hit[0] < CACHE_TTL[interval]
        cooling = now < _backoff.get(symbol, 0)
        if fresh or (hit and cooling):
            return [dict(p) for p in hit[1]]
    if cooling:
        raise ValueError(f"{symbol} source is rate-limiting, retry shortly")
    try:
        points = _fetch_candles(interval, symbol, pages)
    except Exception as exc:
        if "429" in str(exc):
            _backoff[symbol] = time.time() + 90      # the source asked us to slow down
        if hit and now - hit[0] < 3600:              # serve the last good candles
            return [dict(p) for p in hit[1]]
        raise
    with _cache_lock:
        _candle_cache[key] = (time.time(), points)
    return [dict(p) for p in points]


def _fetch_candles(interval, symbol, pages):
    points = get_btc_candles(interval, pages) if symbol == "btc" else get_xau_candles(interval)

    if len(points) < 50:
        raise ValueError("Not enough candle data")
    return points


def get_price(symbol="xau"):
    """Cached a few seconds; on errors the last price (up to 10 min old) is returned."""
    now = time.time()
    with _cache_lock:
        hit = _price_cache.get(symbol)
        if hit and (now - hit[0] < PRICE_TTL[symbol] or now < _backoff.get(symbol, 0)):
            return dict(hit[1])
    try:
        data = _fetch_price(symbol)
    except Exception as exc:
        if "429" in str(exc):
            _backoff[symbol] = time.time() + 90
        if hit and now - hit[0] < 600:
            return dict(hit[1])
        raise
    with _cache_lock:
        _price_cache[symbol] = (time.time(), data)
    return dict(data)


def _fetch_price(symbol):
    if symbol == "btc":
        response = requests.get(f"{COINBASE_URL}/ticker", headers=HTTP_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        return {"price": float(data["price"]), "price_as_of": data.get("time")}

    response = requests.get(PRICE_URL, headers=XAUS_HEADER_SETS[0], timeout=10)
    response.raise_for_status()
    data = response.json()
    return {"price": data.get("spot_usd_oz"), "price_as_of": data.get("price_as_of")}


def ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]

    for value in values[1:]:
        result.append(
            alpha * value + (1 - alpha) * result[-1]
        )

    return result


def rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze_timeframe(interval, points=None, symbol="xau"):
    if points is None:
        points = get_candles(interval, symbol)
    closes = [float(p["c"]) for p in points]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    macd_line = [
        fast - slow
        for fast, slow in zip(
            ema(closes, 12),
            ema(closes, 26)
        )
    ]

    signal_line = ema(macd_line, 9)

    current_rsi = rsi(closes)
    current_macd = macd_line[-1]
    current_signal = signal_line[-1]

    if (
        ema20[-1] > ema50[-1]
        and current_macd > current_signal
        and 50 < current_rsi < 70
    ):
        signal = "BUY"

    elif (
        ema20[-1] < ema50[-1]
        and current_macd < current_signal
        and 30 < current_rsi < 50
    ):
        signal = "SELL"

    else:
        signal = "WAIT"

    return {
        "timeframe": interval,
        "price": round(closes[-1], 2),
        "signal": signal,
        "indicators": {
            "RSI": round(current_rsi, 2),
            "EMA20": round(ema20[-1], 2),
            "EMA50": round(ema50[-1], 2),
            "MACD": round(current_macd, 4),
            "MACD_signal": round(current_signal, 4)
        }
    }


@app.get("/")
def home():
    return {
        "app": "Forex AI Analyzer",
        "status": "online",
        "version": APP_VERSION,
        "symbols": [v["name"] for v in SYMBOLS.values()]
    }


NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_news_cache = {"t": 0, "items": [], "fail_t": 0}


def get_news():
    """High-impact USD events of this week (Forex Factory calendar), cached 30 min."""
    with _cache_lock:
        if time.time() - _news_cache["t"] < 3 * 3600 or time.time() - _news_cache["fail_t"] < 900:
            return list(_news_cache["items"])
    items = []
    try:
        r = requests.get(NEWS_URL, headers=XAUS_HEADER_SETS[0], timeout=15)
        r.raise_for_status()
        for ev in r.json():
            if ev.get("impact") != "High" or ev.get("country") != "USD":
                continue
            try:
                ts = int(datetime.fromisoformat(ev["date"]).timestamp())
            except (KeyError, ValueError, TypeError):
                continue
            items.append({
                "title": ev.get("title", ""),
                "country": "USD",
                "time": ts,
                "forecast": ev.get("forecast", ""),
                "previous": ev.get("previous", ""),
            })
        items.sort(key=lambda x: x["time"])
    except (requests.RequestException, ValueError) as exc:
        print("news unavailable:", type(exc).__name__, exc)
        with _cache_lock:
            _news_cache["fail_t"] = time.time()      # wait 15 min before asking again
            if _news_cache["items"]:
                return list(_news_cache["items"])
        return []
    with _cache_lock:
        _news_cache.update(t=time.time(), items=items)
    return items


def news_near(ts, window_min=30):
    """First high-impact event within +-window minutes of ts, or None."""
    for ev in get_news():
        if abs(ev["time"] - ts) <= window_min * 60:
            return ev
    return None


@app.get("/news")
def news():
    return {"events": get_news(), "source": "Forex Factory (High impact, USD)"}


@app.get("/health")
def health():
    return {"status": "healthy", "version": APP_VERSION}


@app.get("/price")
def price(symbol: str = "xau"):
    symbol = check_symbol(symbol)
    try:
        data = get_price(symbol)
        return {
            "symbol": SYMBOLS[symbol]["name"],
            "price": data["price"],
            "currency": "USD",
            "source": SYMBOLS[symbol]["source"],
            "price_as_of": data["price_as_of"]
        }
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=502, detail="Price unavailable")


@app.get("/candles")
def candles(interval: str = "5m", symbol: str = "xau", limit: int = 200):
    symbol = check_symbol(symbol)
    limit = max(50, min(int(limit), 1500))
    if interval not in INTERVALS:
        raise HTTPException(
            status_code=400,
            detail="Use 1m, 5m, 15m, 30m, 1h, 4h or 1d"
        )

    try:
        return {
            "symbol": SYMBOLS[symbol]["name"],
            "timeframe": interval,
            "points": get_candles(interval, symbol, pages_needed(interval, limit))[-limit:]
        }
    except Exception as exc:
        print(f"candles {symbol} {interval} failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=502,
            detail=f"Candle data unavailable ({type(exc).__name__})"
        )


@app.get("/analyze")
def analyze(symbol: str = "xau"):
    symbol = check_symbol(symbol)
    results = {}

    for interval in ANALYZE_TFS:
        try:
            results[interval] = analyze_timeframe(interval, symbol=symbol)
        except Exception:
            results[interval] = {
                "signal": "UNAVAILABLE",
                "error": "Data unavailable"
            }

    signals = [
        results[tf]["signal"]
        for tf in ANALYZE_TFS
        if "signal" in results[tf]
    ]

    if len(signals) != 3:
        overall = "WAIT"
        reason = "Зарим timeframe-ийн мэдээлэл дутуу байна."

    elif signals.count("BUY") >= 2 and "SELL" not in signals:
        overall = "BUY"
        reason = "Олон timeframe өсөх чиглэлийг дэмжиж байна."

    elif signals.count("SELL") >= 2 and "BUY" not in signals:
        overall = "SELL"
        reason = "Олон timeframe буурах чиглэлийг дэмжиж байна."

    else:
        overall = "WAIT"
        reason = "Timeframe-үүдийн дохио зөрүүтэй байна."

    return {
        "symbol": SYMBOLS[symbol]["name"],
        "overall_signal": overall,
        "timeframes": results,
        "reason": reason,
        "data_source": SYMBOLS[symbol]["source"],
        "confidence": None
    }
def get_atr(points, period=14):
    if len(points) < period + 1:
        return None

    ranges = []
    for i in range(1, len(points)):
        high = float(points[i]["h"])
        low = float(points[i]["l"])
        prev = float(points[i - 1]["c"])
        ranges.append(max(high - low, abs(high - prev), abs(low - prev)))

    return round(sum(ranges[-period:]) / period, 4)


AI_LOCK = threading.Lock()
AI_CACHE = {}          # symbol -> (timestamp, response)
AI_CALLS = {}          # client ip -> last call timestamp
AI_HOURLY = []         # timestamps of real OpenAI calls
AI_CACHE_SECONDS = 60
AI_IP_COOLDOWN = 30
AI_MAX_PER_HOUR = 40


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.get("/ai-analyze")
def ai_analyze(request: Request, symbol: str = "xau"):
    symbol = check_symbol(symbol)
    now = time.time()
    ip = client_ip(request)

    with AI_LOCK:
        cached = AI_CACHE.get(symbol)
        if cached and now - cached[0] < AI_CACHE_SECONDS:
            return {**cached[1], "cached": True}

        if now - AI_CALLS.get(ip, 0) < AI_IP_COOLDOWN:
            raise HTTPException(
                status_code=429,
                detail="Хэт олон хүсэлт. 30 секундын дараа дахин оролдоно уу."
            )

        AI_HOURLY[:] = [t for t in AI_HOURLY if now - t < 3600]
        if len(AI_HOURLY) >= AI_MAX_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="AI шинжилгээний цагийн хязгаар дууслаа. Дараа дахин оролдоно уу."
            )

        AI_CALLS[ip] = now
        AI_HOURLY.append(now)

    result = run_ai_analysis(symbol)
    with AI_LOCK:
        AI_CACHE[symbol] = (time.time(), result)
    return result


def run_ai_analysis(symbol):
    name = SYMBOLS[symbol]["name"]
    source = SYMBOLS[symbol]["source"]
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI API түлхүүр тохируулаагүй байна."
        )

    try:
        market = {}

        for interval in ANALYZE_TFS:
            points = get_candles(interval, symbol)

            market[interval] = {
                "analysis": analyze_timeframe(interval, points),
                "atr14": get_atr(points),
                "recent_candles": [
                    {
                        "open": p["o"],
                        "high": p["h"],
                        "low": p["l"],
                        "close": p["c"]
                    }
                    for p in points[-12:]
                ]
            }

        price_data = get_price(symbol)

        market_data = {
            "symbol": name,
            "price": price_data.get("price"),
            "price_as_of": price_data.get("price_as_of"),
            "source": source,
            "timeframes": market
        }

        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL", "WAIT"]
                },
                "confidence_percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100
                },
                "summary": {"type": "string"},
                "reasons": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "entry_price": {
                    "anyOf": [
                        {"type": "number"},
                        {"type": "null"}
                    ]
                },
                "stop_loss": {
                    "anyOf": [
                        {"type": "number"},
                        {"type": "null"}
                    ]
                },
                "take_profit": {
                    "anyOf": [
                        {"type": "number"},
                        {"type": "null"}
                    ]
                },
                "risk_reward": {"type": "string"},
                "risk_level": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH"]
                },
                "invalidation": {"type": "string"},
                "caution": {"type": "string"}
            },
            "required": [
                "action",
                "confidence_percent",
                "summary",
                "reasons",
                "entry_price",
                "stop_loss",
                "take_profit",
                "risk_reward",
                "risk_level",
                "invalidation",
                "caution"
            ],
            "additionalProperties": False
        }

        instructions = f"""
You are a careful {name} scalping analysis assistant.
Analyze the supplied 5m, 15m and 1h candles and indicators.
Respond in Mongolian.

Compare all three timeframes. If signals conflict,
data is incomplete, or the setup is unclear, choose WAIT.

Never invent news, spread, commission or broker prices.
Only provide entry, stop loss and take profit when a
defensible setup exists. Otherwise return null for all three.

For BUY: stop loss below entry and take profit above entry.
For SELL: stop loss above entry and take profit below entry.

Confidence is a qualitative AI estimate, NOT a proven
win probability. Explain the reasons and risks.
The {source} price is not the exact XM execution quote.
Never promise profit.
"""

        ai_response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": os.getenv("OPENAI_MODEL", "gpt-5.5"),
                "instructions": instructions,
                "input": json.dumps(market_data, ensure_ascii=False),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "scalping_analysis",
                        "strict": True,
                        "schema": schema
                    }
                },
                "max_output_tokens": 900
            },
            timeout=45
        )

        ai_response.raise_for_status()
        response_data = ai_response.json()

        output_text = ""

        for item in response_data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        output_text += part.get("text", "")

        if not output_text:
            raise ValueError("AI response is empty")

        result = json.loads(output_text)

        return {
            "symbol": name,
            "data_source": source,
            "broker_quote": False,
            "price_as_of": price_data.get("price_as_of"),
            "analysis": result,
            "disclaimer": "AI-ийн дүгнэлт баталгаатай арилжааны дохио биш."
        }

  
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:1000] if exc.response is not None else str(exc)
        print(f"OpenAI HTTP error {status}: {body}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="AI үйлчилгээний хүсэлт амжилтгүй боллоо."
        )

    except requests.RequestException as exc:
        print(f"AI/network error: {type(exc).__name__}: {str(exc)[:500]}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="Зах зээлийн өгөгдөл эсвэл AI хариунд алдаа гарлаа."
        )

    except (ValueError, KeyError, TypeError) as exc:
        print(f"AI data error: {type(exc).__name__}: {str(exc)[:500]}", flush=True)
        raise HTTPException(
            status_code=502,
            detail="Зах зээлийн өгөгдөл эсвэл AI хариунд алдаа гарлаа."
        )
