"""
Foundry Systems — Client Acquisition Engine (FS-CAE)
Operator Control Tower Web Interface
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401 - Register all models with Base.metadata
from app.config import get_settings
from app.database import Base, build_engine
from app.routers import (
    dashboard,
    inbox,
    invoices,
    projects,
    queue,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all tables exist on startup
    settings = get_settings()
    engine = build_engine(settings.DATABASE_URL)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="Foundry Systems — Operator Control Tower",
    description="Commercial Acquisition, Outreach Dispatch, and Financial Reconciliation Cockpit",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static directory
static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include Routers
app.include_router(dashboard.router)
app.include_router(queue.router)
app.include_router(inbox.router)
app.include_router(invoices.router)
app.include_router(projects.router)


@app.get("/health")
def health_check():
    """Service health ping."""
    return {"status": "HEALTHY", "service": "FS-CAE Control Tower", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
