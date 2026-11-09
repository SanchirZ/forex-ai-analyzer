from fastapi import FastAPI, HTTPException
import requests

app = FastAPI(title="Forex AI Analyzer")

CHART_URL = "https://xaus.com/api/v1/chart"
PRICE_URL = "https://xaus.com/api/v1/spot?compact=1"


def get_candles():
    response = requests.get(
        CHART_URL,
        params={
            "symbol": "xau",
            "range": "5d",
            "interval": "5m"
        },
        timeout=15
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

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


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
def candles():
    try:
        return {
            "symbol": "XAUUSD",
            "timeframe": "5m",
            "points": get_candles()[-20:]
        }
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Candle data unavailable"
        )


@app.get("/analyze")
def analyze():
    try:
        points = get_candles()
        closes = [float(point["c"]) for point in points]

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
        current_ema20 = ema20[-1]
        current_ema50 = ema50[-1]
        current_macd = macd_line[-1]
        current_signal = signal_line[-1]

        if (
            current_ema20 > current_ema50
            and current_macd > current_signal
            and 50 < current_rsi < 70
        ):
            signal = "BUY"
            reason = "EMA болон MACD өсөлтийг зааж байна."

        elif (
            current_ema20 < current_ema50
            and current_macd < current_signal
            and 30 < current_rsi < 50
        ):
            signal = "SELL"
            reason = "EMA болон MACD бууралтыг зааж байна."

        else:
            signal = "WAIT"
            reason = "Одоогоор тодорхой арилжааны нөхцөл алга."

        return {
            "symbol": "XAUUSD",
            "timeframe": "5m",
            "price": round(closes[-1], 2),
            "signal": signal,
            "confidence": None,
            "indicators": {
                "RSI": round(current_rsi, 2),
                "EMA20": round(current_ema20, 2),
                "EMA50": round(current_ema50, 2),
                "MACD": round(current_macd, 4),
                "MACD_signal": round(current_signal, 4)
            },
            "reason": reason,
            "data_source": "XAUS"
        }

    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Analysis unavailable"
        )
    
