# from typing import Dict, Any, List, Optional
# from datetime import datetime, timedelta, timezone
# import mimetypes
# from app.services.graph_service import GraphService
# from app.services.gemini_extraction_service import GeminiExtractionService
# from app.services.postgres_storage_service import PostgresStorageService

# # OLD OCR-BASED EXTRACTION
# # Kept as backup for now.
# # Gemini is currently used for invoice extraction.
# # from app.services.free_extraction_service import FreeExtractionService
# # from app.services.document_processor import DocumentProcessor

# from app.models.invoice import InvoiceRecord
# from app.utils.validation import InvoiceValidator
# from app.utils.file_utils import FileUtils
# from app.utils.timestamp_tracker import TimestampTracker
# from app.utils.document_dedup_tracker import DocumentDedupTracker
# from app.config.settings import settings
# from app.utils.logging_config import logger

# # Image extensions handled by the same Gemini multimodal path as PDFs, but
# # reported with a different `source` value.
# IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tiff', '.bmp')


# class InvoiceProcessor:
#     """Main invoice processing orchestrator."""

#     def __init__(self):
#         self.graph_service = GraphService()

#         # OLD OCR-BASED EXTRACTION
#         # Kept as backup for now.
#         # Gemini is currently used for invoice extraction.
#         # self.document_service = FreeExtractionService()
#         # self.document_processor = DocumentProcessor()

#         # Extraction/understanding is delegated entirely to Gemini - see
#         # GeminiExtractionService. No local OCR (Tesseract/pytesseract) is
#         # used anywhere in the ACTIVE pipeline anymore; Gemini's own
#         # multimodal understanding handles scanned/photographed content
#         # directly. The old OCR-based FreeExtractionService/DocumentProcessor
#         # classes above are commented out, not deleted - their files
#         # (app/services/free_extraction_service.py, app/utils/table_extraction.py,
#         # app/services/document_processor.py) are untouched and can be
#         # restored by uncommenting the two lines above and the __init__
#         # lines below, and switching the call sites in _process_attachment/
#         # _process_email_body back to the old method names.
#         self.document_service = GeminiExtractionService()
#         # PostgreSQL is the only storage backend. (This app previously
#         # supported an Excel output file and a "dual" write mode for
#         # verifying PostgreSQL against it during migration - both were
#         # removed once PostgreSQL was confirmed correct.)
#         self.postgres_service = PostgresStorageService()

#         # The storage service duplicate-checks and document-dedup decisions
#         # are based on.
#         self.primary_storage = self.postgres_service

#         self.timestamp_tracker = TimestampTracker()
#         self.document_dedup_tracker = DocumentDedupTracker()

#     def _is_document_processed(self, content_hash: str) -> bool:
#         """Document-level dedup check. Uses PostgreSQL's real unique
#         constraint when available (race-safe across concurrent requests),
#         otherwise falls back to the Phase 1 JSON-file tracker."""
#         if self.postgres_service:
#             return self.postgres_service.is_document_processed(content_hash)
#         return self.document_dedup_tracker.is_processed(content_hash)

#     def _mark_document_processed(self, content_hash: str):
#         if self.postgres_service:
#             self.postgres_service.mark_document_processed(content_hash)
#         else:
#             self.document_dedup_tracker.mark_processed(content_hash)

#     @staticmethod
#     def _gemini_invoices_to_raw(gemini_result: Dict[str, Any]) -> List[Dict[str, Any]]:
#         """Map GeminiExtractionService's output field names onto the exact
#         raw-dict shape InvoiceValidator.validate_invoice_data() expects -
#         the same shape the old extraction path produced, so everything
#         downstream of this point (validation, InvoiceRecord, duplicate
#         checking, PostgreSQL storage) needed NO changes at all:

#             Gemini field      ->  raw dict key expected downstream
#             ----------------      --------------------------------
#             utr_number        ->  utr_no
#             invoice_number     ->  invoice_number      (same name)
#             invoice_date       ->  invoice_date        (same name)
#             invoice_amount     ->  invoice_amount      (same name)
#             total_payment      ->  total_payment       (same name)
#             total_net_payment  ->  total_net_payment   (same name)
#             payment_amount     ->  payment_done_amount
#             vendor_name        ->  vendor_name         (same name)
#             invoice_source     ->  source

#         `email_timestamp` is intentionally NOT mapped here - there is no
#         column for it in the existing invoices table, and the task this
#         integration was built for explicitly said not to change the
#         PostgreSQL schema. GeminiExtractionService still returns it, in
#         case a future, separately-requested schema change wants it.
#         """
#         raw_list = []
#         for inv in gemini_result.get("invoices", []):
#             raw_list.append({
#                 "utr_no": inv.get("utr_number"),
#                 "invoice_number": inv.get("invoice_number"),
#                 "invoice_date": inv.get("invoice_date"),
#                 "invoice_amount": inv.get("invoice_amount"),
#                 "total_payment": inv.get("total_payment"),
#                 "total_net_payment": inv.get("total_net_payment"),
#                 "payment_done_amount": inv.get("payment_amount"),
#                 "vendor_name": inv.get("vendor_name"),
#                 "source": inv.get("invoice_source"),
#             })
#         return raw_list

#     def process_inbox(self, limit: int = 10) -> Dict[str, Any]:
#         """Process emails from the inbox and extract invoices."""
#         logger.info("Starting inbox processing")

#         results = {
#             "emails_processed": 0,
#             "attachments_processed": 0,
#             "invoices_extracted": 0,
#             "invoices_stored": 0,
#             "documents_skipped_duplicate": 0,
#             "errors": []
#         }

#         try:
#             logger.info("Storage backend: postgres")

#             # Filter for new emails only if enabled
#             since_filter = None
#             if settings.process_only_new_emails and settings.use_last_processed_timestamp:
#                 since_filter = self.timestamp_tracker.get_filter_string()
#                 if since_filter:
#                     logger.info(f"Processing only new emails since: {since_filter}")
#                 else:
#                     logger.info("No previous timestamp found - processing recent emails")
#                     one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
#                     since_filter = one_hour_ago.strftime('%Y-%m-%dT%H:%M:%SZ')
#                     logger.info(f"Processing emails from last 1 hour: {since_filter}")

#             messages = self.graph_service.get_messages(limit=limit, since=since_filter)
#             results["emails_processed"] = len(messages)

#             if len(messages) == 0:
#                 logger.info("No new emails to process")
#                 return results

#             # Watermark for the NEXT run's `since` filter. Tracked as the
#             # newest receivedDateTime actually fetched THIS run - not
#             # datetime.now() - so that when `messages` hits the `limit` cap
#             # (more matching emails exist than this run fetched), the
#             # watermark only advances past what was truly processed. Emails
#             # beyond the cap keep their receivedDateTime >= this watermark,
#             # so the next run's `since` filter will pick them up too,
#             # instead of "now" silently stranding them forever. Relies on
#             # GraphService.get_messages() ordering oldest-first, so a capped
#             # batch is the oldest unprocessed emails, not the newest.
#             latest_received_dt: Optional[datetime] = None

#             for message in messages:
#                 message_id = message.get("id")
#                 subject = message.get("subject", "No Subject")

#                 received_str = message.get("receivedDateTime")
#                 if received_str:
#                     try:
#                         received_dt = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
#                         if latest_received_dt is None or received_dt > latest_received_dt:
#                             latest_received_dt = received_dt
#                     except ValueError:
#                         logger.warning(
#                             f"Could not parse receivedDateTime '{received_str}' "
#                             f"for message {message_id} - watermark for this "
#                             f"message will fall back to run-completion time"
#                         )

#                 logger.info(f"Processing email: {subject} (ID: {message_id})")

#                 try:
#                     attachments = self.graph_service.get_attachments(message_id)

#                     # Process email body content first (may contain 0, 1, or many invoices)
#                     body_result = self._process_email_body(message_id, subject)
#                     if body_result["success"]:
#                         results["invoices_extracted"] += body_result["invoices_found"]
#                         results["invoices_stored"] += body_result["invoices_stored"]
#                     elif body_result.get("duplicate"):
#                         results["documents_skipped_duplicate"] += 1

#                     if attachments:
#                         logger.info(f"Found {len(attachments)} attachments in email: {subject}")

#                         for attachment in attachments:
#                             attachment_result = self._process_attachment(message_id, attachment)

#                             results["attachments_processed"] += 1

#                             if attachment_result["success"]:
#                                 results["invoices_extracted"] += attachment_result["invoices_found"]
#                                 results["invoices_stored"] += attachment_result["invoices_stored"]
#                             elif attachment_result.get("duplicate"):
#                                 results["documents_skipped_duplicate"] += 1
#                             else:
#                                 results["errors"].append({
#                                     "email_id": message_id,
#                                     "attachment_id": attachment.get("id"),
#                                     "filename": attachment.get("name"),
#                                     "error": attachment_result.get("error", "Unknown error")
#                                 })
#                     else:
#                         logger.info(f"No attachments found for email: {subject}")

#                     if settings.mark_emails_as_read:
#                         self.graph_service.mark_as_read(message_id)

#                 except Exception as e:
#                     error_msg = f"Failed to process email {message_id}: {str(e)}"
#                     logger.error(error_msg)
#                     results["errors"].append({
#                         "email_id": message_id,
#                         "error": error_msg
#                     })

#             logger.info(f"Processing complete. Results: {results}")

#             if results["emails_processed"] > 0:
#                 if len(messages) == limit:
#                     # Fetch cap was hit - there may be more matching emails
#                     # still waiting beyond this batch. Advance only to the
#                     # newest one actually fetched this run, not to "now",
#                     # so the next run's `since` filter resumes exactly
#                     # where this one left off instead of skipping anything.
#                     new_watermark = latest_received_dt or datetime.now(timezone.utc)
#                     logger.info(
#                         f"Fetch cap ({limit}) reached this run - advancing "
#                         f"watermark to the newest email fetched "
#                         f"({new_watermark.isoformat()}) rather than now, so "
#                         f"any remaining backlog is picked up next run"
#                     )
#                 else:
#                     # Fetched fewer than the cap - this run caught every
#                     # matching email, so it's safe to advance to now.
#                     new_watermark = datetime.now(timezone.utc)

#                 self.timestamp_tracker.update_timestamp(new_watermark)
#                 logger.info("Updated last processed timestamp (UTC)")

#             last_processed = self.timestamp_tracker.get_last_processed_time()
#             results["last_processed_utc"] = last_processed.isoformat() if last_processed else None
#             results["last_processed_ist"] = self.timestamp_tracker.get_last_processed_ist_display()

#             return results

#         except Exception as e:
#             error_msg = f"Fatal error during inbox processing: {str(e)}"
#             logger.error(error_msg)
#             results["errors"].append({"error": error_msg})
#             return results

#     def _process_email_body(self, message_id: str, subject: str) -> Dict[str, Any]:
#         """Process email body content for invoice information (may contain
#         zero, one, or many invoices, e.g. a table listing several)."""
#         result = {
#             "success": False,
#             "invoices_found": 0,
#             "invoices_stored": 0,
#             "duplicate": False,
#             "error": None
#         }

#         try:
#             # Get the RAW HTML body so any <table> structure survives - a table
#             # listing multiple invoices must not be flattened into one blob of text.
#             html_content = self.graph_service.get_message_html(message_id)

#             if not html_content or len(html_content.strip()) < 50:
#                 logger.info(f"Email body too short or empty: {subject}")
#                 result["error"] = "Email body too short"
#                 return result

#             # Document-level duplicate check: has this EXACT body content
#             # already been processed before (e.g. a manual re-run hitting
#             # the same message)? If so, skip re-extracting it entirely -
#             # separate from invoice-level duplicate detection, which
#             # instead asks whether this business invoice has been stored
#             # before, regardless of which document it came from.
#             body_hash = DocumentDedupTracker.compute_hash(html_content.encode("utf-8", errors="ignore"))
#             if self._is_document_processed(body_hash):
#                 logger.info(f"Email body already processed (duplicate document): {subject}")
#                 result["duplicate"] = True
#                 result["error"] = "Duplicate document (already processed)"
#                 return result

#             logger.info(f"Processing email body content (length: {len(html_content)} chars)")

#             # OLD OCR-BASED EXTRACTION
#             # Kept as backup for now.
#             # Gemini is currently used for invoice extraction.
#             # extracted_list = self.document_service.extract_invoices_from_html(
#             #     html_content, f"email_body_{message_id[:8]}", source="Email Body"
#             # )

#             # Gemini reads the HTML directly - including any <table> of
#             # multiple invoices - no local parsing needed here.
#             gemini_result = self.document_service.extract_from_text(
#                 html_content, source_label="Email Body"
#             )
#             extracted_list = self._gemini_invoices_to_raw(gemini_result)

#             if not extracted_list:
#                 result["error"] = "No data extracted from email body"
#                 return result

#             # Extraction genuinely succeeded (even if it turns out to
#             # contain zero storable invoices) - mark this exact content as
#             # handled so a future identical resend is skipped. A FAILED
#             # extraction above is deliberately NOT marked, so it remains
#             # eligible for retry (e.g. after a bug fix).
#             self._mark_document_processed(body_hash)

#             for extracted_data in extracted_list:
#                 stored = self._finalize_and_store_invoice(extracted_data)
#                 if stored is None:
#                     continue  # not likely an invoice, skip silently

#                 result["invoices_found"] += 1
#                 if stored:
#                     result["invoices_stored"] += 1

#             result["success"] = result["invoices_found"] > 0
#             if result["success"]:
#                 logger.info(
#                     f"Successfully processed email body: {subject} "
#                     f"({result['invoices_found']} invoice(s) found, {result['invoices_stored']} stored)"
#                 )
#             else:
#                 logger.info(f"Email body does not appear to contain invoice data: {subject}")

#             return result

#         except Exception as e:
#             error_msg = f"Failed to process email body {subject}: {str(e)}"
#             logger.error(error_msg)
#             result["error"] = error_msg
#             return result

#     def _finalize_and_store_invoice(self, extracted_data: Dict[str, Any]) -> Optional[bool]:
#         """Validate, normalize, and store a single extracted invoice dict.
#         Returns True if stored, False if it looked like an invoice but
#         wasn't stored (insufficient data or a duplicate), or None if it
#         doesn't look like an invoice at all (caller should not count it)."""
#         normalized_data = InvoiceValidator.validate_invoice_data(extracted_data)
#         invoice = InvoiceRecord(**normalized_data)

#         if not invoice.is_likely_invoice():
#             logger.info("Document does not appear to be an invoice")
#             return None

#         if not invoice.invoice_number and not invoice.invoice_amount:
#             logger.info("Document lacks key invoice fields (no invoice number or invoice amount)")
#             return False

#         # Invoice number (+ vendor name when known on both sides - see
#         # PostgresStorageService.check_duplicate) is the business identity.
#         # Source is deliberately NOT part of this check, so the same real
#         # invoice arriving as a PDF, a DOCX, an XLSX row, or in the email
#         # body is recognized as the same invoice rather than four separate
#         # ones. Records without an invoice number can't be deduplicated
#         # this way and are always stored.
#         if invoice.invoice_number and self.primary_storage.check_duplicate(invoice.invoice_number, invoice.vendor_name):
#             logger.info(f"Skipping duplicate invoice: {invoice.invoice_number}")
#             return False

#         return self.postgres_service.add_invoice(invoice.to_dict())

#     def _process_attachment(self, message_id: str, attachment: Dict[str, Any]) -> Dict[str, Any]:
#         """Process a single attachment (may yield zero, one, or many invoices,
#         e.g. a table of invoices inside one PDF/Excel/Word file)."""
#         attachment_id = attachment.get("id")
#         filename = attachment.get("name", "unknown")
#         content_type = attachment.get("contentType")

#         result = {
#             "success": False,
#             "invoices_found": 0,
#             "invoices_stored": 0,
#             "duplicate": False,
#             "error": None
#         }

#         try:
#             if not InvoiceValidator.is_supported_file_type(filename, content_type):
#                 logger.info(f"Skipping unsupported file type: {filename}")
#                 result["error"] = "Unsupported file type"
#                 return result

#             logger.info(f"Downloading attachment: {filename}")
#             attachment_content = self.graph_service.download_attachment(message_id, attachment_id)

#             # Document-level duplicate check: has this EXACT file content
#             # already been processed before (e.g. the same file forwarded
#             # in a second email, or a manual re-run)? Hashing the raw bytes
#             # catches this regardless of filename or which message/
#             # attachment ID it arrives under this time - separate from
#             # invoice-level duplicate detection (see
#             # PostgresStorageService.check_duplicate), which asks whether
#             # this business invoice has been stored before, from ANY document.
#             attachment_hash = DocumentDedupTracker.compute_hash(attachment_content)
#             if self._is_document_processed(attachment_hash):
#                 logger.info(f"Attachment already processed (duplicate document): {filename}")
#                 result["duplicate"] = True
#                 result["error"] = "Duplicate document (already processed)"
#                 return result

#             logger.info(f"Extracting invoice(s) from: {filename}")

#             filename_lower = filename.lower()

#             if filename_lower.endswith(('.docx', '.doc')):
#                 # GeminiExtractionService converts DOCX -> text internally
#                 # (python-docx, not OCR) before sending it to Gemini - see
#                 # that module's docstring for why this conversion step is
#                 # needed at all.
#                 #
#                 # OLD OCR-BASED EXTRACTION
#                 # Kept as backup for now.
#                 # Gemini is currently used for invoice extraction.
#                 # word_data = self.document_processor.process_word_document(attachment_content, filename)
#                 # if word_data.get("processing_status") == "FAILED":
#                 #     result["error"] = word_data.get("error", "Word document processing failed")
#                 #     return result
#                 # extracted_list = self.document_service.extract_invoices_from_tables(
#                 #     word_data.get("tables", []), filename, source="DOCX",
#                 #     surrounding_text=word_data.get("text_content", "")
#                 # )
#                 # if not extracted_list:
#                 #     combined_text = word_data.get("text_content", "")
#                 #     for table in word_data.get("tables", []):
#                 #         for row in table:
#                 #             combined_text += "\n" + " ".join(str(c) for c in row if c)
#                 #     extracted_list = self.document_service.extract_invoices_from_text(
#                 #         combined_text, filename, source="DOCX"
#                 #     )
#                 gemini_result = self.document_service.extract_from_docx_bytes(
#                     attachment_content, source_label="DOCX"
#                 )

#             elif filename_lower.endswith(('.xlsx', '.xls')):
#                 # Same idea for XLSX (openpyxl, not OCR).
#                 #
#                 # OLD OCR-BASED EXTRACTION
#                 # Kept as backup for now.
#                 # Gemini is currently used for invoice extraction.
#                 # excel_data = self.document_processor.process_excel_document(attachment_content, filename)
#                 # if excel_data.get("processing_status") == "FAILED":
#                 #     result["error"] = excel_data.get("error", "Excel document processing failed")
#                 #     return result
#                 # sheet_tables = list(excel_data.get("sheets", {}).values())
#                 # all_cells_text = "\n".join(
#                 #     str(cell) for sheet_data in sheet_tables for row in sheet_data for cell in row
#                 # )
#                 # extracted_list = self.document_service.extract_invoices_from_tables(
#                 #     sheet_tables, filename, source="XLSX", surrounding_text=all_cells_text
#                 # )
#                 # if not extracted_list:
#                 #     combined_text = ""
#                 #     for sheet_name, sheet_data in excel_data.get("sheets", {}).items():
#                 #         combined_text += f"\nSheet: {sheet_name}\n"
#                 #         for row in sheet_data:
#                 #             combined_text += " ".join(str(c) for c in row if c) + "\n"
#                 #     extracted_list = self.document_service.extract_invoices_from_text(
#                 #         combined_text, filename, source="XLSX"
#                 #     )
#                 gemini_result = self.document_service.extract_from_xlsx_bytes(
#                     attachment_content, source_label="XLSX"
#                 )

#             else:
#                 # PDF and images - sent directly to Gemini as bytes. Gemini's
#                 # own multimodal understanding handles both text-based PDFs
#                 # and scanned/photographed content (no local OCR step), and
#                 # a single PDF containing multiple invoices naturally
#                 # produces multiple entries in the returned list.
#                 #
#                 # OLD OCR-BASED EXTRACTION
#                 # Kept as backup for now.
#                 # Gemini is currently used for invoice extraction.
#                 # source = "Image" if filename_lower.endswith(IMAGE_EXTENSIONS) else "PDF"
#                 # extracted_list = self.document_service.extract_invoices(
#                 #     attachment_content, filename, source=source
#                 # )
#                 gemini_input_content = attachment_content
#                 if filename_lower.endswith(IMAGE_EXTENSIONS):
#                     source_label = "Image"
#                     if filename_lower.endswith(('.tiff', '.bmp')):
#                         # Gemini only accepts image/png, image/jpeg,
#                         # image/webp, image/heic, image/heif - TIFF and BMP
#                         # are rejected outright. Convert to PNG bytes with
#                         # Pillow (already a project dependency) rather than
#                         # let these files fail at the Gemini call.
#                         from PIL import Image
#                         import io as _io
#                         img = Image.open(_io.BytesIO(attachment_content))
#                         buf = _io.BytesIO()
#                         img.convert("RGB").save(buf, format="PNG")
#                         gemini_input_content = buf.getvalue()
#                         mime_type = "image/png"
#                     else:
#                         mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
#                 else:
#                     source_label = "PDF"
#                     mime_type = "application/pdf"

#                 gemini_result = self.document_service.extract_from_bytes(
#                     gemini_input_content, mime_type=mime_type, source_label=source_label
#                 )

#             extracted_list = self._gemini_invoices_to_raw(gemini_result)

#             if not extracted_list:
#                 result["error"] = "No data extracted"
#                 return result

#             # Extraction genuinely succeeded (even if it turns out to
#             # contain zero storable invoices) - mark this exact file content
#             # as handled so a future identical resend is skipped. A FAILED
#             # extraction above is deliberately NOT marked, so it remains
#             # eligible for retry (e.g. after a bug fix).
#             self._mark_document_processed(attachment_hash)

#             for extracted_data in extracted_list:
#                 stored = self._finalize_and_store_invoice(extracted_data)
#                 if stored is None:
#                     continue  # not likely an invoice, skip silently

#                 result["invoices_found"] += 1
#                 if stored:
#                     result["invoices_stored"] += 1

#             result["success"] = result["invoices_found"] > 0
#             if result["success"]:
#                 logger.info(
#                     f"Successfully processed attachment: {filename} "
#                     f"({result['invoices_found']} invoice(s) found, {result['invoices_stored']} stored)"
#                 )
#             else:
#                 result["error"] = "No invoice-like data found in attachment"

#             FileUtils.cleanup_temp_directory()

#             return result

#         except Exception as e:
#             error_msg = f"Failed to process attachment {filename}: {str(e)}"
#             logger.error(error_msg)
#             result["error"] = error_msg
#             return result

#     def process_single_attachment(self, message_id: str, attachment_id: str, filename: str) -> Dict[str, Any]:
#         """Process a single attachment by ID (for testing)."""
#         logger.info(f"Processing single attachment: {filename}")

#         try:
#             attachments = self.graph_service.get_attachments(message_id)
#             attachment = next((a for a in attachments if a.get("id") == attachment_id), None)

#             if not attachment:
#                 return {"success": False, "error": "Attachment not found"}

#             return self._process_attachment(message_id, attachment)

#         except Exception as e:
#             error_msg = f"Failed to process single attachment: {str(e)}"
#             logger.error(error_msg)
#             return {"success": False, "error": error_msg}




from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
import mimetypes
from app.services.graph_service import GraphService
from app.services.gemini_extraction_service import GeminiExtractionService
from app.services.postgres_storage_service import PostgresStorageService

# OLD OCR-BASED EXTRACTION
# Kept as backup for now.
# Gemini is currently used for invoice extraction.
# from app.services.free_extraction_service import FreeExtractionService
# from app.services.document_processor import DocumentProcessor

from app.models.invoice import InvoiceRecord
from app.utils.validation import InvoiceValidator
from app.utils.file_utils import FileUtils
from app.utils.timestamp_tracker import TimestampTracker
from app.utils.document_dedup_tracker import DocumentDedupTracker
from app.config.settings import settings
from app.utils.logging_config import logger

# Image extensions handled by the same Gemini multimodal path as PDFs, but
# reported with a different `source` value.
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tiff', '.bmp')


class InvoiceProcessor:
    """Main invoice processing orchestrator."""

    def __init__(self):
        self.graph_service = GraphService()

        # OLD OCR-BASED EXTRACTION
        # Kept as backup for now.
        # Gemini is currently used for invoice extraction.
        # self.document_service = FreeExtractionService()
        # self.document_processor = DocumentProcessor()

        # Extraction/understanding is delegated entirely to Gemini - see
        # GeminiExtractionService. No local OCR (Tesseract/pytesseract) is
        # used anywhere in the ACTIVE pipeline anymore; Gemini's own
        # multimodal understanding handles scanned/photographed content
        # directly. The old OCR-based FreeExtractionService/DocumentProcessor
        # classes above are commented out, not deleted - their files
        # (app/services/free_extraction_service.py, app/utils/table_extraction.py,
        # app/services/document_processor.py) are untouched and can be
        # restored by uncommenting the two lines above and the __init__
        # lines below, and switching the call sites in _process_attachment/
        # _process_email_body back to the old method names.
        self.document_service = GeminiExtractionService()
        # PostgreSQL is the only storage backend. (This app previously
        # supported an Excel output file and a "dual" write mode for
        # verifying PostgreSQL against it during migration - both were
        # removed once PostgreSQL was confirmed correct.)
        self.postgres_service = PostgresStorageService()

        # The storage service duplicate-checks and document-dedup decisions
        # are based on.
        self.primary_storage = self.postgres_service

        self.timestamp_tracker = TimestampTracker()
        self.document_dedup_tracker = DocumentDedupTracker()

    def _is_document_processed(self, content_hash: str) -> bool:
        """Document-level dedup check. Uses PostgreSQL's real unique
        constraint when available (race-safe across concurrent requests),
        otherwise falls back to the Phase 1 JSON-file tracker."""
        if self.postgres_service:
            return self.postgres_service.is_document_processed(content_hash)
        return self.document_dedup_tracker.is_processed(content_hash)

    def _mark_document_processed(self, content_hash: str):
        if self.postgres_service:
            self.postgres_service.mark_document_processed(content_hash)
        else:
            self.document_dedup_tracker.mark_processed(content_hash)

    @staticmethod
    def _gemini_invoices_to_raw(gemini_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Map GeminiExtractionService's output field names onto the exact
        raw-dict shape InvoiceValidator.validate_invoice_data() expects -
        the same shape the old extraction path produced, so everything
        downstream of this point (validation, InvoiceRecord, duplicate
        checking, PostgreSQL storage) needed NO changes at all:

            Gemini field      ->  raw dict key expected downstream
            ----------------      --------------------------------
            utr_number        ->  utr_no
            invoice_number     ->  invoice_number      (same name)
            invoice_date       ->  invoice_date        (same name)
            invoice_amount     ->  invoice_amount      (same name)
            total_payment      ->  total_payment       (same name)
            total_net_payment  ->  total_net_payment   (same name)
            payment_amount     ->  payment_done_amount
            vendor_name        ->  vendor_name         (same name)
            invoice_source     ->  source

        `email_timestamp` is intentionally NOT mapped here - there is no
        column for it in the existing invoices table, and the task this
        integration was built for explicitly said not to change the
        PostgreSQL schema. GeminiExtractionService still returns it, in
        case a future, separately-requested schema change wants it.
        """
        raw_list = []
        for inv in gemini_result.get("invoices", []):
            raw_list.append({
                "utr_no": inv.get("utr_number"),
                "invoice_number": inv.get("invoice_number"),
                "invoice_date": inv.get("invoice_date"),
                "invoice_amount": inv.get("invoice_amount"),
                "total_payment": inv.get("total_payment"),
                "total_net_payment": inv.get("total_net_payment"),
                "payment_done_amount": inv.get("payment_amount"),
                "vendor_name": inv.get("vendor_name"),
                "source": inv.get("invoice_source"),
            })
        return raw_list

    def process_inbox(self, limit: int = 10) -> Dict[str, Any]:
        """Process emails from the inbox and extract invoices."""
        logger.info("Starting inbox processing")

        results = {
            "emails_processed": 0,
            "attachments_processed": 0,
            "invoices_extracted": 0,
            "invoices_stored": 0,
            "documents_skipped_duplicate": 0,
            "errors": []
        }

        try:
            logger.info("Storage backend: postgres")

            # Filter for new emails only if enabled
            since_filter = None
            if settings.process_only_new_emails and settings.use_last_processed_timestamp:
                since_filter = self.timestamp_tracker.get_filter_string()
                if since_filter:
                    logger.info(f"Processing only new emails since: {since_filter}")
                else:
                    logger.info("No previous timestamp found - processing recent emails")
                    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
                    since_filter = one_hour_ago.strftime('%Y-%m-%dT%H:%M:%SZ')
                    logger.info(f"Processing emails from last 1 hour: {since_filter}")

            messages = self.graph_service.get_messages(limit=limit, since=since_filter)
            results["emails_processed"] = len(messages)

            if len(messages) == 0:
                logger.info("No new emails to process")
                return results

            # Watermark for the NEXT run's `since` filter. Tracked as the
            # newest receivedDateTime actually fetched THIS run - not
            # datetime.now() - so that when `messages` hits the `limit` cap
            # (more matching emails exist than this run fetched), the
            # watermark only advances past what was truly processed. Emails
            # beyond the cap keep their receivedDateTime >= this watermark,
            # so the next run's `since` filter will pick them up too,
            # instead of "now" silently stranding them forever. Relies on
            # GraphService.get_messages() ordering oldest-first, so a capped
            # batch is the oldest unprocessed emails, not the newest.
            # latest_received_dt: Optional[datetime] = None

            # for message in messages:
            #     message_id = message.get("id")
            #     subject = message.get("subject", "No Subject")

            #     received_str = message.get("receivedDateTime")
            #     if received_str:
            #         try:
            #             received_dt = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
            #             if latest_received_dt is None or received_dt > latest_received_dt:
            #                 latest_received_dt = received_dt
            #         except ValueError:
            #             logger.warning(
            #                 f"Could not parse receivedDateTime '{received_str}' "
            #                 f"for message {message_id} - watermark for this "
            #                 f"message will fall back to run-completion time"
            #             )

            #     logger.info(f"Processing email: {subject} (ID: {message_id})")

            #     try:

            latest_received_dt: Optional[datetime] = None
            ids_at_latest: List[str] = []

            for message in messages:
                message_id = message.get("id")
                subject = message.get("subject", "No Subject")

                received_dt: Optional[datetime] = None
                received_str = message.get("receivedDateTime")
                if received_str:
                    try:
                        received_dt = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
                        if latest_received_dt is None or received_dt > latest_received_dt:
                            latest_received_dt = received_dt
                            ids_at_latest = [message_id]
                        elif received_dt == latest_received_dt:
                            ids_at_latest.append(message_id)
                    except ValueError:
                        logger.warning(
                            f"Could not parse receivedDateTime '{received_str}' "
                            f"for message {message_id} - watermark for this "
                            f"message will fall back to run-completion time"
                        )

                # Authoritative local check, independent of whatever Graph's
                # own `since` filter did or didn't exclude - see
                # TimestampTracker.is_already_seen for why this exists.
                if received_dt is not None and self.timestamp_tracker.is_already_seen(message_id, received_dt):
                    logger.info(
                        f"Skipping message already handled at this exact timestamp "
                        f"(ID: {message_id}): {subject}"
                    )
                    continue

                logger.info(f"Processing email: {subject} (ID: {message_id})")

                try:            
                    attachments = self.graph_service.get_attachments(message_id)

                    # Process email body content first (may contain 0, 1, or many invoices)
                    body_result = self._process_email_body(message_id, subject)
                    if body_result["success"]:
                        results["invoices_extracted"] += body_result["invoices_found"]
                        results["invoices_stored"] += body_result["invoices_stored"]
                    elif body_result.get("duplicate"):
                        results["documents_skipped_duplicate"] += 1

                    if attachments:
                        logger.info(f"Found {len(attachments)} attachments in email: {subject}")

                        for attachment in attachments:
                            attachment_result = self._process_attachment(message_id, attachment)

                            results["attachments_processed"] += 1

                            if attachment_result["success"]:
                                results["invoices_extracted"] += attachment_result["invoices_found"]
                                results["invoices_stored"] += attachment_result["invoices_stored"]
                            elif attachment_result.get("duplicate"):
                                results["documents_skipped_duplicate"] += 1
                            else:
                                results["errors"].append({
                                    "email_id": message_id,
                                    "attachment_id": attachment.get("id"),
                                    "filename": attachment.get("name"),
                                    "error": attachment_result.get("error", "Unknown error")
                                })
                    else:
                        logger.info(f"No attachments found for email: {subject}")

                    if settings.mark_emails_as_read:
                        self.graph_service.mark_as_read(message_id)

                except Exception as e:
                    error_msg = f"Failed to process email {message_id}: {str(e)}"
                    logger.error(error_msg)
                    results["errors"].append({
                        "email_id": message_id,
                        "error": error_msg
                    })

            logger.info(f"Processing complete. Results: {results}")

            if results["emails_processed"] > 0:
                if len(messages) == limit:
                    # Fetch cap was hit - there may be more matching emails
                    # still waiting beyond this batch. Advance only to the
                    # newest one actually fetched this run, not to "now",
                    # so the next run's `since` filter resumes exactly
                    # where this one left off instead of skipping anything.
                    new_watermark = latest_received_dt or datetime.now(timezone.utc)
                    logger.info(
                        f"Fetch cap ({limit}) reached this run - advancing "
                        f"watermark to the newest email fetched "
                        f"({new_watermark.isoformat()}) rather than now, so "
                        f"any remaining backlog is picked up next run"
                    )
                else:
                    # Fetched fewer than the cap - but NOT necessarily
                    # "every matching email that exists right now": Graph
                    # can have a brief indexing lag where a message's
                    # receivedDateTime is already in the past, yet that
                    # message doesn't show up in a `receivedDateTime ge X`
                    # query for a few moments after it actually arrives. If
                    # this run's query missed such a message and the
                    # watermark advanced to wall-clock now(), that message's
                    # own (earlier) timestamp would fall permanently before
                    # the next run's filter - silently stranding it forever,
                    # even though it was never processed. So advance only to
                    # the newest message actually fetched this run, exactly
                    # like the capped branch above, never to now(). The
                    # latest message may then be matched again by the next
                    # poll's `ge` filter - harmless, since the document-hash
                    # duplicate check (cheap, before any Gemini call) skips
                    # it without reprocessing.
                    new_watermark = latest_received_dt or datetime.now(timezone.utc)

                # self.timestamp_tracker.update_timestamp(new_watermark)
                self.timestamp_tracker.update_timestamp(new_watermark, message_ids=ids_at_latest)
                logger.info("Updated last processed timestamp (UTC)")

            last_processed = self.timestamp_tracker.get_last_processed_time()
            results["last_processed_utc"] = last_processed.isoformat() if last_processed else None
            results["last_processed_ist"] = self.timestamp_tracker.get_last_processed_ist_display()

            return results

        except Exception as e:
            error_msg = f"Fatal error during inbox processing: {str(e)}"
            logger.error(error_msg)
            results["errors"].append({"error": error_msg})
            return results

    def _process_email_body(self, message_id: str, subject: str) -> Dict[str, Any]:
        """Process email body content for invoice information (may contain
        zero, one, or many invoices, e.g. a table listing several)."""
        result = {
            "success": False,
            "invoices_found": 0,
            "invoices_stored": 0,
            "duplicate": False,
            "error": None
        }

        try:
            # Get the RAW HTML body so any <table> structure survives - a table
            # listing multiple invoices must not be flattened into one blob of text.
            html_content = self.graph_service.get_message_html(message_id)

            if not html_content or len(html_content.strip()) < 50:
                logger.info(f"Email body too short or empty: {subject}")
                result["error"] = "Email body too short"
                return result

            # Document-level duplicate check: has this EXACT body content
            # already been processed before (e.g. a manual re-run hitting
            # the same message)? If so, skip re-extracting it entirely -
            # separate from invoice-level duplicate detection, which
            # instead asks whether this business invoice has been stored
            # before, regardless of which document it came from.
            body_hash = DocumentDedupTracker.compute_hash(html_content.encode("utf-8", errors="ignore"))
            if self._is_document_processed(body_hash):
                logger.info(f"Email body already processed (duplicate document): {subject}")
                result["duplicate"] = True
                result["error"] = "Duplicate document (already processed)"
                return result

            logger.info(f"Processing email body content (length: {len(html_content)} chars)")

            # OLD OCR-BASED EXTRACTION
            # Kept as backup for now.
            # Gemini is currently used for invoice extraction.
            # extracted_list = self.document_service.extract_invoices_from_html(
            #     html_content, f"email_body_{message_id[:8]}", source="Email Body"
            # )

            # Gemini reads the HTML directly - including any <table> of
            # multiple invoices - no local parsing needed here.
            gemini_result = self.document_service.extract_from_text(
                html_content, source_label="Email Body"
            )
            extracted_list = self._gemini_invoices_to_raw(gemini_result)

            if not extracted_list:
                result["error"] = "No data extracted from email body"
                return result

            # Extraction genuinely succeeded (even if it turns out to
            # contain zero storable invoices) - mark this exact content as
            # handled so a future identical resend is skipped. A FAILED
            # extraction above is deliberately NOT marked, so it remains
            # eligible for retry (e.g. after a bug fix).
            self._mark_document_processed(body_hash)

            for extracted_data in extracted_list:
                stored = self._finalize_and_store_invoice(extracted_data)
                if stored is None:
                    continue  # not likely an invoice, skip silently

                result["invoices_found"] += 1
                if stored:
                    result["invoices_stored"] += 1

            result["success"] = result["invoices_found"] > 0
            if result["success"]:
                logger.info(
                    f"Successfully processed email body: {subject} "
                    f"({result['invoices_found']} invoice(s) found, {result['invoices_stored']} stored)"
                )
            else:
                logger.info(f"Email body does not appear to contain invoice data: {subject}")

            return result

        except Exception as e:
            error_msg = f"Failed to process email body {subject}: {str(e)}"
            logger.error(error_msg)
            result["error"] = error_msg
            return result

    def _finalize_and_store_invoice(self, extracted_data: Dict[str, Any]) -> Optional[bool]:
        """Validate, normalize, and store a single extracted invoice dict.
        Returns True if stored, False if it looked like an invoice but
        wasn't stored (insufficient data or a duplicate), or None if it
        doesn't look like an invoice at all (caller should not count it)."""
        normalized_data = InvoiceValidator.validate_invoice_data(extracted_data)
        invoice = InvoiceRecord(**normalized_data)

        if not invoice.is_likely_invoice():
            logger.info("Document does not appear to be an invoice")
            return None

        if not invoice.invoice_number and not invoice.invoice_amount:
            logger.info("Document lacks key invoice fields (no invoice number or invoice amount)")
            return False

        # Invoice number (+ vendor name when known on both sides - see
        # PostgresStorageService.check_duplicate) is the business identity.
        # Source is deliberately NOT part of this check, so the same real
        # invoice arriving as a PDF, a DOCX, an XLSX row, or in the email
        # body is recognized as the same invoice rather than four separate
        # ones. Records without an invoice number can't be deduplicated
        # this way and are always stored.
        if invoice.invoice_number and self.primary_storage.check_duplicate(invoice.invoice_number, invoice.vendor_name):
            logger.info(f"Skipping duplicate invoice: {invoice.invoice_number}")
            return False

        return self.postgres_service.add_invoice(invoice.to_dict())

    def _process_attachment(self, message_id: str, attachment: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single attachment (may yield zero, one, or many invoices,
        e.g. a table of invoices inside one PDF/Excel/Word file)."""
        attachment_id = attachment.get("id")
        filename = attachment.get("name", "unknown")
        content_type = attachment.get("contentType")

        result = {
            "success": False,
            "invoices_found": 0,
            "invoices_stored": 0,
            "duplicate": False,
            "error": None
        }

        try:
            if not InvoiceValidator.is_supported_file_type(filename, content_type):
                logger.info(f"Skipping unsupported file type: {filename}")
                result["error"] = "Unsupported file type"
                return result

            logger.info(f"Downloading attachment: {filename}")
            attachment_content = self.graph_service.download_attachment(message_id, attachment_id)

            # Document-level duplicate check: has this EXACT file content
            # already been processed before (e.g. the same file forwarded
            # in a second email, or a manual re-run)? Hashing the raw bytes
            # catches this regardless of filename or which message/
            # attachment ID it arrives under this time - separate from
            # invoice-level duplicate detection (see
            # PostgresStorageService.check_duplicate), which asks whether
            # this business invoice has been stored before, from ANY document.
            attachment_hash = DocumentDedupTracker.compute_hash(attachment_content)
            if self._is_document_processed(attachment_hash):
                logger.info(f"Attachment already processed (duplicate document): {filename}")
                result["duplicate"] = True
                result["error"] = "Duplicate document (already processed)"
                return result

            logger.info(f"Extracting invoice(s) from: {filename}")

            filename_lower = filename.lower()

            if filename_lower.endswith(('.docx', '.doc')):
                # GeminiExtractionService converts DOCX -> text internally
                # (python-docx, not OCR) before sending it to Gemini - see
                # that module's docstring for why this conversion step is
                # needed at all.
                #
                # OLD OCR-BASED EXTRACTION
                # Kept as backup for now.
                # Gemini is currently used for invoice extraction.
                # word_data = self.document_processor.process_word_document(attachment_content, filename)
                # if word_data.get("processing_status") == "FAILED":
                #     result["error"] = word_data.get("error", "Word document processing failed")
                #     return result
                # extracted_list = self.document_service.extract_invoices_from_tables(
                #     word_data.get("tables", []), filename, source="DOCX",
                #     surrounding_text=word_data.get("text_content", "")
                # )
                # if not extracted_list:
                #     combined_text = word_data.get("text_content", "")
                #     for table in word_data.get("tables", []):
                #         for row in table:
                #             combined_text += "\n" + " ".join(str(c) for c in row if c)
                #     extracted_list = self.document_service.extract_invoices_from_text(
                #         combined_text, filename, source="DOCX"
                #     )
                gemini_result = self.document_service.extract_from_docx_bytes(
                    attachment_content, source_label="DOCX"
                )

            elif filename_lower.endswith(('.xlsx', '.xls')):
                # Same idea for XLSX (openpyxl, not OCR).
                #
                # OLD OCR-BASED EXTRACTION
                # Kept as backup for now.
                # Gemini is currently used for invoice extraction.
                # excel_data = self.document_processor.process_excel_document(attachment_content, filename)
                # if excel_data.get("processing_status") == "FAILED":
                #     result["error"] = excel_data.get("error", "Excel document processing failed")
                #     return result
                # sheet_tables = list(excel_data.get("sheets", {}).values())
                # all_cells_text = "\n".join(
                #     str(cell) for sheet_data in sheet_tables for row in sheet_data for cell in row
                # )
                # extracted_list = self.document_service.extract_invoices_from_tables(
                #     sheet_tables, filename, source="XLSX", surrounding_text=all_cells_text
                # )
                # if not extracted_list:
                #     combined_text = ""
                #     for sheet_name, sheet_data in excel_data.get("sheets", {}).items():
                #         combined_text += f"\nSheet: {sheet_name}\n"
                #         for row in sheet_data:
                #             combined_text += " ".join(str(c) for c in row if c) + "\n"
                #     extracted_list = self.document_service.extract_invoices_from_text(
                #         combined_text, filename, source="XLSX"
                #     )
                gemini_result = self.document_service.extract_from_xlsx_bytes(
                    attachment_content, source_label="XLSX"
                )

            else:
                # PDF and images - sent directly to Gemini as bytes. Gemini's
                # own multimodal understanding handles both text-based PDFs
                # and scanned/photographed content (no local OCR step), and
                # a single PDF containing multiple invoices naturally
                # produces multiple entries in the returned list.
                #
                # OLD OCR-BASED EXTRACTION
                # Kept as backup for now.
                # Gemini is currently used for invoice extraction.
                # source = "Image" if filename_lower.endswith(IMAGE_EXTENSIONS) else "PDF"
                # extracted_list = self.document_service.extract_invoices(
                #     attachment_content, filename, source=source
                # )
                gemini_input_content = attachment_content
                if filename_lower.endswith(IMAGE_EXTENSIONS):
                    source_label = "Image"
                    if filename_lower.endswith(('.tiff', '.bmp')):
                        # Gemini only accepts image/png, image/jpeg,
                        # image/webp, image/heic, image/heif - TIFF and BMP
                        # are rejected outright. Convert to PNG bytes with
                        # Pillow (already a project dependency) rather than
                        # let these files fail at the Gemini call.
                        from PIL import Image
                        import io as _io
                        img = Image.open(_io.BytesIO(attachment_content))
                        buf = _io.BytesIO()
                        img.convert("RGB").save(buf, format="PNG")
                        gemini_input_content = buf.getvalue()
                        mime_type = "image/png"
                    else:
                        mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
                else:
                    source_label = "PDF"
                    mime_type = "application/pdf"

                gemini_result = self.document_service.extract_from_bytes(
                    gemini_input_content, mime_type=mime_type, source_label=source_label
                )

            extracted_list = self._gemini_invoices_to_raw(gemini_result)

            if not extracted_list:
                result["error"] = "No data extracted"
                return result

            # Extraction genuinely succeeded (even if it turns out to
            # contain zero storable invoices) - mark this exact file content
            # as handled so a future identical resend is skipped. A FAILED
            # extraction above is deliberately NOT marked, so it remains
            # eligible for retry (e.g. after a bug fix).
            self._mark_document_processed(attachment_hash)

            for extracted_data in extracted_list:
                stored = self._finalize_and_store_invoice(extracted_data)
                if stored is None:
                    continue  # not likely an invoice, skip silently

                result["invoices_found"] += 1
                if stored:
                    result["invoices_stored"] += 1

            result["success"] = result["invoices_found"] > 0
            if result["success"]:
                logger.info(
                    f"Successfully processed attachment: {filename} "
                    f"({result['invoices_found']} invoice(s) found, {result['invoices_stored']} stored)"
                )
            else:
                result["error"] = "No invoice-like data found in attachment"

            FileUtils.cleanup_temp_directory()

            return result

        except Exception as e:
            error_msg = f"Failed to process attachment {filename}: {str(e)}"
            logger.error(error_msg)
            result["error"] = error_msg
            return result

    def process_single_attachment(self, message_id: str, attachment_id: str, filename: str) -> Dict[str, Any]:
        """Process a single attachment by ID (for testing)."""
        logger.info(f"Processing single attachment: {filename}")

        try:
            attachments = self.graph_service.get_attachments(message_id)
            attachment = next((a for a in attachments if a.get("id") == attachment_id), None)

            if not attachment:
                return {"success": False, "error": "Attachment not found"}

            return self._process_attachment(message_id, attachment)

        except Exception as e:
            error_msg = f"Failed to process single attachment: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}