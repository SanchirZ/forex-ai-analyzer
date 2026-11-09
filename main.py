
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import json
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


@app.get("/ai-analyze")
def ai_analyze():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI API түлхүүр тохируулаагүй байна."
        )

    try:
        market = {}

        for interval in ["1m", "5m", "15m"]:
            points = get_candles(interval)

            market[interval] = {
                "analysis": analyze_timeframe(interval),
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

        price_response = requests.get(PRICE_URL, timeout=10)
        price_response.raise_for_status()
        price_data = price_response.json()

        market_data = {
            "symbol": "XAUUSD",
            "price": price_data.get("spot_usd_oz"),
            "price_as_of": price_data.get("price_as_of"),
            "source": "XAUS",
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

        instructions = """
You are a careful XAUUSD scalping analysis assistant.
Analyze the supplied 1m, 5m and 15m candles and indicators.
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
The XAUS price is not the exact XM execution quote.
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
                        "name": "gold_scalping_analysis",
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
            "symbol": "XAUUSD",
            "data_source": "XAUS",
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
