

"""Gemini-based invoice extraction service - STANDALONE.

Used by app/services/invoice_processor.py as the active invoice extraction
layer. The old OCR-based FreeExtractionService (PyMuPDF/pdfplumber/
Tesseract) is kept in the project as a disabled backup only.

How documents get to Gemini:
- PDF and images are sent to Gemini directly as bytes (extract_from_bytes).
  Gemini's own document/image understanding handles both text-based PDFs
  and scanned/photographed ones - no local OCR step, Tesseract/
  pytesseract are not imported anywhere in this file.
- DOCX and XLSX are the one exception: Gemini's API does not accept those
  binary formats as a document part the way it does PDF/images. This
  service first pulls the raw text/cell content out of them using
  python-docx / openpyxl (already project dependencies, used elsewhere
  for the same purpose) and sends that as plain text instead. This is
  ordinary structured file parsing, not optical character recognition -
  it doesn't reintroduce an OCR dependency, it's just the necessary
  conversion step for formats Gemini can't read as binary documents.
- Email body / HTML is sent to Gemini as plain text (extract_from_text) -
  Gemini reads HTML tables fine as text, no special handling needed.

Two requested output fields - invoice_source and email_timestamp - are
NOT things Gemini can determine from the document content itself: the
model has no way to know which channel a file arrived through, or when
the email was received. Asking it to fill those in would only invite
guessing, which the task explicitly forbids ("never guess or invent
values"). Instead, Gemini is only asked for the six fields genuinely
derivable from content; invoice_source and email_timestamp are filled in
afterward from metadata the CALLER already knows and passes in. The
final returned shape still matches the requested schema exactly - see
_run_extraction() below.
"""
import io
import logging
import re
import time
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF - used only to split a very long PDF into page-range
             # chunks before sending it to Gemini; see extract_from_bytes, Tuple

import fitz  # PyMuPDF - already a project dependency (requirements.txt)

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema Gemini is asked to fill in - content-derived fields only. See the
# module docstring for why invoice_source/email_timestamp aren't here.
# ---------------------------------------------------------------------------

class _ExtractedInvoiceContent(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    invoice_amount: Optional[float] = None
    payment_amount: Optional[float] = None
    utr_number: Optional[str] = None
    total_payment: Optional[float] = None
    total_net_payment: Optional[float] = None
    vendor_name: Optional[str] = None


class _PaymentBatch(BaseModel):
    """One payment table / batch in the document and the invoices in it.

    Gemini is asked to say WHICH invoices belong to each batch (by invoice
    number) instead of repeating the batch's UTR on every row itself.
    Row-by-row copying based on "nearest subtotal" is what let a UTR drift
    onto the wrong group of rows - the membership list here is applied in
    code (see _apply_payment_batches), so the model's own uncertainty about
    where one group ends and the next begins can't silently spread a UTR
    across more invoices than it actually covers."""
    utr_number: Optional[str] = None
    total_payment: Optional[float] = None
    total_net_payment: Optional[float] = None
    invoice_numbers: List[str] = []


class _ExtractionContentResult(BaseModel):
    invoices: List[_ExtractedInvoiceContent]
    payment_batches: List[_PaymentBatch] = []


EXTRACTION_INSTRUCTIONS = """Extract invoice data from business documents (email, PDF, image, DOCX, XLSX). For each distinct invoice found, extract:

- invoice_number: Unique identifier (labels: Invoice No, Invoice Number, Inv No, Bill No, Invoice #)
- invoice_date: Issue date (labels: Invoice Date, Bill Date, Inv Date). Normalize to YYYY-MM-DD if confident; otherwise return as written
- invoice_amount: Total billed amount (grand total)
- payment_amount: Amount paid specifically for THIS invoice (labels: Payment Done, Net Payment Done, Amount Paid, Paid Amount, Net Payment when per-invoice). Use final stated figure; don't recompute from deductions. NOT the same as total_payment/total_net_payment (batch-level). Return null if no per-invoice paid amount stated
- utr_number: Bank UTR/transaction reference
- total_payment: Batch/table total before deductions (if distinguished from invoice_amount)
- total_net_payment: Final net amount after all deductions (labels: Net Payment, Net Payable, Net Amount Payable, Net Amount). Table-level figure. If no net figure printed, return null
- vendor_name: Vendor/supplier name (issuer, not recipient)

Rules:
1. Return null if value not clearly present. Never guess, estimate, infer, or invent
2. Multiple invoices → one array entry per invoice. No invoices → empty array
3. Match by meaning, not exact labels. Never fabricate values
4. Numbers: plain (no symbols, no separators) e.g. 45250.75 not "Rs. 45,250.75". Negative: trailing minus (e.g. "2,377.00-" → -2377.00). Parentheses (e.g. "(2,500.00)") are positive unless text clearly indicates deduction
5. Preserve exact decimals - no rounding/truncing

Payment batches (document can have one or several): Each batch = group of invoice rows with shared UTR/total. Report in "payment_batches" array:
- utr_number: UTR for that batch (exact)
- total_payment: Batch total payment (exact digits, no rounding)
- total_net_payment: Batch final net payment (or null if none printed)
- invoice_numbers: invoice_number of every row in this batch

Batch assignment rules:
- UTR/totals can be above/below/beside rows. Use document structure (tables, borders, headings, blank rows, which rows subtotal adds to)
- Use arithmetic check: row amounts should sum to batch total (allowing for deductions)
- One UTR/total = one batch. Never split UTR across groups, never leave group without its UTR
- Single batch document = one entry with all invoices
- No UTR/totals = empty payment_batches array
- Batch = genuinely shared value (one UTR, one combined total printed once). If each invoice has its own total_payment label (e.g. "Total Payment: X" under individual invoice), that's per-invoice value → put on invoice's total_payment field, not batch. In that case, batch's total_payment/total_net_payment = null (no combined figure)
- Per-invoice fields: fill utr_number/total_payment/total_net_payment from invoice's own label if present, else from batch's shared value, else null. Batch list fixes rows where shared value wasn't repeated; never erases invoice's own stated value
6. Never copy batch-level figure to payment_amount. payment_amount = null unless document states paid amount for that specific invoice
"""


class GeminiExtractionService:
    """Standalone invoice extraction using Gemini's multimodal understanding.
    Not yet called by the email processing pipeline - see module docstring."""

    # Official model limits from https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
    MAX_INPUT_TOKENS = 1_048_576
    MAX_OUTPUT_TOKENS = 65_536

    def __init__(self):
        if not settings.gemini_api_key:
            raise ValueError(
                "Gemini is not configured - set GEMINI_API_KEY in .env "
                "(get a free key at https://aistudio.google.com/apikey)"
            )
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = "gemini-3.1-flash-lite"
        self.configured_max_output_tokens = 65536  # From GenerateContentConfig in _run_extraction

        # Log model limits at initialization
        logger.info(
            f"[GEMINI LIMITS] Model: {self.model} | "
            f"Max input: {self.MAX_INPUT_TOKENS} | "
            f"Max output: {self.MAX_OUTPUT_TOKENS} | "
            f"Configured output limit: {self.configured_max_output_tokens}"
        )

    # -- Public API -----------------------------------------------------

    def extract_from_bytes(
        self,
        content: bytes,
        mime_type: str,
        source_label: str,
        email_timestamp: Optional[str] = None,    
    ) -> Dict[str, Any]:
        """Extract invoices from raw PDF or image bytes.

        `mime_type` must be a type Gemini accepts as a document part
        directly - "application/pdf" or an "image/*" type (png, jpeg,
        webp, etc). For DOCX/XLSX use extract_from_docx_bytes() /
        extract_from_xlsx_bytes() instead - see module docstring for why.

        `source_label` and `email_timestamp` are metadata the CALLER
        already knows (e.g. "PDF attachment: invoice.pdf", the email's
        received time) - not extracted from the content itself.

        A PDF longer than settings.pdf_chunk_threshold_pages is
        automatically split into smaller groups of pages and merged - see
        _extract_pdf_in_chunks. This is invisible to the caller: the
        return shape is identical either way.
        """
        if mime_type == "application/pdf":
            try:
                page_count = fitz.open(stream=content, filetype="pdf").page_count
            except Exception as e:
                logger.warning(f"Could not read PDF page count for {source_label} ({e}) - sending as one piece")
                page_count = 1
            if page_count > settings.pdf_chunk_threshold_pages:
                return self._extract_pdf_in_chunks(
                    content, page_count, source_label, email_timestamp
                )

        part = types.Part.from_bytes(data=content, mime_type=mime_type)
        return self._run_extraction(
            contents=[part],
            source_label=source_label,
            email_timestamp=email_timestamp,
        )

    def _extract_pdf_in_chunks(
        self,
        content: bytes,
        page_count: int,
        source_label: str,
        email_timestamp: Optional[str],
    ) -> Dict[str, Any]:
        """Split a long PDF into groups of settings.pdf_chunk_size_pages
        pages, extract each group with its own Gemini call, and merge the
        results.

        Why: a single call reading a very long PDF can finish normally
        (finish_reason STOP, no error) while having silently failed to
        read everything in the middle of the document - a known weak spot
        of asking a model to read one very long document in one pass. Each
        chunk here stays small enough to read reliably.

        Chunks overlap by one page, and each chunk gets its own UTR/batch
        matching via _run_extraction -> _apply_payment_batches, exactly as
        a single-call document would. Known limitation: if one payment
        batch's rows genuinely span more pages than the overlap covers (a
        shape not seen in any real document so far - every vendor so far
        puts one payment advice on one page), the earlier chunk's partial
        view of it is kept over the later chunk's complete view, since
        de-duplication below keeps the first occurrence by invoice_number.
        Raise the overlap, or lower pdf_chunk_size_pages, if a real vendor
        document turns out to need it.

        If any chunk fails (including the MAX_TOKENS case in
        _run_extraction), the WHOLE document is a failure - no partial
        result is returned - so the person investigating sees exactly
        which page range broke and why, rather than a silently incomplete
        "success" or an ambiguous count mismatch.
        """
        size, overlap = settings.pdf_chunk_size_pages, 1
        ranges = []
        pos = 0
        while pos < page_count:
            end = min(pos + size - 1, page_count - 1)
            ranges.append((pos, end))
            if end == page_count - 1:
                break
            pos = end + 1 - overlap

        logger.info(
            f"{source_label}: {page_count} pages, splitting into {len(ranges)} "
            f"chunks of up to {size} pages (one call each) instead of one call "
            f"for the whole document"
        )

        src = fitz.open(stream=content, filetype="pdf")
        all_invoices: List[Dict[str, Any]] = []
        seen_invoice_keys: set = set()
        total_gemini_time = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cached_tokens = 0

        for i, (start, end) in enumerate(ranges, start=1):
            chunk_label = f"{source_label} (pages {start + 1}-{end + 1} of {page_count})"
            chunk_doc = fitz.open()
            chunk_doc.insert_pdf(src, from_page=start, to_page=end)
            chunk_bytes = chunk_doc.tobytes()

            logger.info(f"Extracting chunk {i}/{len(ranges)}: {chunk_label}")
            part = types.Part.from_bytes(data=chunk_bytes, mime_type="application/pdf")
            result = self._run_extraction(
                contents=[part],
                source_label=chunk_label,
                email_timestamp=email_timestamp,
            )
            # _run_extraction already raises on a chunk that fails after
            # its own retries (including MAX_TOKENS) - nothing further to
            # catch here; letting it propagate is the point (see docstring).

            # Aggregate token metadata across chunks
            metadata = result.get("_gemini_metadata", {})
            total_gemini_time += metadata.get("time_sec", 0)
            if metadata.get("input_tokens") is not None:
                total_input_tokens += metadata["input_tokens"]
            if metadata.get("output_tokens") is not None:
                total_output_tokens += metadata["output_tokens"]
            if metadata.get("cached_tokens") is not None:
                total_cached_tokens += metadata["cached_tokens"]

            chunk_invoice_count = len(result["invoices"])
            for inv in result["invoices"]:
                key = self._norm_invoice_no(inv.get("invoice_number"))
                if key and key in seen_invoice_keys:
                    continue  # duplicate from the 1-page overlap - keep the earlier copy
                if key:
                    seen_invoice_keys.add(key)
                all_invoices.append(inv)
            logger.info(
                f"Chunk {i}/{len(ranges)} ({chunk_label}): {chunk_invoice_count} invoice(s) "
                f"({len(all_invoices)} total so far after de-duplication)"
            )

        logger.info(
            f"{source_label}: all {len(ranges)} chunks extracted successfully, "
            f"{len(all_invoices)} invoice(s) total"
        )

        # Log aggregated PDF timing and token usage
        logger.info(
            f"[PDF] {source_label} | Total Gemini time: {total_gemini_time:.2f} sec | "
            f"Input: {total_input_tokens if total_input_tokens else 'unavailable'} | "
            f"Output: {total_output_tokens if total_output_tokens else 'unavailable'} | "
            f"Total: {total_input_tokens + total_output_tokens if (total_input_tokens and total_output_tokens) else 'unavailable'}"
        )

        result = {"invoices": all_invoices}
        result["_gemini_metadata"] = {
            "time_sec": total_gemini_time,
            "input_tokens": total_input_tokens if total_input_tokens else None,
            "output_tokens": total_output_tokens if total_output_tokens else None,
            "total_tokens": (total_input_tokens + total_output_tokens) if (total_input_tokens and total_output_tokens) else None,
            "cached_tokens": total_cached_tokens if total_cached_tokens else None,
        }
        return result

    def extract_from_text(
        self,
        text: str,
        source_label: str,
        email_timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract invoices from plain text or HTML content - use this for
        the email body, and for DOCX/XLSX content already converted to
        text by the two helpers below."""
        return self._run_extraction(
            contents=[text],
            source_label=source_label,
            email_timestamp=email_timestamp,
        )

    def extract_from_docx_bytes(
        self,
        content: bytes,
        source_label: str,
        email_timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DOCX -> plain text (via python-docx, not OCR) -> extract_from_text()."""
        text = self._docx_to_text(content)
        return self.extract_from_text(text, source_label, email_timestamp)

    def extract_from_xlsx_bytes(
        self,
        content: bytes,
        source_label: str,
        email_timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """XLSX -> plain text table (via openpyxl, not OCR) -> extract_from_text()."""
        text = self._xlsx_to_text(content)
        return self.extract_from_text(text, source_label, email_timestamp)

    # -- Internal ---------------------------------------------------------

    def _run_extraction(
        self,
        contents: list,
        source_label: str,
        email_timestamp: Optional[str],
    ) -> Dict[str, Any]:
        # Gemini intermittently returns 503/429 or a malformed/truncated
        # JSON reply. Retry a couple of times before giving up so one bad
        # response doesn't silently cost a whole document.
        parsed: Optional[_ExtractionContentResult] = None
        last_error: Optional[Exception] = None
        gemini_start_time = time.perf_counter()
        final_response = None

        for attempt in range(1, 4):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=EXTRACTION_INSTRUCTIONS,
                        response_mime_type="application/json",
                        response_schema=_ExtractionContentResult,
                        temperature=0.0,  # data extraction, not creative writing - keep it deterministic
                        # Without an explicit ceiling this falls back to the
                        # model's own default, which a document with many
                        # invoices (e.g. a large batch payment advice) can
                        # exceed. Because the output is schema-constrained
                        # JSON, running out of room does NOT raise an error -
                        # the SDK just closes the array early at the last
                        # complete invoice, so a 500-invoice document can
                        # silently come back with only e.g. 240. The
                        # finish_reason check below is what actually catches
                        # this; this ceiling just gives it more room first.
                        max_output_tokens=65536,
                    ),
                )
                final_response = response

                finish_reason = None
                try:
                    finish_reason = response.candidates[0].finish_reason
                except (AttributeError, IndexError, TypeError):
                    pass
                if finish_reason is not None and str(finish_reason).upper().endswith("MAX_TOKENS"):
                    # Retrying won't help - temperature=0.0 and the same
                    # input will hit the same ceiling again. Fail loudly
                    # instead of silently handing back a partial result that
                    # looks identical to a complete, successful one.
                    raise RuntimeError(
                        "Gemini's response was cut off (MAX_TOKENS) before finishing "
                        "this document - it likely has too many invoices for one "
                        "extraction call. Partial results are NOT used, to avoid "
                        "silently storing an incomplete document as if it succeeded."
                    )

                parsed = getattr(response, "parsed", None)
                if parsed is None:
                    # Fallback in case this SDK/version didn't auto-parse - validate
                    # the raw JSON text against the same schema by hand.
                    parsed = _ExtractionContentResult.model_validate_json(response.text)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Gemini extraction attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    time.sleep(2 * attempt)
        if parsed is None:
            raise last_error

        # Extract token usage from response metadata
        gemini_time = time.perf_counter() - gemini_start_time
        input_tokens = None
        output_tokens = None
        total_tokens = None
        cached_tokens = None

        try:
            usage_metadata = getattr(final_response, "usage_metadata", None)
            if usage_metadata:
                input_tokens = getattr(usage_metadata, "prompt_token_count", None)
                output_tokens = getattr(usage_metadata, "candidates_token_count", None)
                total_tokens = getattr(usage_metadata, "total_token_count", None)
                cached_tokens = getattr(usage_metadata, "cached_content_token_count", None)
        except (AttributeError, TypeError):
            pass

        # Log Gemini timing and token usage
        input_str = str(input_tokens) if input_tokens is not None else "unavailable"
        output_str = str(output_tokens) if output_tokens is not None else "unavailable"
        total_str = str(total_tokens) if total_tokens is not None else "unavailable"
        cached_str = f" (cached: {cached_tokens})" if cached_tokens is not None else ""

        logger.info(
            f"[GEMINI] Model: {self.model} | Time: {gemini_time:.2f} sec | "
            f"Input: {input_str} | Output: {output_str} | Total: {total_str}{cached_str}"
        )

        # Log token limit utilization if we have actual usage
        if input_tokens is not None and self.MAX_INPUT_TOKENS:
            input_pct = (input_tokens / self.MAX_INPUT_TOKENS) * 100
            logger.info(
                f"[GEMINI LIMITS] Input: {input_tokens} / {self.MAX_INPUT_TOKENS} ({input_pct:.2f}%)"
            )
        if output_tokens is not None and self.configured_max_output_tokens:
            output_pct = (output_tokens / self.configured_max_output_tokens) * 100
            logger.info(
                f"[GEMINI LIMITS] Output: {output_tokens} / {self.configured_max_output_tokens} ({output_pct:.2f}%)"
            )

        # Return result with token metadata attached
        result = {"invoices": []}
        try:
            # Merge Gemini's content-derived fields with the caller-supplied
            # metadata to produce the exact output shape the task requires.
            invoices = []
            for inv in parsed.invoices:
                invoices.append({
                    "invoice_number": inv.invoice_number,
                    "invoice_date": inv.invoice_date,
                    "invoice_amount": inv.invoice_amount,
                    "payment_amount": inv.payment_amount,
                    "utr_number": inv.utr_number,
                    "total_payment": inv.total_payment,
                    "total_net_payment": inv.total_net_payment,
                    "vendor_name": inv.vendor_name,
                    "invoice_source": source_label,
                    "email_timestamp": email_timestamp,
                })

            self._apply_payment_batches(invoices, parsed.payment_batches)
            result = {"invoices": invoices}
        finally:
            # Always attach token metadata for upstream aggregation
            result["_gemini_metadata"] = {
                "time_sec": gemini_time,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cached_tokens": cached_tokens,
            }

        return result

    # -- Batch assignment ---------------------------------------------------

    @staticmethod
    def _norm_invoice_no(value: Optional[str]) -> str:
        """Comparison key for invoice numbers: case/space/punctuation
        insensitive, so "APRKT 2510051648" matches "APRKT2510051648"."""
        return re.sub(r"[^A-Z0-9]", "", (value or "").upper())

    @classmethod
    def _apply_payment_batches(cls, invoices: List[Dict[str, Any]], batches) -> None:
        """Apply each batch's utr_number / total_payment / total_net_payment
        to its member invoices, using the batch's invoice_numbers list.
        Mutates `invoices` in place.

        Only overwrites a field when the BATCH actually states a value for
        it. A batch is often just a shared UTR grouping invoices that each
        already carry their own, independently stated total_payment (e.g.
        several invoices paid under one UTR, each individually labelled
        "Total Payment: X" in the document) - if the batch itself has no
        combined total_payment/total_net_payment, that must never blank out
        a value the model already read directly off that invoice.

        Invoices not named in any batch keep whatever the model returned on
        the row itself. An invoice named in two batches is left untouched
        (ambiguous - better to keep the row's own value than to guess).
        """
        if not batches:
            return

        owner: Dict[str, int] = {}
        ambiguous = set()
        for idx, batch in enumerate(batches):
            for number in batch.invoice_numbers:
                key = cls._norm_invoice_no(number)
                if not key:
                    continue
                if key in owner and owner[key] != idx:
                    ambiguous.add(key)
                owner[key] = idx

        for inv in invoices:
            key = cls._norm_invoice_no(inv.get("invoice_number"))
            if not key or key in ambiguous or key not in owner:
                continue
            batch = batches[owner[key]]
            if batch.utr_number is not None:
                inv["utr_number"] = batch.utr_number
            if batch.total_payment is not None:
                inv["total_payment"] = batch.total_payment
            if batch.total_net_payment is not None:
                inv["total_net_payment"] = batch.total_net_payment

        # Sanity log: a batch's rows should roughly add up to its total.
        # Informational only - deductions legitimately make them differ.
        for idx, batch in enumerate(batches):
            members = [i for i in invoices
                       if owner.get(cls._norm_invoice_no(i.get("invoice_number"))) == idx]
            amounts = [i.get("invoice_amount") for i in members]
            if batch.total_payment is None or not members or any(a is None for a in amounts):
                continue
            row_sum = sum(amounts)
            if abs(row_sum - batch.total_payment) > 1.0:
                logger.info(
                    f"Payment batch {batch.utr_number}: rows sum to {row_sum:.2f} "
                    f"but total_payment is {batch.total_payment:.2f} "
                    f"(may be normal if deductions apply)"
                )

    @staticmethod
    def _docx_to_text(content: bytes) -> str:
        from docx import Document

        doc = Document(io.BytesIO(content))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)

    @staticmethod
    def _xlsx_to_text(content: bytes) -> str:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f"--- Sheet: {sheet.title} ---")
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(lines)
