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


EXTRACTION_INSTRUCTIONS = """You are an invoice data extraction system. You will be shown the content of a business document (an email, a PDF, an image of an invoice, or extracted text from a DOCX/XLSX file). It may be a single invoice, multiple invoices in one document, a payment advice, or something that is not an invoice at all.

Extract the following fields for EVERY distinct invoice you find in the content:

- invoice_number: The invoice's own identifying number. Field labels that mean this: "Invoice No", "Invoice Number", "Inv No", "Bill No", "Invoice #".
- invoice_date: The date the invoice was issued. Field labels that mean this: "Invoice Date", "Bill Date", "Inv Date". Normalize to YYYY-MM-DD when you are confident of the day/month/year; otherwise return the date exactly as written.
- invoice_amount: The total amount the invoice is billed for (the invoice's total/grand total).
- payment_amount: The amount that has actually been paid TOWARD THIS SPECIFIC INVOICE INDIVIDUALLY, if the document states one for that invoice by itself. This is different from invoice_amount - it's what was paid, not what was billed. Field labels that mean this: "Payment Done", "Net Payment Done", "Amount Paid", "Paid Amount", "Net Payment" (when printed once per invoice, not once for a whole table - see total_net_payment below for that case). This applies just as much to a plain narrative/paragraph-style payment advice (e.g. one paragraph per invoice, each ending in its own "PAYMENT DONE Rs. X" line, possibly after lines showing TDS or credit-note deductions worked out above it) as it does to a table - do not extract this only from tables. Use the FINAL stated figure for that invoice; do not recompute it yourself from the deduction lines even if you could. It is also different from total_payment/total_net_payment (see rule 6 below), which are batch-level - do NOT fill payment_amount with a batch-level value, and do NOT leave payment_amount null just because the invoice also happens to share a batch UTR with other invoices. If the document does not state a per-invoice paid amount for this specific invoice, return null for payment_amount even when total_payment/total_net_payment is known for the batch.
- utr_number: The bank UTR / transaction reference / payment reference number, if present.
- total_payment: The batch/table total amount payable/due (before the final deductions that give total_net_payment), if the document distinguishes this from invoice_amount (e.g. after taxes or adjustments). If the document does not distinguish it from invoice_amount, return null rather than guessing.
- total_net_payment: The FINAL NET amount of the payment table/batch this invoice belongs to, after all deductions (TDS, debits/advances, discounts). Field labels that mean this: "Net Payment" (in the table's Total row), "Net Payable", "Net Amount Payable", "Net Amount". It is a table-level figure, so every invoice in that table gets the same value. Example: a Total row whose Net Payment column reads 13,113,325.39 gives 13113325.39; a "NET PAYABLE 13,670,690.51" line under the table gives 13670690.51. It is NOT the gross/total before deductions. If no net figure is printed for the table, return null - never compute it yourself.
- vendor_name: The name of the vendor/supplier who issued the invoice (not the recipient/customer). This is used to tell apart invoices from different vendors that happen to share an invoice number, so extract it whenever the document identifies who issued the invoice.

Rules you must follow exactly:
1. If a field's value is not clearly present in the content, return null for that field. Do NOT guess, estimate, infer from context, or invent a value under any circumstances.
2. If the document contains multiple distinct invoices (e.g. a multi-invoice PDF, or a table listing several invoices), return one entry in the "invoices" array per invoice.
3. If the content contains no invoice at all, return an empty "invoices" array.
4. Different vendors use different field names and layouts - use your understanding of the document's meaning, not just exact label matching, to find the right value. But never fabricate a value that isn't actually present.
5. Numbers must be plain numbers (no currency symbols, no thousands separators) - e.g. 45250.75, not "Rs. 45,250.75". A NEGATIVE amount is written in these documents with a trailing minus sign, e.g. "2,377.00-" or "11,267.63-" (a deduction such as TDS or Adv/Debit) - return that as a negative number, e.g. -2377.00. Parentheses around an amount, e.g. "(2,500.00)", are NOT a negative sign in these documents - they are just a way of drawing attention to or separating the figure. Return the plain positive number for a parenthesized amount, e.g. 2500.00, unless the surrounding text itself makes clear it is a deduction (in which case follow that wording, not the parentheses).
6. Payment batches. A document can hold ONE or SEVERAL payment tables/batches - each is a group of invoice rows with its own UTR (bank payment reference), total payment and net payment. Report them in the top-level "payment_batches" array, one entry per batch:
   - utr_number: the UTR printed for that batch, exactly as written.
   - total_payment: that batch's total payment amount (exact digits, e.g. 1541514.29 - never rounded).
   - total_net_payment: that batch's final net payment/net payable (see the total_net_payment field above), or null if none is printed.
   - invoice_numbers: the invoice_number of EVERY invoice row that belongs to that batch, copied exactly as you return them in "invoices".
   How to decide which rows belong to which batch:
   - The UTR / totals of a batch can be printed ABOVE its rows (a header), BELOW its rows (a subtotal or Total row) or beside them. Do NOT assume one direction. Use the document's own structure: separate tables, borders, headings, blank rows, and which rows a subtotal/Total row actually adds up.
   - Check with arithmetic when you can: the invoice amounts of a batch's rows normally add up to (or, after deductions, come close to) that batch's total payment. If a group of rows adds up to a printed total, that total and its UTR belong to that group and to no other group.
   - A UTR/total belongs to exactly ONE batch. Never give the same UTR to two different groups of rows unless the document truly shows them paid under that one UTR, and never leave a group without its UTR when the document prints one for it.
   - If the whole document is a single batch, return a single entry listing every invoice.
   - If the document has no UTR/payment totals at all, return an empty "payment_batches" array.
   - A batch is for a value that is genuinely SHARED across its rows (one UTR, one combined total, printed once for the group) - not for a value that is separately printed for each invoice. If instead each invoice states its OWN total_payment individually (e.g. "Total Payment: X" written right under that one invoice's own details, not in a group subtotal), that is a per-invoice value: put it directly on that invoice's own total_payment field, exactly like invoice_amount, EVEN IF several such invoices also happen to share one UTR and therefore belong to the same batch for utr_number purposes. In that case the batch's own total_payment/total_net_payment should be null (there is no combined figure to report), and each invoice keeps the individual value you read for it - do not average, sum, copy one invoice's value onto another, or blank them because a batch exists.
   In the per-invoice fields, still fill utr_number, total_payment and total_net_payment with your best value for that invoice - from its own explicit label if it has one, otherwise from its batch's shared value if the batch has one, otherwise null. The batch list exists to fix rows where a shared value was not repeated on every row; it is never a reason to erase a value an invoice already states for itself.
7. Do NOT propagate any batch-level figure into payment_amount. payment_amount stays null unless the document states a paid amount for that specific invoice on its own (e.g. a per-row Net Payment column in a payment advice).
8. Preserve exact decimal digits as printed - do not round or truncate amounts.
"""


class GeminiExtractionService:
    """Standalone invoice extraction using Gemini's multimodal understanding.
    Not yet called by the email processing pipeline - see module docstring."""

    def __init__(self):
        if not settings.gemini_api_key:
            raise ValueError(
                "Gemini is not configured - set GEMINI_API_KEY in .env "
                "(get a free key at https://aistudio.google.com/apikey)"
            )
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = "gemini-3.1-flash-lite"

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
        """
        part = types.Part.from_bytes(data=content, mime_type=mime_type)
        return self._run_extraction(
            contents=[EXTRACTION_INSTRUCTIONS, part],
            source_label=source_label,
            email_timestamp=email_timestamp,
        )

    def extract_from_text(
        self,
        text: str,
        source_label: str,
        email_timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract invoices from plain text or HTML content - use this for
        the email body, and for DOCX/XLSX content already converted to
        text by the two helpers below."""
        prompt = f"{EXTRACTION_INSTRUCTIONS}\n\nDocument content:\n{text}"
        return self._run_extraction(
            contents=[prompt],
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
        for attempt in range(1, 4):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_ExtractionContentResult,
                        temperature=0.0,  # data extraction, not creative writing - keep it deterministic
                    ),
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
        return {"invoices": invoices}

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