"""The backend agent's service."""

from fastapi import FastAPI

from backend.routers import orders

app = FastAPI()
app.include_router(orders.router, prefix="/api")


@app.get("/health")
def health():
    return {"ok": True}
