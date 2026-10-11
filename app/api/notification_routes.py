"""Dashboard notification API (GET/POST /api/notifications/...).

Every endpoint requires the same admin login as the rest of the API.

Also holds two small async helpers - record_run_notifications() and
record_run_failure_notification() - that main.py / routes.py call after an
invoice run. They NEVER raise, so a notification problem can never turn
into a failed invoice run.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services.auth_service import require_auth
from app.services.notification_service import (
    NOTIFICATION_TYPES,
    SEVERITIES,
    NotificationService,
)
from app.utils.logging_config import logger

router = APIRouter(prefix="/notifications", tags=["notifications"])

_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Created lazily on first use, re-using the invoice pipeline's existing
    PostgreSQL connection pool (no new connection settings)."""
    global _service
    if _service is None:
        # Imported here (not at the top) to avoid a circular import:
        # routes.py is where the shared `processor` lives.
        from app.api.routes import processor
        _service = NotificationService(processor.postgres_service.pool)
    return _service


async def _run(func, *args):
    """Run a blocking DB call in a worker thread, like the existing routes."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


def _unavailable(action: str, error: Exception) -> HTTPException:
    # Logged at WARNING (not ERROR): the stability monitor alerts on ERROR
    # lines, and a dashboard hiccup should not page anyone.
    logger.warning(f"Notifications: could not {action}: {error}")
    return HTTPException(status_code=503, detail="Notifications are temporarily unavailable")


# ---------- Hooks used after an invoice run (never raise) ----------

async def record_run_notifications(results) -> None:
    try:
        await _run(get_notification_service().record_run, results)
    except Exception as e:
        logger.warning(f"Notifications: could not record run: {e}")


async def record_run_failure_notification(error_text: str) -> None:
    try:
        await _run(get_notification_service().record_run_failure, error_text)
    except Exception as e:
        logger.warning(f"Notifications: could not record run failure: {e}")


# ---------- API ----------

@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    severity: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    unread_only: bool = Query(False),
    user: str = Depends(require_auth),
):
    if severity and severity not in SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity must be one of {', '.join(SEVERITIES)}")
    if type and type not in NOTIFICATION_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {', '.join(NOTIFICATION_TYPES)}")
    try:
        return await _run(
            get_notification_service().list_notifications,
            limit, offset, severity, type, unread_only,
        )
    except Exception as e:
        raise _unavailable("list notifications", e)


@router.get("/unread-count")
async def unread_count(user: str = Depends(require_auth)):
    try:
        return {"unread": await _run(get_notification_service().get_unread_count)}
    except Exception as e:
        raise _unavailable("count unread notifications", e)


@router.post("/read-all")
async def mark_all_read(user: str = Depends(require_auth)):
    try:
        changed = await _run(get_notification_service().mark_all_read)
        return {"marked": changed, "unread": 0}
    except Exception as e:
        raise _unavailable("mark all notifications as read", e)


@router.post("/{notification_id}/read")
async def mark_read(notification_id: int, user: str = Depends(require_auth)):
    try:
        svc = get_notification_service()
        found = await _run(svc.mark_read, notification_id)
        if not found:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"id": notification_id, "is_read": True, "unread": await _run(svc.get_unread_count)}
    except HTTPException:
        raise
    except Exception as e:
        raise _unavailable("mark notification as read", e)
