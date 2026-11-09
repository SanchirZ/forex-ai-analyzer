from fastapi import FastAPI, HTTPException
import requests

app = FastAPI(title="Forex AI Analyzer")

@app.get("/")
def home():
    return {"app": "Forex AI Analyzer", "status": "online"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/price")
def gold_price():
    try:
        response = requests.get(
            "https://xaus.com/api/v1/spot?compact=1",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        return {
            "symbol": "XAUUSD",
            "price": data.get("spot_usd_oz"),
            "currency": "USD",
            "source": "XAUS",
            "data_state": data.get("data_state"),
            "price_as_of": data.get("price_as_of")
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Gold price provider unavailable"
        )

@app.get("/analyze")
def analyze():
    return {
        "symbol": "XAUUSD",
        "signal": "WAIT",
        "confidence": None,
        "reason": "AI analysis is not connected yet"
    }
