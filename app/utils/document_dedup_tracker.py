import hashlib
import json
import os
from typing import List
from app.utils.logging_config import logger


class DocumentDedupTracker:
    """Tracks which source DOCUMENTS (email bodies / attachments) have
    already been processed, keyed by a content hash - so the exact same
    email body or the exact same attachment content is never processed
    twice, even if it's seen again (e.g. a manual re-run of /api/process,
    or a message that stays unread because marking it as read failed).

    This is deliberately separate from INVOICE-level duplicate detection
    (see PostgresStorageService.check_duplicate, keyed by invoice number
    and vendor name):
    - Document-level: "have I already extracted THIS exact file/body?"
    - Invoice-level:  "have I already stored THIS business invoice,
      regardless of which document it came from?"

    Both are needed - the same document must never be re-extracted, and the
    same business invoice must never be stored twice even if it legitimately
    arrives through two different documents (its PDF attachment and a copy
    pasted into the email body, for example).

    Persisted as a flat JSON list of content hashes (not a database - Phase
    1 is Excel-only), capped at MAX_ENTRIES to avoid unbounded growth over a
    long-running deployment; oldest entries are evicted first.
    """

    MAX_ENTRIES = 20000

    def __init__(self, store_file: str = "processed_documents.json"):
        self.store_file = store_file
        self._hashes: List[str] = []
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.store_file):
                with open(self.store_file, "r") as f:
                    data = json.load(f)
                    self._hashes = list(data.get("processed_hashes", []))
        except Exception as e:
            logger.warning(f"Could not load document dedup store: {str(e)}")
            self._hashes = []

    def _save(self):
        try:
            with open(self.store_file, "w") as f:
                json.dump({"processed_hashes": self._hashes}, f)
        except Exception as e:
            logger.error(f"Could not save document dedup store: {str(e)}")

    @staticmethod
    def compute_hash(content: bytes) -> str:
        """Content hash used as the document's identity - stable regardless
        of which email/attachment metadata (message ID, attachment ID,
        filename) it happens to arrive under this time."""
        return hashlib.sha256(content).hexdigest()

    def is_processed(self, content_hash: str) -> bool:
        """Has a document with this exact content already been handled?"""
        return content_hash in self._hashes

    def mark_processed(self, content_hash: str):
        """Record that a document with this exact content has now been
        handled, so a future exact repeat is skipped."""
        if content_hash in self._hashes:
            return
        self._hashes.append(content_hash)
        if len(self._hashes) > self.MAX_ENTRIES:
            self._hashes = self._hashes[-self.MAX_ENTRIES:]
        self._save()
