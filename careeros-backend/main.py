from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.db import close_all_connections
from app.routes import dashboard, gmail, jobs, overlay, profile, settings as settings_routes
from app.routes.gmail import start_periodic_scan, stop_periodic_scan
from app.ws.autoapply_handler import router as autoapply_ws_router
from app.ws.dashboard_handler import router as dashboard_ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_periodic_scan()
    yield
    stop_periodic_scan()
    # close shared + all cached per-user "own mongo" clients on shutdown
    await close_all_connections()


app = FastAPI(title="CareerOS API", lifespan=lifespan)

# CORS -- only the website origin is allowed to call this API from a browser.
# The Electron bot and OAuth redirects don't go through browser CORS at all,
# so this only needs to cover the website's frontend origin(s).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.WEBSITE_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- REST routers ----
app.include_router(overlay.router)
app.include_router(jobs.router)
app.include_router(profile.router)
app.include_router(settings_routes.router)
app.include_router(dashboard.router)
app.include_router(gmail.router)

# ---- WebSocket routers ----
app.include_router(autoapply_ws_router)
app.include_router(dashboard_ws_router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}
