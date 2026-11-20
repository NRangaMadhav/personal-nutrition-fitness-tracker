import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .api import router as tracking_router
from .ai.routes import router as ai_router
from .database import engine
from .storage import router as storage_router


app = FastAPI(title="Personal Nutrition & Fitness Tracker", version="1.0.0")
app.include_router(tracking_router)
app.include_router(storage_router)
app.include_router(ai_router)
logger = logging.getLogger(__name__)


@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "unavailable"})
    return {"status": "ok", "database": "connected"}


@app.exception_handler(OperationalError)
async def database_unavailable(request, exc):
    logger.error("Database operation failed; check the local PostgreSQL service.")
    return JSONResponse(status_code=503, content={"detail": "Database unavailable. Please try again shortly."})
