from fastapi import FastAPI

app = FastAPI(title="Forex AI Analyzer")

@app.get("/")
def home():
    return {
        "app": "Forex AI Analyzer",
        "status": "online"
    }

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/analyze")
def analyze():
    return {
        "symbol": "XAUUSD",
        "signal": "WAIT",
        "confidence": None,
        "reason": "Waiting for live XM market data"
    }
