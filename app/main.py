import asyncio
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes import router, processor
from app.utils.logging_config import logger
from app.config.settings import settings


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI Invoice Automation System - Phase 1"
)

# Allows a separately-running frontend (e.g. React's dev server on a
# different port) to call this API. Wide open for local development -
# restrict allow_origins to the actual frontend's URL before this is
# reachable by anyone other than you.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

_background_task = None


async def _background_processing_loop():
    """Poll the inbox on a fixed interval so new invoices are picked up
    automatically without anyone having to run process_emails.py by hand."""
    interval_seconds = max(settings.background_processing_interval_minutes, 1) * 60
    logger.info(
        f"Background processing started - checking for new invoices every "
        f"{settings.background_processing_interval_minutes} minute(s)"
    )

    loop = asyncio.get_event_loop()

    while True:
        if processor.sync_paused:
            # Paused via POST /api/sync/stop - skip this cycle entirely
            # rather than calling process_inbox(). Checked fresh every
            # cycle, so POST /api/sync/resume takes effect on the next one.
            logger.debug("Background sync is paused - skipping this cycle")
        else:
            try:
                # process_inbox() is synchronous (network + file I/O), so run it in
                # a worker thread rather than blocking the whole event loop.
                results = await loop.run_in_executor(None, processor.process_inbox)
                logger.info(f"Background processing run complete: {results}")
            except Exception as e:
                logger.error(f"Background processing run failed: {str(e)}")
                # Don't let one bad run kill the loop - just wait and try again.

        await asyncio.sleep(interval_seconds)


@app.on_event("startup")
async def startup_event():
    global _background_task
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

    if settings.enable_background_processing:
        _background_task = asyncio.create_task(_background_processing_loop())
    else:
        logger.info(
            "Background processing is disabled - set "
            "ENABLE_BACKGROUND_PROCESSING=true in .env to poll automatically, "
            "or use POST /api/process to trigger a run manually."
        )


@app.on_event("shutdown")
async def shutdown_event():
    logger.info(f"Shutting down {settings.app_name}")
    if _background_task:
        _background_task.cancel()


@app.get("/")
async def root():
    return {
        "application": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "background_processing": settings.enable_background_processing,
        "background_processing_interval_minutes": settings.background_processing_interval_minutes,
        "description": "AI Invoice Automation System - Phase 1",
        "note": (
            "Background processing is running automatically."
            if settings.enable_background_processing
            else "Background processing is disabled. Use POST /api/process to "
                 "manually trigger email processing, or set "
                 "ENABLE_BACKGROUND_PROCESSING=true in .env to automate it."
        )
    }


# Optional: serve the built React UI from this same process, so running
# without Docker still gives you one URL for everything, with no separate
# web server (nginx/IIS) to install. This is inert and changes nothing
# above if invoice-ui/dist doesn't exist (e.g. you're only using the API,
# or running the UI separately with `npm run dev` as before) - it is only
# ever added when the folder is actually there.
#
# Registered LAST and deliberately does NOT touch the "/" route above -
# GET / still always returns the JSON status block, exactly as before, so
# the stability monitor's health check (which expects that JSON) is
# unaffected either way.
_ui_dist = Path(__file__).resolve().parent.parent / "invoice-ui" / "dist"
if _ui_dist.is_dir():
    logger.info(f"Serving built UI from {_ui_dist}")
    app.mount("/assets", StaticFiles(directory=_ui_dist / "assets"), name="ui-assets")

    @app.get("/{full_path:path}")
    async def _serve_ui(full_path: str):
        # Any path not already matched above (an /api/... route, "/" itself,
        # or a real file under /assets) is a client-side route the React
        # Router handles in the browser (e.g. /invoices, /dashboard) - hand
        # it the same index.html and let it take over, exactly like a
        # single-page-app web server's "SPA fallback" would.
        return FileResponse(_ui_dist / "index.html")
else:
    logger.info(
        f"No built UI found at {_ui_dist} - only the API is served. Run "
        "`npm run build` in invoice-ui/ to also serve the UI from here."
    )