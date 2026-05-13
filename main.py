from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os

app = FastAPI()

@app.get("/")
def root():
    port = os.environ.get("PORT", "NAO_DEFINIDO")
    return HTMLResponse(f"<h1>Faiston Ops - Online!</h1><p>PORT={port}</p>")

@app.get("/api/health")
def health():
    return {"status": "ok", "port": os.environ.get("PORT")}