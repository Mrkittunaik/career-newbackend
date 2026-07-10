import asyncio
import logging

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.services.gmail_service import scan_all_connected_users, scan_inbox_for_replies

router = APIRouter(prefix="/gmail", tags=["gmail"])

logger = logging.getLogger(__name__)

# How often the periodic scan sweeps every connected user's inbox.
_PERIODIC_SCAN_INTERVAL_MINUTES = 15

_periodic_scan_task: asyncio.Task | None = None


@router.post("/scan")
async def trigger_scan(current_user: dict = Depends(get_current_user)):
    """Manual 'check now' trigger for the dashboard button. Runs synchronously
    (single user, so it's fast) and returns a summary."""
    user_id = str(current_user["_id"])
    result = await scan_inbox_for_replies(user_id)
    return {"success": True, **result}


async def _periodic_scan_loop() -> None:
    """Background loop: sweeps scan_inbox_for_replies across every connected
    user every _PERIODIC_SCAN_INTERVAL_MINUTES.

    NOTE: this is a simple in-process asyncio loop, good enough while the
    number of connected Gmail users is small. If usage grows, replace this
    with a proper scheduled task queue (Celery beat + workers, or RQ +
    rq-scheduler) so scans run in a separate worker process, can be retried
    individually per user, and don't compete with request-handling for this
    process's event loop.
    """
    while True:
        try:
            summary = await scan_all_connected_users()
            logger.info("gmail periodic scan complete: %s", summary)
        except Exception:
            logger.exception("gmail periodic scan failed")

        await asyncio.sleep(_PERIODIC_SCAN_INTERVAL_MINUTES * 60)


def start_periodic_scan() -> None:
    """Call once from main.py's lifespan startup to kick off the background
    loop. Idempotent -- calling twice won't spawn a second loop."""
    global _periodic_scan_task
    if _periodic_scan_task is None or _periodic_scan_task.done():
        _periodic_scan_task = asyncio.create_task(_periodic_scan_loop())


def stop_periodic_scan() -> None:
    """Call from main.py's lifespan shutdown to cancel the loop cleanly."""
    global _periodic_scan_task
    if _periodic_scan_task is not None:
        _periodic_scan_task.cancel()
        _periodic_scan_task = None
