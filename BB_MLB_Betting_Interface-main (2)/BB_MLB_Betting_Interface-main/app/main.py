"""
BarBoards backend — FastAPI application.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.bdl_client import bdl_client
from app.config import settings
from app.routes import router as api_router
from app.webhooks import router as webhooks_router
from app.ws import router as ws_router
from app import win_expectancy
from app.win_expectancy import load_table, _we_table

# ── Logging ─────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("barboards")


# ── Lifecycle ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting BarBoards backend...")
    await bdl_client.start()
    logger.info("All systems online.")
    yield
    # Shutdown
    logger.info("Shutting down...")
    await bdl_client.close()
    logger.info("Goodbye.")


# ── App ─────────────────────────────────────────

app = FastAPI(
    title="BarBoards Live Odds",
    description="Real-time betting odds and social engagement for sports bars",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow the React frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(api_router)
app.include_router(webhooks_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "bdl_headroom": bdl_client.headroom,
    }
