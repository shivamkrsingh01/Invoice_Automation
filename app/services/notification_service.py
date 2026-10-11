"""Dashboard notification service.

Stores notifications in their own `notifications` table (the `invoices`
table and every other existing table are never touched) and turns the
result of an invoice-processing run into notifications.

Design rules (see the notes in each method):
  * It REUSES the existing PostgreSQL connection pool - no new connection
    string, no new credentials, no new database library.
  * Creating a notification NEVER raises. A dashboard problem must not be
    able to interrupt invoice processing. Failures are logged at WARNING
    level (not ERROR), because the Telegram stability monitor treats ERROR
    lines in logs/app.log as alerts.
  * It never calls Telegram. Telegram alerts come from the separate
    stability_monitor process and are left completely alone, so the two
    systems cannot affect each other.
  * Only events that can be read directly from the results dict returned
    by InvoiceProcessor.process_inbox() are turned into notifications.
"""

import hashlib
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# Standard logging (not app.utils.logging_config) so this module has no
# import-time dependency on the app settings. Records still reach
# logs/app.log because the root logger is configured by the app.
logger = logging.getLogger("app.notifications")

SEVERITIES = ("success", "warning", "error", "system")

NOTIFICATION_TYPES = (
    "invoice_processed",   # invoices stored successfully
    "invoice_not_stored",  # invoices found but none stored
    "duplicate_document",  # exact email/attachment already processed
    "processing_error",    # one email/attachment failed
    "run_failed",          # the whole inbox run failed
    "run_recovered",       # first clean run after a failed one
)

# Months are measured in this time zone (matches the IST display the app
# already uses). Postgres does the month maths, so 28/29/30/31-day months
# need no special handling.
MONTH_TIMEZONE = "Asia/Kolkata"

# How many error notifications one run may create (the rest are summarised).
MAX_ERROR_NOTIFICATIONS_PER_RUN = 5
MAX_TEXT_LENGTH = 300
# A DB call from here waits at most this long for a pool connection, so a
# busy/slow database can never hold up the invoice pipeline for long.
POOL_TIMEOUT_SECONDS = 5
# Monthly clean-up is cheap but only needs to run now and then.
CLEANUP_MIN_INTERVAL_SECONDS = 6 * 60 * 60

FATAL_PREFIX = "Fatal error during inbox processing"

CREATE_NOTIFICATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    notification_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('success', 'warning', 'error', 'system')),
    title TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    invoice_id BIGINT,
    event_key TEXT,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_NOTIFICATION_INDEXES = [
    # Newest-first listing.
    "CREATE INDEX IF NOT EXISTS idx_notifications_created_at "
    "ON notifications (created_at DESC, id DESC)",
    # Unread count / unread-only listing.
    "CREATE INDEX IF NOT EXISTS idx_notifications_unread "
    "ON notifications (id) WHERE is_read = FALSE",
    # Same event can never be stored twice (retry protection).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_event_key "
    "ON notifications (event_key) WHERE event_key IS NOT NULL",
]


def _short(text: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_key(error: Dict[str, Any]) -> str:
    """Stable identity for one failure, so the same failing attachment that
    is retried every cycle produces ONE notification, not one per cycle."""
    raw = "|".join(
        str(error.get(k, "")) for k in ("email_id", "attachment_id", "filename", "error")
    )
    return "err:" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:24]


def build_run_notifications(results: Dict[str, Any], run_id: str) -> List[Dict[str, Any]]:
    """Pure function: process_inbox() results -> list of notification dicts.

    Mirrors how the stability monitor describes the same run for Telegram
    (same counters, same categories). Empty runs and runs with no invoice
    data and no errors produce nothing.
    """
    out: List[Dict[str, Any]] = []
    emails = _int(results.get("emails_processed"))
    atts = _int(results.get("attachments_processed"))
    found = _int(results.get("invoices_extracted"))
    stored = _int(results.get("invoices_stored"))
    dup_docs = _int(results.get("documents_skipped_duplicate"))
    errors = results.get("errors") or []

    # --- per-failure notifications (deduplicated across retries) -------
    shown = 0
    for err in errors:
        if not isinstance(err, dict):
            err = {"error": str(err)}
        if shown >= MAX_ERROR_NOTIFICATIONS_PER_RUN:
            break
        where = err.get("filename") or ""
        text = _short(err.get("error", "Unknown error"))
        out.append({
            "notification_type": "processing_error",
            "severity": "error",
            "title": "Invoice processing error",
            "message": (f"{where}: {text}" if where else text),
            "event_key": _error_key(err),
        })
        shown += 1
    if len(errors) > shown:
        out.append({
            "notification_type": "processing_error",
            "severity": "error",
            "title": "More processing errors",
            "message": f"{len(errors) - shown} more error(s) in this run - see logs/app.log",
            "event_key": f"errmore:{run_id}",
        })

    # --- one summary notification per run ------------------------------
    not_stored = max(found - stored, 0)
    counts = f"Emails: {emails}, attachments: {atts}, invoices found: {found}, stored: {stored}."
    if dup_docs:
        counts += f" Duplicate documents skipped: {dup_docs}."

    if found and stored:
        msg = counts
        if not_stored:
            msg += (f" {not_stored} not stored (already in the database "
                    f"or missing key fields).")
        out.append({
            "notification_type": "invoice_processed",
            "severity": "success",
            "title": "Invoice processed" if stored == 1 else "Invoices processed",
            "message": msg,
            "event_key": f"run:{run_id}",
        })
    elif found and not stored:
        out.append({
            "notification_type": "invoice_not_stored",
            "severity": "warning",
            "title": "Invoices not stored",
            "message": counts + " Nothing new was stored (already in the database "
                                "or missing key fields).",
            "event_key": f"run:{run_id}",
        })
    elif dup_docs:
        out.append({
            "notification_type": "duplicate_document",
            "severity": "system",
            "title": "Duplicate document skipped",
            "message": counts + " This exact email/attachment was already processed.",
            "event_key": f"run:{run_id}",
        })
    return out


class NotificationService:
    def __init__(self, pool):
        self.pool = pool
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._last_cleanup = 0.0
        # In-memory only: lets us send ONE "failed" and ONE "recovered"
        # notification per outage instead of one per cycle. Resets on restart.
        self._last_run_failed = False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def ensure_schema(self):
        """Create the table/indexes if missing. Create-only and re-runnable:
        nothing is dropped, altered or truncated."""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
                with conn.cursor() as cur:
                    cur.execute(CREATE_NOTIFICATIONS_TABLE)
                    for stmt in CREATE_NOTIFICATION_INDEXES:
                        cur.execute(stmt)
                conn.commit()
            self._schema_ready = True
            logger.info("Notifications table ready")

    def cleanup_previous_months(self) -> int:
        """Delete notifications created before the 1st of the CURRENT month
        (in MONTH_TIMEZONE). Touches only the notifications table. Month
        lengths are handled by Postgres' date_trunc, not by us."""
        self.ensure_schema()
        with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM notifications WHERE created_at < "
                    "(date_trunc('month', now() AT TIME ZONE %s) AT TIME ZONE %s)",
                    (MONTH_TIMEZONE, MONTH_TIMEZONE),
                )
                deleted = cur.rowcount
            conn.commit()
        if deleted:
            logger.info(f"Removed {deleted} notification(s) from previous months")
        return deleted

    def _maybe_cleanup(self):
        """Throttled, never raises."""
        now = time.monotonic()
        if self._last_cleanup and now - self._last_cleanup < CLEANUP_MIN_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        try:
            self.cleanup_previous_months()
        except Exception as e:
            logger.warning(f"Notification clean-up skipped: {e}")

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def create(self, notification_type: str, severity: str, title: str,
               message: str = "", invoice_id: Optional[int] = None,
               event_key: Optional[str] = None) -> Optional[int]:
        """Store one notification. NEVER raises. Returns the new id, or None
        if it was not stored (duplicate event_key or a failure)."""
        try:
            if severity not in SEVERITIES:
                severity = "system"
            self.ensure_schema()
            with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO notifications "
                        "(notification_type, severity, title, message, invoice_id, event_key) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (event_key) WHERE event_key IS NOT NULL DO NOTHING "
                        "RETURNING id",
                        (notification_type, severity, _short(title, 200),
                         _short(message, 600), invoice_id, event_key),
                    )
                    row = cur.fetchone()
                conn.commit()
            return row[0] if row else None
        except Exception as e:
            logger.warning(f"Could not store dashboard notification: {e}")
            return None

    def record_run(self, results: Dict[str, Any]):
        """Create notifications for one finished process_inbox() run.
        NEVER raises."""
        try:
            run_id = uuid.uuid4().hex
            errors = results.get("errors") or []
            fatal = next(
                (e for e in errors
                 if isinstance(e, dict) and str(e.get("error", "")).startswith(FATAL_PREFIX)),
                None,
            )
            if fatal is not None:
                self._record_failure(fatal.get("error"), run_id)
                return
            self._record_recovery_if_needed(run_id)
            for item in build_run_notifications(results, run_id):
                self.create(**item)
            self._maybe_cleanup()
        except Exception as e:
            logger.warning(f"Could not record run notifications: {e}")

    def record_run_failure(self, error_text: str):
        """The whole background/manual run raised. NEVER raises."""
        try:
            self._record_failure(error_text, uuid.uuid4().hex)
        except Exception as e:
            logger.warning(f"Could not record run-failure notification: {e}")

    def _record_failure(self, error_text: Any, run_id: str):
        # Only the FIRST failure of an outage is announced.
        if self._last_run_failed:
            return
        self._last_run_failed = True
        self.create(
            "run_failed", "error", "Inbox processing failed",
            _short(error_text), event_key=f"fail:{run_id}",
        )

    def _record_recovery_if_needed(self, run_id: str):
        if not self._last_run_failed:
            return
        self._last_run_failed = False
        self.create(
            "run_recovered", "system", "Inbox processing recovered",
            "The latest run completed without a fatal error.",
            event_key=f"recover:{run_id}",
        )

    # ------------------------------------------------------------------
    # Reading / updating (these RAISE on DB errors so the API can answer
    # with a proper error instead of pretending there is no data)
    # ------------------------------------------------------------------
    def get_unread_count(self) -> int:
        self.ensure_schema()
        self._maybe_cleanup()
        with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM notifications WHERE is_read = FALSE")
                return cur.fetchone()[0]

    def list_notifications(self, limit: int = 20, offset: int = 0,
                           severity: Optional[str] = None,
                           notification_type: Optional[str] = None,
                           unread_only: bool = False) -> Dict[str, Any]:
        self.ensure_schema()
        where, params = [], []
        if severity:
            where.append("severity = %s")
            params.append(severity)
        if notification_type:
            where.append("notification_type = %s")
            params.append(notification_type)
        if unread_only:
            where.append("is_read = FALSE")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM notifications {where_sql}", params)
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT id, notification_type, severity, title, message, "
                    "invoice_id, is_read, read_at, created_at "
                    f"FROM notifications {where_sql} "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    params + [limit, offset],
                )
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM notifications WHERE is_read = FALSE")
                unread = cur.fetchone()[0]

        items = [
            {
                "id": r[0],
                "notification_type": r[1],
                "severity": r[2],
                "title": r[3],
                "message": r[4],
                "invoice_id": r[5],
                "is_read": r[6],
                "read_at": r[7].isoformat() if r[7] else None,
                "created_at": r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total, "unread": unread,
                "limit": limit, "offset": offset}

    def mark_read(self, notification_id: int) -> bool:
        """Idempotent. Returns False only if the id does not exist."""
        self.ensure_schema()
        with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notifications SET is_read = TRUE, "
                    "read_at = COALESCE(read_at, now()) WHERE id = %s RETURNING id",
                    (notification_id,),
                )
                found = cur.fetchone() is not None
            conn.commit()
        return found

    def mark_all_read(self) -> int:
        self.ensure_schema()
        with self.pool.connection(timeout=POOL_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notifications SET is_read = TRUE, read_at = now() "
                    "WHERE is_read = FALSE"
                )
                changed = cur.rowcount
            conn.commit()
        return changed
