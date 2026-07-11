from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.db import ensure_indexes
from app.routers import auth, chat, dashboard, internal, jobs, payments, profile, settings as settings_router, ws

app = FastAPI(title="CareerOS API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serves uploaded resumes/documents back at the URLs returned by /profile/documents
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(profile.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(dashboard.router, prefix=API_PREFIX)
app.include_router(settings_router.router, prefix=API_PREFIX)
app.include_router(payments.router, prefix=API_PREFIX)
app.include_router(internal.router, prefix=API_PREFIX)
# WebSocket lives at /ws/dashboard (no /api/v1 prefix) — matches dashboardSocket.js's deriveWsBase()
app.include_router(ws.router)


@app.on_event("startup")
async def on_startup():
    await ensure_indexes()


@app.get("/health")
async def health():
    return {"status": "ok"}
