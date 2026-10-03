"""PostgreSQL storage service - the only storage backend.

Handles the 8 canonical invoice fields, with these business rules:
  - Money (Decimal) -> NUMERIC(18,2), never FLOAT/REAL.
  - Invoice date -> DATE.
  - A missing value -> a real SQL NULL (see to_display_value() below for
    converting back to the "Null" business convention used elsewhere,
    e.g. when displaying a record).
"""

from typing import Any, Dict, Optional
from decimal import Decimal, InvalidOperation
import psycopg
from psycopg_pool import ConnectionPool
from app.config.settings import settings
from app.utils.logging_config import logger

# The same 8 canonical fields as InvoiceRecord.REQUIRED_COLUMNS.
# Order matters here - it drives both the CREATE TABLE and the INSERT statement.
INVOICE_COLUMNS = [
    "utr_no",         # TEXT
    "invoice_number",  # TEXT
    "invoice_date",    # DATE
    "invoice_amount",  # NUMERIC(18,2)
    "total_payment",   # NUMERIC(18,2)
    "total_net_payment",  # NUMERIC(18,2)
    "payment_done",    # NUMERIC(18,2)
    "vendor_name",     # TEXT
    "source",          # TEXT
]

CREATE_INVOICES_TABLE = """
CREATE TABLE IF NOT EXISTS invoices (
    id BIGSERIAL PRIMARY KEY,
    utr_no TEXT,
    invoice_number TEXT,
    invoice_date DATE,
    invoice_amount NUMERIC(18,2),
    total_payment NUMERIC(18,2),
    total_net_payment NUMERIC(18,2),
    payment_done NUMERIC(18,2),
    vendor_name TEXT,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Indexes to make duplicate lookups fast - NOT hard uniqueness constraints.

# Migration for databases created before total_net_payment existed.
# CREATE TABLE IF NOT EXISTS does not touch an existing table, so the
# column has to be added explicitly. Safe to run on every start.
ADD_TOTAL_NET_PAYMENT_COLUMN = """
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS total_net_payment NUMERIC(18,2);
"""

CREATE_INVOICE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_invoices_invoice_number ON invoices (invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_name_lower ON invoices (lower(vendor_name));
"""

# Document-level duplicate detection (has this exact email body / attachment
# content already been processed?) 
CREATE_PROCESSED_DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS processed_documents (
    content_hash TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresStorageService:
    """PostgreSQL storage for the canonical invoice output (Phase 2).

    Connects using a small connection pool (not one connection per call,
    not a large always-on pool either - just enough to be safe under
    FastAPI's concurrent requests without over-engineering it for this
    project's scale).
    """

    def __init__(self):
        if not settings.postgres_db or not settings.postgres_user:
            raise ValueError(
                "PostgreSQL is not configured - set POSTGRES_DB and "
                "POSTGRES_USER (and POSTGRES_PASSWORD, POSTGRES_HOST, "
                "POSTGRES_PORT as needed) in .env"
            )

        conninfo = (
            f"host={settings.postgres_host} port={settings.postgres_port} "
            f"dbname={settings.postgres_db} user={settings.postgres_user}"
        )
        if settings.postgres_sslmode:
            # Only added when set (e.g. POSTGRES_SSLMODE=require for a
            # managed/cloud Postgres like Neon) - a local/native PostgreSQL
            # with nothing set here behaves exactly as before.
            conninfo += f" sslmode={settings.postgres_sslmode}"
        # Password is passed via kwargs, never interpolated into the
        # conninfo string that might end up in a log/traceback somewhere.
        logger.info(
            f"Connecting to PostgreSQL: host={settings.postgres_host} "
            f"port={settings.postgres_port} db={settings.postgres_db} "
            f"user={settings.postgres_user}"
        )

        try:
            self.pool = ConnectionPool(
                conninfo=conninfo,
                kwargs={"password": settings.postgres_password},
                min_size=1,
                max_size=5,
                open=True,
            )
            self._initialize_schema()
            logger.info("PostgreSQL storage service initialized")
        except psycopg.OperationalError as e:
            logger.error(f"Could not connect to PostgreSQL: {str(e)}")
            raise

    def _initialize_schema(self):
        """Create the tables/indexes if they don't already exist. Safe to
        call every time the app starts - CREATE TABLE IF NOT EXISTS and
        CREATE INDEX IF NOT EXISTS are no-ops once the schema exists."""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(CREATE_INVOICES_TABLE)
                    cur.execute(ADD_TOTAL_NET_PAYMENT_COLUMN)
                    cur.execute(CREATE_INVOICE_INDEXES)
                    cur.execute(CREATE_PROCESSED_DOCUMENTS_TABLE)
                conn.commit()
        except psycopg.Error as e:
            logger.error(f"Failed to initialize PostgreSQL schema: {str(e)}")
            raise

# Invoice storage

    def add_invoice(self, invoice_data: Dict[str, Any]) -> bool:
        """Insert one invoice row. Money fields must already be Decimal (or
        None) - never float, matching the Phase 1 business rule; psycopg
        sends Decimal to NUMERIC natively, no conversion needed. A missing
        value is Python None, which becomes a real SQL NULL - never the
        text "Null" (that conversion only happens on the way back out, see
        to_display_row()).
        """
        values = [invoice_data.get(col) for col in INVOICE_COLUMNS]

        insert_sql = f"""
            INSERT INTO invoices ({', '.join(INVOICE_COLUMNS)})
            VALUES ({', '.join(['%s'] * len(INVOICE_COLUMNS))})
        """

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(insert_sql, values)
                conn.commit()
            logger.info(
                f"Stored invoice in PostgreSQL: {invoice_data.get('invoice_number')}"
            )
            return True
        except psycopg.Error as e:

            logger.error(f"Failed to store invoice in PostgreSQL: {str(e)}")
            return False

    def check_duplicate(self, invoice_number: Optional[str], vendor_name: Optional[str] = None) -> bool:
        """Duplicate-detection business rule: invoice_number is the
        identity; vendor_name is an
        extra check only when known on BOTH the new record and an existing
        row with the same invoice_number.
        """
        if not invoice_number:
            return False

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT vendor_name FROM invoices WHERE invoice_number = %s",
                        (invoice_number,),
                    )
                    rows = cur.fetchall()
        except psycopg.Error as e:
            logger.error(f"Failed to check duplicate in PostgreSQL: {str(e)}")
            return False

        if not rows:
            return False

        normalized_vendor = vendor_name.strip().lower() if vendor_name else None

        for (existing_vendor,) in rows:
            existing_vendor_normalized = (
                existing_vendor.strip().lower() if existing_vendor else None
            )
            if normalized_vendor and existing_vendor_normalized:
                if normalized_vendor != existing_vendor_normalized:
                    continue  # same invoice_number, different KNOWN vendors - not a duplicate
            logger.info(
                f"Duplicate found (PostgreSQL): invoice_number={invoice_number}, vendor_name={vendor_name}"
            )
            return True

        return False

    # Document-level duplicate detection (a real DB constraint, unlike the

    def is_document_processed(self, content_hash: str) -> bool:
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM processed_documents WHERE content_hash = %s",
                        (content_hash,),
                    )
                    return cur.fetchone() is not None
        except psycopg.Error as e:
            logger.error(f"Failed to check document dedup in PostgreSQL: {str(e)}")
            return False

    def mark_document_processed(self, content_hash: str) -> bool:
        """Atomic, race-safe (unlike the JSON-file tracker used in Excel-only
        mode) thanks to the PRIMARY KEY constraint on content_hash - two
        concurrent requests processing the same document can both attempt
        this and only one will "win", with no risk of a corrupted file."""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO processed_documents (content_hash) VALUES (%s) "
                        "ON CONFLICT (content_hash) DO NOTHING",
                        (content_hash,),
                    )
                conn.commit()
            return True
        except psycopg.Error as e:
            logger.error(f"Failed to mark document processed in PostgreSQL: {str(e)}")
            return False

    # Read-back helpers

    def get_statistics(self) -> Dict[str, Any]:
        """Summary stats for the stored invoices."""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*), COALESCE(SUM(invoice_amount), 0) FROM invoices")
                    count, total = cur.fetchone()
            return {
                "total_invoices": count,
                "total_invoice_amount": float(total),
                "backend": "postgresql",
                "database": settings.postgres_db,
            }
        except psycopg.Error as e:
            logger.error(f"Failed to get PostgreSQL statistics: {str(e)}")
            return {"total_invoices": 0, "total_invoice_amount": 0.0, "error": str(e)}

    def fetch_all_for_comparison(self):
        """All stored rows, oldest first - used only by the Excel-vs-
        PostgreSQL comparison step during migration testing, not by the
        normal application flow."""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {', '.join(INVOICE_COLUMNS)} FROM invoices ORDER BY id"
                    )
                    cols = INVOICE_COLUMNS
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
        except psycopg.Error as e:
            logger.error(f"Failed to fetch PostgreSQL rows: {str(e)}")
            return []

    def list_invoices(self, limit: int = 25, offset: int = 0, search: Optional[str] = None) -> Dict[str, Any]:
        """Paginated list for the UI, newest first. `search` (optional)
        matches invoice_number or vendor_name, case-insensitive, partial."""
        where_sql = ""
        params: list = []
        if search:
            where_sql = "WHERE invoice_number ILIKE %s OR vendor_name ILIKE %s"
            like = f"%{search}%"
            params = [like, like]

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM invoices {where_sql}", params)
                    total = cur.fetchone()[0]

                    cur.execute(
                        f"""SELECT id, {', '.join(INVOICE_COLUMNS)}, created_at
                            FROM invoices {where_sql}
                            ORDER BY id DESC
                            LIMIT %s OFFSET %s""",
                        params + [limit, offset],
                    )
                    cols = ["id"] + INVOICE_COLUMNS + ["created_at"]
                    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {"total": total, "rows": rows}
        except psycopg.Error as e:
            logger.error(f"Failed to list invoices from PostgreSQL: {str(e)}")
            return {"total": 0, "rows": []}

    def list_incomplete_invoices(self, limit: int = 25, offset: int = 0) -> Dict[str, Any]:
        """Invoices missing one or more key fields - the same rule used in
        the manual pgAdmin verification checklist, now exposed as an API
        for the Validation page."""
        where_sql = (
            "WHERE invoice_number IS NULL OR invoice_amount IS NULL "
            "OR vendor_name IS NULL OR invoice_date IS NULL"
        )
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM invoices {where_sql}")
                    total = cur.fetchone()[0]

                    cur.execute(
                        f"""SELECT id, {', '.join(INVOICE_COLUMNS)}, created_at
                            FROM invoices {where_sql}
                            ORDER BY id DESC
                            LIMIT %s OFFSET %s""",
                        [limit, offset],
                    )
                    cols = ["id"] + INVOICE_COLUMNS + ["created_at"]
                    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {"total": total, "rows": rows}
        except psycopg.Error as e:
            logger.error(f"Failed to list incomplete invoices from PostgreSQL: {str(e)}")
            return {"total": 0, "rows": []}

    @staticmethod
    def to_display_value(value):
        """Convert a value read back from PostgreSQL into the project's
        display convention: SQL NULL -> the text "Null". A real Decimal 0
        stays 0 - it is never conflated with a missing value."""
        return "Null" if value is None else value

    def close(self):
        """Close the connection pool (call on app shutdown)."""
        try:
            self.pool.close()
        except Exception as e:
            logger.warning(f"Error closing PostgreSQL pool: {str(e)}")