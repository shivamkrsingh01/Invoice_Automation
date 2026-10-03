import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Response, Depends
from pydantic import BaseModel
from app.utils.logging_config import logger
from app.services.invoice_processor import InvoiceProcessor
from app.services.auth_service import (
    authenticate,
    create_session_token,
    require_auth,
    COOKIE_NAME,
    TOKEN_TTL_HOURS,
)

router = APIRouter()

# Initialize processor
processor = InvoiceProcessor()

# Shared pause flag for the background sync loop (see app/main.py's
# _background_processing_loop, which checks this each cycle). Lives on the
# processor instance since routes.py and main.py both already import it -
# simplest way to share this one bit of state without a new module.
processor.sync_paused = False

# QAService is created lazily (only on the first /api/qa call, not at
# import time) so the app still starts fine even if GEMINI_API_KEY isn't
# set - you just can't use the Q&A page until it is.
_qa_service = None


def _get_qa_service():
    global _qa_service
    if _qa_service is None:
        from app.services.qa_service import QAService
        _qa_service = QAService()
    return _qa_service


class QARequest(BaseModel):
    question: str


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- Unauthenticated ----------
# Only /health and /auth/login work without being logged in. Every other
# endpoint below requires `user: str = Depends(require_auth)`.

@router.get("/health")
async def health_check():
    """Health check endpoint."""
    logger.info("Health check requested")
    return {
        "status": "healthy",
        "service": "AI Invoice Automation"
    }


@router.post("/auth/login")
async def login(body: LoginRequest, response: Response):
    """Check username/password against .env and, if correct, set a signed
    session cookie. The cookie is httpOnly - JavaScript can never read it,
    which is what actually protects it from being stolen via XSS."""
    try:
        ok = authenticate(body.username, body.password)
    except ValueError as e:
        # ADMIN_USERNAME/ADMIN_PASSWORD not set in .env - a config
        # problem, not a wrong-password problem, so 503 not 401.
        raise HTTPException(status_code=503, detail=str(e))

    if not ok:
        logger.warning(f"Failed login attempt for username: {body.username}")
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    try:
        token = create_session_token(body.username)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=TOKEN_TTL_HOURS * 60 * 60,
        # secure=True should be added once this is served over HTTPS -
        # left off for now since local development is plain HTTP.
    )
    logger.info(f"Admin login: {body.username}")
    return {"username": body.username}


@router.post("/auth/logout")
async def logout(response: Response):
    """Clears the session cookie. Works even if already logged out."""
    response.delete_cookie(COOKIE_NAME)
    return {"status": "logged out"}


@router.get("/auth/me")
async def get_me(user: str = Depends(require_auth)):
    """The frontend calls this on load to check whether an existing
    session is still valid, and to know who's logged in."""
    return {"username": user}


# ---------- Authenticated ----------

@router.post("/process")
async def process_invoices(limit: int = 10, user: str = Depends(require_auth)):
    """Trigger invoice processing pipeline."""
    logger.info(f"Invoice processing requested with limit: {limit} (by {user})")

    try:
        # process_inbox() is synchronous (network + file I/O + OCR), so run
        # it in a worker thread rather than blocking the whole event loop -
        # same reasoning as the background polling loop in app/main.py,
        # which shares this same `processor` instance. Calling it directly
        # here would freeze every other request (including /health) for the
        # full duration of a manual run.
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, processor.process_inbox, limit)
        return {
            "status": "completed",
            "results": results
        }
    except Exception as e:
        logger.error(f"Processing failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/invoices")
async def get_invoices(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, description="Matches invoice number or vendor name"),
    user: str = Depends(require_auth),
):
    """Paginated invoice listing for the Invoices/Dashboard pages."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, processor.postgres_service.list_invoices, limit, offset, search
        )
        return {
            "total": result["total"],
            "limit": limit,
            "offset": offset,
            "invoices": result["rows"],
        }
    except Exception as e:
        logger.error(f"Failed to list invoices: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/invoices/validation")
async def get_invoices_needing_validation(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: str = Depends(require_auth),
):
    """Invoices missing a key field (invoice_number, invoice_amount,
    vendor_name, or invoice_date) - for the Validation page."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, processor.postgres_service.list_incomplete_invoices, limit, offset
        )
        return {
            "total": result["total"],
            "limit": limit,
            "offset": offset,
            "invoices": result["rows"],
        }
    except Exception as e:
        logger.error(f"Failed to list invoices needing validation: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sync/status")
async def get_sync_status(user: str = Depends(require_auth)):
    """Status for the Dashboard's sync banner and the sidebar toggle."""
    from app.config.settings import settings

    last_sync = None
    try:
        last_sync = processor.timestamp_tracker.get_last_processed_time()
        if last_sync is not None:
            last_sync = last_sync.isoformat()
    except Exception as e:
        logger.warning(f"Could not read last sync timestamp: {str(e)}")

    return {
        "background_processing_enabled": settings.enable_background_processing,
        "paused": processor.sync_paused,
        "interval_minutes": settings.background_processing_interval_minutes,
        "last_sync": last_sync,
    }


@router.post("/sync/stop")
async def stop_sync(user: str = Depends(require_auth)):
    """Pause the background polling loop. Does not cancel a run already
    in progress - it just skips the next cycle onward until resumed."""
    processor.sync_paused = True
    logger.info(f"Background sync paused via API (by {user})")
    return {"paused": True}


@router.post("/sync/resume")
async def resume_sync(user: str = Depends(require_auth)):
    """Resume the background polling loop."""
    processor.sync_paused = False
    logger.info(f"Background sync resumed via API (by {user})")
    return {"paused": False}


@router.post("/qa")
async def ask_question(body: QARequest, user: str = Depends(require_auth)):
    """Answer a natural-language question about the stored invoice data
    (Part 4). Fetches the invoice rows and hands them to Gemini as plain
    text - never lets the model generate or run SQL directly."""
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    try:
        qa_service = _get_qa_service()
    except ValueError as e:
        # Most likely GEMINI_API_KEY isn't set - a config problem, not a
        # server error, so 400 rather than 500.
        raise HTTPException(status_code=400, detail=str(e))

    try:
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(
            None, qa_service.answer_question, body.question, processor.postgres_service
        )
        return {"question": body.question, "answer": answer}
    except Exception as e:
        logger.error(f"Q&A request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))