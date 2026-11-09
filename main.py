from fastapi import FastAPI, HTTPException
import requests
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(title="Forex AI Analyzer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
CHART_URL = "https://xaus.com/api/v1/chart"
PRICE_URL = "https://xaus.com/api/v1/spot?compact=1"


def get_candles(interval="5m"):
    response = requests.get(
        CHART_URL,
        params={
            "symbol": "xau",
            "range": "5d",
            "interval": interval
        },
        timeout=20
    )
    response.raise_for_status()

    points = response.json().get("points", [])

    if len(points) < 50:
        raise ValueError("Not enough candle data")

    return points


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


def analyze_timeframe(interval):
    points = get_candles(interval)
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
        "status": "online"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/price")
def gold_price():
    try:
        response = requests.get(PRICE_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        return {
            "symbol": "XAUUSD",
            "price": data.get("spot_usd_oz"),
            "currency": "USD",
            "source": "XAUS",
            "price_as_of": data.get("price_as_of")
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Gold price unavailable"
        )


@app.get("/candles")
def candles(interval: str = "5m"):
    if interval not in ["1m", "5m", "15m"]:
        raise HTTPException(
            status_code=400,
            detail="Use 1m, 5m or 15m"
        )

    try:
        return {
            "symbol": "XAUUSD",
            "timeframe": interval,
            "points": get_candles(interval)[-20:]
        }
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Candle data unavailable"
        )


@app.get("/analyze")
def analyze():
    results = {}

    for interval in ["1m", "5m", "15m"]:
        try:
            results[interval] = analyze_timeframe(interval)
        except Exception:
            results[interval] = {
                "signal": "UNAVAILABLE",
                "error": "Data unavailable"
            }

    signals = [
        results[tf]["signal"]
        for tf in ["1m", "5m", "15m"]
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
        "symbol": "XAUUSD",
        "overall_signal": overall,
        "timeframes": results,
        "reason": reason,
        "data_source": "XAUS",
        "confidence": None
    }
