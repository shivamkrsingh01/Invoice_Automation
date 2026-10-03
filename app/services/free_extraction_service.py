import fitz  # PyMuPDF
import pdfplumber
import pytesseract
from pytesseract import Output
from PIL import Image
import io
import re
import os
import shutil
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Optional
from app.utils.logging_config import logger
from app.utils.table_extraction import (
    looks_like_invoice_header,
    build_invoices_from_table,
    build_payment_advice_invoices_from_pages,
    looks_like_payment_advice_header,
    looks_like_headerless_batch_table,
    build_invoices_from_headerless_batches,
)

# --- Label vocabularies for the 8 canonical fields -------------------------
#
# STRONG labels are explicit, invoice-level labels - if one of these is
# found, we trust it immediately. WEAK labels ("total", "amount") are only
# used as a last resort, and only outside anything that looks like a
# line-item table, so a per-row "Amount" column (Description | Qty | Rate |
# Amount) is never mistaken for the invoice-level total.

STRONG_INVOICE_AMOUNT_LABELS = [
    "total invoice value",
    "invoice value",
    "invoice amount",
    "invoice amt",
    "inv amt",
    "bill amount",
    "grand total",
    "amount due",
    "balance due",
]
WEAK_INVOICE_AMOUNT_LABELS = ["total", "amount"]

TOTAL_PAYMENT_LABELS = [
    "total payment",
    "amount paid",
    "payment amount",
    "paid amount",
    "amount remitted",
    "total paid",
    # "Net Payable" / "Net Payment" is the common footer/summary label on
    # payment-advice-style invoice tables (e.g. email-body remittance
    # tables) for the net amount actually paid across the whole table.
    "net payable",
    "net payment",
    # "PAYMENT DONE Rs. X" is a common remittance-advice phrase for the
    # net amount actually paid on a specific invoice. This is a raw-text
    # extraction label only - unrelated to the calculated Yes/No "Payment
    # Done" output column (see InvoiceValidator.calculate_payment_done).
    "payment done",
]

UTR_LABELS = [
    "utr no", "utr number", "utr ref", "rtgs utr", "neft utr",
    "transaction reference", "transaction ref", "utr","UTR no.",
]

# Column-header words that mark a table as a per-line-item table rather
# than an invoice-level summary. Two or more of these on one line is a
# strong signal that any "Amount"/"Total" on that line (or the rows right
# under it) is a line-item value, not the invoice total.
LINE_ITEM_HEADER_HINTS = [
    "description", "particulars", "item", "qty", "quantity", "rate",
    "unit price", "hsn",
]

# Explicit, unambiguous "this line names an invoice/bill number" label
# patterns. Factored out so both _extract_invoice_number (single-value
# search over a whole block of text) and _extract_kv_block_invoices
# (splitting a multi-invoice block of text into one block per invoice)
# use exactly the same definition of "this looks like an invoice number
# label". Order matters for _extract_invoice_number: most specific first.
INVOICE_NUMBER_STRONG_PATTERNS = [
    r'\binvoice\s*number\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\binvoice\s*no\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\binvoice\s*#\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\binv\s*no\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\binv\s*#\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\bbill\s*number\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\bbill\s*no\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\binvoice\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    # negative lookahead: don't match "inv" when it's really the start of
    # the word "invoice" (that's handled by the patterns above)
    r'\binv(?!oice)\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
]
# Weaker, generic labels - only used as a last resort by
# _extract_invoice_number, and deliberately NOT used for block-splitting
# (too likely to false-match an unrelated "Reference: ..." or
# "Number: ..." line that isn't the start of a new invoice).
INVOICE_NUMBER_WEAK_PATTERNS = [
    r'\bnumber\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
    r'\breference\s*:?\s*([A-Z0-9][A-Z0-9\-/]*)',
]


class FreeExtractionService:
    """Free document extraction service using PyMuPDF, pdfplumber, and
    Tesseract OCR. Extracts ONLY the 8 canonical invoice fields (see
    app.models.invoice.InvoiceRecord) - no line items, no per-vendor extra
    fields, no other invoice metadata.

    Every public method takes a `source` string that the caller assigns
    based on which channel/format the data came from (e.g. "Email Body",
    "PDF", "DOCX", "Image", "XLSX"); the extractor stamps it onto every
    record it returns rather than guessing it itself.
    """

    def __init__(self):
        self.use_ocr = True  # Set to False if you don't want to use OCR

        # Locate Tesseract across platforms: prefer whatever's already on PATH
        # (normal on Linux/Mac via apt/brew), fall back to common Windows install paths.
        tesseract_on_path = shutil.which("tesseract")
        candidate_paths = [
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        ]

        if tesseract_on_path:
            logger.info(f"Tesseract found on PATH: {tesseract_on_path}")
        else:
            found = False
            for path in candidate_paths:
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    logger.info(f"Tesseract configured at: {path}")
                    found = True
                    break
            if not found:
                logger.warning(
                    "Tesseract not found on PATH or in common install locations - "
                    "OCR will fail. Install Tesseract and ensure it's on PATH, or "
                    "set pytesseract.pytesseract.tesseract_cmd manually."
                )

        logger.info("Free Extraction Service initialized (PyMuPDF + pdfplumber + Tesseract)")

    # -- Public entry points -------------------------------------------------

    def extract_invoice_from_text(self, text_content: str, filename: str = "text_content",
                                   source: str = "DOCX") -> Dict[str, Any]:
        """Extract invoice information from plain text (used as a fallback
        for email bodies, Word docs, Excel docs when no invoice table was
        found)."""
        logger.info(f"Extracting invoice from text content: {filename}")

        try:
            return self._parse_invoice_text(text_content, filename, source)
        except Exception as e:
            logger.error(f"Text extraction failed: {str(e)}")
            return {
                "source_filename": filename,
                "source": source,
                "processing_status": "FAILED",
                "error": str(e)
            }

    def extract_invoices_from_text(self, text_content: str, filename: str = "text_content",
                                    source: str = "DOCX") -> List[Dict[str, Any]]:
        """Extract one or MORE invoices from plain text (used as a fallback
        for Word/Excel docs when no structured table was found - see
        InvoiceProcessor._process_attachment). Unlike extract_invoice_from_text
        (singular, kept for backward compatibility), this can return several
        invoices when the text is several repeated 'Label: value' blocks or
        the 'CODE DT date = Rs.amount' remittance shape - the same two
        multi-invoice patterns already handled for email bodies and PDFs, so
        a Word doc listing multiple invoices as plain paragraphs (no real
        Word table) doesn't silently lose every invoice but the first."""
        if not text_content:
            return [self.extract_invoice_from_text(text_content, filename, source)]

        try:
            line_invoices = self._extract_line_pattern_invoices(text_content, filename, source)
            if line_invoices:
                logger.info(f"Extracted {len(line_invoices)} invoice(s) from line pattern: {filename}")
                return line_invoices

            kv_invoices = self._extract_kv_block_invoices(text_content, filename, source)
            if kv_invoices:
                logger.info(f"Extracted {len(kv_invoices)} invoice(s) from repeated label blocks: {filename}")
                return kv_invoices

            return [self._parse_invoice_text(text_content, filename, source)]
        except Exception as e:
            logger.error(f"Text extraction failed: {str(e)}")
            return [{
                "source_filename": filename,
                "source": source,
                "processing_status": "FAILED",
                "error": str(e)
            }]

    def extract_invoices(self, document_content: bytes, filename: str = "document",
                          source: str = "PDF") -> List[Dict[str, Any]]:
        """Extract one or MORE invoices from a PDF or image document.
        Handles both a single invoice per document and a table listing many
        invoices (e.g. a payment remittance advice), and both born-digital
        and scanned/rasterized PDFs."""
        logger.info(f"Extracting invoice(s): {filename}")

        try:
            # 0. Payment Advice / remittance documents: many bills listed in one
            #    table (Bill No / Bill Date / Gross Amount / Net Payment columns).
            #    Checked first since this column shape is distinct from a normal
            #    invoice header and needs its own field mapping (see
            #    app.utils.table_extraction and app.utils.validation).
            payment_advice_invoices = self._extract_payment_advice_pdfplumber(document_content, filename, source)
            if payment_advice_invoices:
                logger.info(f"Extracted {len(payment_advice_invoices)} bill(s) from Payment Advice: {filename}")
                return payment_advice_invoices

            # 1. Born-digital PDFs: look for a real table with an invoice-like header first.
            #    This is the most reliable path since there's no OCR guesswork involved.
            table_invoices = self._extract_table_invoices_pdfplumber(document_content, filename, source)
            if table_invoices:
                logger.info(f"Extracted {len(table_invoices)} invoice(s) from PDF table: {filename}")
                return table_invoices

            # 2. Normal text extraction (PyMuPDF, then pdfplumber if that's thin)
            text_data = self._extract_with_pymupdf(document_content)
            if not text_data or len(text_data.strip()) < 50:
                text_data = self._extract_with_pdfplumber(document_content)

            # 3. Still no usable text -> this is an image-based/scanned/printed PDF -> OCR.
            #    Try to reconstruct table rows/columns from OCR word positions first;
            #    only fall back to flat single-invoice parsing if that doesn't find a table.
            if self.use_ocr and (not text_data or len(text_data.strip()) < 50):
                ocr_invoices, ocr_text = self._extract_table_invoices_ocr(document_content, filename, source)
                if ocr_invoices:
                    logger.info(f"Extracted {len(ocr_invoices)} invoice(s) via OCR table reconstruction: {filename}")
                    return ocr_invoices
                text_data = ocr_text

            # 4. Try the remittance-style repeated line pattern (e.g.
            #    "WBBEL2510005615 DT 31.01.2026 = Rs.3033732.66") before
            #    falling back to single-invoice parsing.
            line_invoices = self._extract_line_pattern_invoices(text_data, filename, source)
            if line_invoices:
                logger.info(f"Extracted {len(line_invoices)} invoice(s) from line pattern: {filename}")
                return line_invoices

            # 5. Several invoices as repeated "Label: value" blocks (see
            #    _extract_kv_block_invoices for the shape this catches).
            kv_invoices = self._extract_kv_block_invoices(text_data, filename, source)
            if kv_invoices:
                logger.info(f"Extracted {len(kv_invoices)} invoice(s) from repeated label blocks: {filename}")
                return kv_invoices

            # 6. Fall back to single-invoice regex parsing on whatever text we have
            return [self._parse_invoice_text(text_data, filename, source)]

        except Exception as e:
            logger.error(f"Invoice extraction failed: {str(e)}")
            return [{
                "source_filename": filename,
                "source": source,
                "processing_status": "FAILED",
                "error": str(e)
            }]

    def extract_invoices_from_tables(self, tables: List[List[List[str]]], filename: str,
                                      source: str = "DOCX", surrounding_text: str = "") -> List[Dict[str, Any]]:
        """Given already-structured tables (e.g. from a Word doc or Excel
        sheet - list of tables, each a list of rows, each a list of cell
        strings), find any that look like an invoice list and build one
        invoice dict per row. Returns [] if no table looks like an invoice
        table (caller should fall back to flattened-text single-invoice
        extraction in that case).

        surrounding_text (optional) is any other text from the same
        document NOT part of the table(s) - e.g. the Word doc's own
        paragraphs above/below the table, where a vendor name or UTR is
        often written once rather than repeated in its own table column
        (the same gap already fixed for the HTML email-table and PDF-table
        paths). Backfilled the same way: only when unambiguous - see
        _extract_all_utrs/_extract_all_vendor_names docstrings."""
        all_invoices: List[Dict[str, Any]] = []
        for table in tables or []:
            if not table or len(table) < 2:
                continue
            for header_idx, row in enumerate(table[:3]):
                if looks_like_invoice_header(row):
                    invoices = build_invoices_from_table(
                        table, header_idx, filename,
                        shared_fields={"source": source},
                        normalize_date_fn=self._normalize_date
                    )
                    all_invoices.extend(invoices)
                    break

        if all_invoices and surrounding_text:
            distinct_utrs = list(dict.fromkeys(self._extract_all_utrs(surrounding_text)))
            utr_no = distinct_utrs[0] if len(distinct_utrs) == 1 else None
            distinct_vendors = list(dict.fromkeys(self._extract_all_vendor_names(surrounding_text)))
            vendor_name = distinct_vendors[0] if len(distinct_vendors) == 1 else None
            for record in all_invoices:
                if utr_no and not record.get("utr_no"):
                    record["utr_no"] = utr_no
                if vendor_name and not record.get("vendor_name"):
                    record["vendor_name"] = vendor_name

        return all_invoices

    def extract_invoices_from_html(self, html_content: str, filename: str = "email_body",
                                    source: str = "Email Body") -> List[Dict[str, Any]]:
        """Extract invoice(s) from a raw HTML email body, preserving <table>
        structure so a table listing multiple invoices isn't flattened into
        one blob of text (which would only ever yield the first invoice)."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content or "", "html.parser")

            all_invoices: List[Dict[str, Any]] = []
            all_rows_seen: List[List[str]] = []
            for table_tag in soup.find_all("table"):
                rows = []
                for tr in table_tag.find_all("tr"):
                    cells = [cell.get_text(strip=True) for cell in tr.find_all(["td", "th"])]
                    if any(c for c in cells):
                        rows.append(cells)

                if len(rows) < 2:
                    continue

                all_rows_seen.extend(rows)

                # Payment Advice detection first: a table with BOTH a Bill
                # No and a Net Payment column is a distinct shape (per-bill
                # payment, one shared Total Payment across the whole
                # document - see build_payment_advice_invoices_from_pages)
                # that the normal single-amount-column mapping below can't
                # represent. Checked before the normal path, mirroring the
                # PDF attachment path (_extract_payment_advice_pdfplumber),
                # so a Payment Advice table pasted into an email body gets
                # the same correct per-bill/shared-total handling a Payment
                # Advice PDF attachment already gets.
                payment_advice_invoices = build_payment_advice_invoices_from_pages(
                    [rows], filename,
                    shared_fields={"source": source},
                    normalize_date_fn=self._normalize_date,
                )
                if payment_advice_invoices:
                    all_invoices.extend(payment_advice_invoices)
                    continue

                found_header = False
                for header_idx, row in enumerate(rows[:3]):
                    if looks_like_invoice_header(row):
                        invoices = build_invoices_from_table(
                            rows, header_idx, filename,
                            shared_fields={"source": source},
                            normalize_date_fn=self._normalize_date
                        )
                        all_invoices.extend(invoices)
                        found_header = True
                        break

                if not found_header and looks_like_headerless_batch_table(rows):
                    # No header labels anywhere in this table - fall back to
                    # a pattern-based (date, bill/ref code, amount) read of
                    # the raw cell values. Only ever runs when the
                    # header-based path above found nothing for this table,
                    # so it can't change anything that already works. See
                    # app.utils.table_extraction.build_invoices_from_headerless_batches.
                    all_invoices.extend(build_invoices_from_headerless_batches(
                        rows, filename,
                        shared_fields={"source": source},
                        normalize_date_fn=self._normalize_date
                    ))

            if all_invoices:
                # Document-level fields (UTR, vendor name, a totals row
                # like "Net Payable") often sit OUTSIDE the per-invoice
                # rows - UTR/vendor in the email's surrounding prose (e.g.
                # "Vendor name: SHIV INDUSTRIES LTD" below the table), and
                # a payment total as its own summary row in the table
                # rather than a per-row column - so they're pulled once
                # here and backfilled onto every invoice from this email,
                # the same way the PDF/OCR paths already do.
                #
                # UTR/vendor are only backfilled when there's exactly ONE
                # distinct value found in the whole email - if the email
                # actually mentions several different UTRs or vendor
                # names, guessing which one belongs to a row that didn't
                # get its own from the table would risk attaching the
                # WRONG one, which is worse than leaving it blank.
                full_text = soup.get_text(separator="\n", strip=True)
                distinct_utrs = list(dict.fromkeys(self._extract_all_utrs(full_text)))
                utr_no = distinct_utrs[0] if len(distinct_utrs) == 1 else None
                distinct_vendors = list(dict.fromkeys(self._extract_all_vendor_names(full_text)))
                vendor_name = distinct_vendors[0] if len(distinct_vendors) == 1 else None
                total_payment = self._extract_row_footer_amount(
                    all_rows_seen, TOTAL_PAYMENT_LABELS + ["net payable"]
                )
                if total_payment is None:
                    total_payment = self._extract_total_payment(full_text)

                for record in all_invoices:
                    if utr_no and not record.get("utr_no"):
                        record["utr_no"] = utr_no
                    if vendor_name and not record.get("vendor_name"):
                        record["vendor_name"] = vendor_name
                    if total_payment is not None and not record.get("total_payment"):
                        record["total_payment"] = total_payment

                return all_invoices

            # No invoice table found in the HTML - try the remittance-style
            # repeated line pattern (e.g. "WBBEL2510005615 DT 31.01.2026 =
            # Rs.3033732.66"), using a line-preserving text flatten so the
            # pattern (and the single-invoice fallback below) can still tell
            # where one line ends and the next begins.
            plain_text = soup.get_text(separator="\n", strip=True)

            line_invoices = self._extract_line_pattern_invoices(plain_text, filename, source)
            if line_invoices:
                return line_invoices

            # Several invoices as repeated "Label: value" blocks (e.g. a
            # typed/forwarded email listing multiple invoices one after
            # another, with varying label wording and no reliable blank-line
            # separation) - checked before giving up to single-invoice.
            kv_invoices = self._extract_kv_block_invoices(plain_text, filename, source)
            if kv_invoices:
                logger.info(f"Extracted {len(kv_invoices)} invoice(s) from repeated label blocks: {filename}")
                return kv_invoices

            # Still nothing recognizable -> fall back to single-invoice extraction
            return [self._parse_invoice_text(plain_text, filename, source)]

        except Exception as e:
            logger.error(f"HTML invoice extraction failed: {str(e)}")
            return [{
                "source_filename": filename,
                "source": source,
                "processing_status": "FAILED",
                "error": str(e)
            }]

    # -- Multi-invoice detection helpers -------------------------------------

    def _extract_line_pattern_invoices(self, text: str, filename: str, source: str) -> List[Dict[str, Any]]:
        """Detect a common remittance-advice plain-text pattern where several
        invoices are listed as repeated lines, not a table, e.g.:
            WBBEL2510005615 DT 31.01.2026 = Rs.3033732.66
            LESS- TDS .1% = Rs. 2570.96
            ...
        This is a specific recurring shape (ID, then 'DT', then a date, then
        '=', then an amount) - not free-form prose - so it can be matched
        reliably even though it isn't inside an HTML/PDF table. Returns []
        if the pattern isn't found, so the caller can fall back further."""
        if not text:
            return []

        pattern = re.compile(
            r'([A-Z]{2,}[A-Z0-9\-/]{4,})\s+DT\s+(\d{1,2}[./]\d{1,2}[./]\d{2,4})\s*=\s*(?:Rs\.?|INR|₹)\s*([\d,]+\.?\d*)',
            re.IGNORECASE
        )
        matches = list(pattern.finditer(text))
        if not matches:
            return []

        # Fields shared by every invoice in this document (vendor, UTR)
        shared_fields: Dict[str, Any] = {"source": source}
        vendor_match = re.search(r'beneficiary\s+name\s*:?\s*([A-Za-z0-9 &.,\-]+)', text, re.IGNORECASE)
        if vendor_match:
            shared_fields["vendor_name"] = vendor_match.group(1).strip()
        utr = self._extract_utr(text)
        if utr:
            shared_fields["utr_no"] = utr

        invoices: List[Dict[str, Any]] = []
        for i, m in enumerate(matches):
            invoice_number, date_str, amount_str = m.group(1), m.group(2), m.group(3)
            try:
                amount = Decimal(amount_str.replace(',', ''))
            except InvalidOperation:
                amount = None

            record: Dict[str, Any] = {
                "source_filename": filename,
                "processing_status": "PROCESSED",
                "invoice_number": invoice_number.strip(),
                "invoice_date": self._normalize_date(date_str),
            }
            if amount is not None:
                record["invoice_amount"] = amount

            # This invoice's own block of text (e.g. its TDS/credit-note
            # deductions and a "PAYMENT DONE Rs. X" line) runs from the end
            # of this match to the start of the next invoice's line, or to
            # the end of the text for the last invoice. Total payment is
            # looked up within just this block, so each invoice gets its
            # own paid amount rather than a document-wide total.
            block_start = m.end()
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block_text = text[block_start:block_end]
            total_payment = self._extract_total_payment(block_text)
            if total_payment is not None:
                record["total_payment"] = total_payment

            record.update(shared_fields)
            invoices.append(record)

        return invoices

    def _extract_kv_block_invoices(self, text: str, filename: str, source: str) -> List[Dict[str, Any]]:
        """Detect several invoices written as repeated 'Label: value' lines
        in plain prose/email text - NOT an HTML <table> (handled by
        extract_invoices_from_html's table branch) and NOT the specific
        'CODE DT date = Rs.amount' remittance shape (handled by
        _extract_line_pattern_invoices above). This is the common shape of
        a forwarded/typed email listing several invoices one after another,
        e.g.:

            Invoice Number: INV-2026-444
            Invoice Date: 10-09-2026
            Invoice Amount: 90000
            Vendor Name: SHI Technologies Pvt Ltd

            Invoice Number: INV-2026-333
            ...

        Each invoice's block may use different label wording (Bill No /
        Invoice # / Inv No / Invoice Number, ...) and blocks are NOT
        guaranteed to be separated by a blank line - so this splits on
        every line that looks like a new invoice-number label instead of
        on blank lines. Returns [] (so the caller falls back further) if
        fewer than 2 such lines are found; a single one is exactly the
        existing single-invoice case, already handled by
        _parse_invoice_text.
        """
        if not text:
            return []

        lines = text.split('\n')
        starts: List[int] = []
        for i, line in enumerate(lines):
            for pattern in INVOICE_NUMBER_STRONG_PATTERNS:
                match = re.match(r'\s*' + pattern, line, re.IGNORECASE)
                if match:
                    value = match.group(1).strip()
                    if len(value) >= 3 and any(c.isdigit() for c in value):
                        starts.append(i)
                    break

        if len(starts) < 2:
            return []

        # A UTR / vendor name that appears only ONCE for the whole email
        # (e.g. mentioned once near the top or bottom rather than repeated
        # per invoice) is backfilled onto any block that doesn't have its
        # own value - the same approach already used for UTR in
        # _extract_line_pattern_invoices and extract_invoices_from_html.
        #
        # This is only safe when the value is UNAMBIGUOUS. If the email
        # actually contains SEVERAL DIFFERENT UTRs (or vendor names) -
        # e.g. a batch email where different invoices were paid under
        # different UTRs - guessing which one belongs to a block that
        # didn't state its own would risk attaching the WRONG UTR to an
        # invoice, which is worse than leaving it blank. So this only
        # backfills when every UTR (or vendor name) found anywhere in the
        # email is the same single value.
        shared_fields: Dict[str, Any] = {}
        distinct_utrs = list(dict.fromkeys(self._extract_all_utrs(text)))
        if len(distinct_utrs) == 1:
            shared_fields["utr_no"] = distinct_utrs[0]
        distinct_vendors = list(dict.fromkeys(self._extract_all_vendor_names(text)))
        if len(distinct_vendors) == 1:
            shared_fields["vendor_name"] = distinct_vendors[0]

        invoices: List[Dict[str, Any]] = []
        for idx, start_line in enumerate(starts):
            end_line = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            block_text = '\n'.join(lines[start_line:end_line])

            record = self._parse_invoice_text(block_text, filename, source)
            for key, value in shared_fields.items():
                if not record.get(key):
                    record[key] = value
            if record.get("processing_status") == "NOT_INVOICE" and record.get("invoice_number"):
                record["processing_status"] = "PROCESSED"
            invoices.append(record)

        return invoices

    def _extract_payment_advice_pdfplumber(self, document_content: bytes, filename: str,
                                            source: str) -> List[Dict[str, Any]]:
        """Detect and extract a Payment Advice / remittance document - one
        PDF listing many bills in a single table - into one canonical
        record per bill (see app.utils.table_extraction.
        build_payment_advice_invoices_from_pages for the field mapping and
        multi-page/line-wrap handling). Returns [] if this document
        doesn't look like a Payment Advice, so the caller falls back to
        the normal invoice extraction paths untouched."""
        try:
            with pdfplumber.open(io.BytesIO(document_content)) as pdf:
                pages_tables: List[List[List[str]]] = []
                full_text = ""
                for page in pdf.pages:
                    full_text += (page.extract_text() or "") + "\n"
                    page_rows: List[List[str]] = []
                    for table in page.extract_tables():
                        page_rows.extend(table)
                    pages_tables.append(page_rows)

            invoices = build_payment_advice_invoices_from_pages(
                pages_tables, filename,
                shared_fields={"source": source},
                normalize_date_fn=self._normalize_date,
            )
            if not invoices:
                return []

            # Document-level fields (vendor, UTR) aren't inside the bill
            # table itself, so they're pulled from the surrounding page
            # text once and applied to every bill from this document.
            vendor_name = self._extract_payment_advice_vendor(full_text)
            utr_no = self._extract_payment_advice_utr(full_text)

            for record in invoices:
                if vendor_name and not record.get("vendor_name"):
                    record["vendor_name"] = vendor_name
                if utr_no and not record.get("utr_no"):
                    record["utr_no"] = utr_no

            return invoices

        except Exception as e:
            logger.error(f"Payment Advice extraction failed for {filename}: {str(e)}")
            return []

    def _extract_payment_advice_vendor(self, text: str) -> Optional[str]:
        """Extract the document-level vendor name from a Payment Advice
        (the payee the advice was addressed to), e.g.
        'Vendor Name : HINDALCO INDUSTRIES LTD'. Generic - not tied to
        any specific vendor."""
        match = re.search(
            r'vendor\s*name\s*:?\s*([A-Za-z][A-Za-z0-9 &.,\-]*?)'
            r'(?=\s*document\s*no|\s*address|\n|$)',
            text, re.IGNORECASE
        )
        if match:
            name = match.group(1).strip()
            return name or None
        return None

    def _extract_payment_advice_utr(self, text: str) -> Optional[str]:
        """Extract an actual UTR/RTGS/NEFT reference number from a Payment
        Advice, if one is present. Many remittance advices only state the
        payment MODE (e.g. 'RTGS/NEFT Reference : RTGS PAYMENT') with no
        real reference number - in that case this deliberately returns
        None rather than inventing a UTR from the word "RTGS"/"PAYMENT"."""
        match = re.search(r'RTGS\s*/?\s*NEFT\s*Reference\s*:?\s*([^\n]+)', text, re.IGNORECASE)
        if not match:
            return None
        value = match.group(1).strip()
        if not any(c.isdigit() for c in value):
            return None
        ref_match = re.search(r'[A-Za-z0-9]{6,25}', value)
        return ref_match.group(0) if ref_match else None

    def _extract_table_invoices_pdfplumber(self, document_content: bytes, filename: str,
                                            source: str) -> List[Dict[str, Any]]:
        """Look for a born-digital (real text layer) table that looks like an
        invoice list, using pdfplumber's table detection."""
        all_invoices: List[Dict[str, Any]] = []
        full_text = ""
        try:
            with pdfplumber.open(io.BytesIO(document_content)) as pdf:
                for page in pdf.pages:
                    full_text += (page.extract_text() or "") + "\n"
                    tables = page.extract_tables()
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        found_header = False
                        for header_idx, row in enumerate(table[:3]):
                            if looks_like_invoice_header(row):
                                invoices = build_invoices_from_table(
                                    table, header_idx, filename,
                                    shared_fields={"source": source},
                                    normalize_date_fn=self._normalize_date
                                )
                                all_invoices.extend(invoices)
                                found_header = True
                                break
                        if not found_header and looks_like_headerless_batch_table(table):
                            # No header labels - fall back to the same
                            # pattern-based reconstruction used for the
                            # email-body path. See
                            # app.utils.table_extraction.build_invoices_from_headerless_batches.
                            all_invoices.extend(build_invoices_from_headerless_batches(
                                table, filename,
                                shared_fields={"source": source},
                                normalize_date_fn=self._normalize_date
                            ))
        except Exception as e:
            logger.error(f"pdfplumber table extraction failed: {str(e)}")
            return all_invoices

        # Document-level fields (vendor, UTR) often sit in surrounding page
        # text ABOVE or BELOW the table rather than in a column of their
        # own (e.g. "Vendor Name: ..." / "UTR: ..." printed once under the
        # table) - same gap already fixed for the HTML email-table path, so
        # applied here too. Only backfilled when unambiguous (see
        # _extract_all_utrs/_extract_all_vendor_names docstrings) so a
        # document with several different UTRs/vendors doesn't get a
        # guessed value attached to a row that didn't have its own.
        if all_invoices and full_text:
            distinct_utrs = list(dict.fromkeys(self._extract_all_utrs(full_text)))
            utr_no = distinct_utrs[0] if len(distinct_utrs) == 1 else None
            distinct_vendors = list(dict.fromkeys(self._extract_all_vendor_names(full_text)))
            vendor_name = distinct_vendors[0] if len(distinct_vendors) == 1 else None
            for record in all_invoices:
                if utr_no and not record.get("utr_no"):
                    record["utr_no"] = utr_no
                if vendor_name and not record.get("vendor_name"):
                    record["vendor_name"] = vendor_name

        return all_invoices

    def _extract_table_invoices_ocr(self, document_content: bytes, filename: str, source: str):
        """OCR a scanned/rasterized PDF at high resolution and reconstruct table
        rows/columns from word bounding boxes, then look for an invoice table.
        Returns (invoices_list, full_plain_text) - invoices_list is [] if no
        table was detected; full_plain_text is used as a fallback for the
        single-invoice regex path."""
        full_text = ""
        all_invoices: List[Dict[str, Any]] = []

        try:
            doc = fitz.open(stream=document_content, filetype="pdf")

            for page_num in range(len(doc)):
                page = doc[page_num]
                # 4x zoom (~288 DPI) - noticeably better OCR accuracy than the
                # previous 3x (~216 DPI) on dense tables with small fonts.
                zoom_matrix = fitz.Matrix(4, 4)
                pix = page.get_pixmap(matrix=zoom_matrix)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                # --psm 6: treat the page as a single uniform block of text,
                # which tends to preserve row order better for tables than the
                # default page-segmentation mode.
                page_text = pytesseract.image_to_string(img, config="--psm 6")
                full_text += page_text + "\n"

                try:
                    ocr_data = pytesseract.image_to_data(img, config="--psm 6", output_type=Output.DICT)
                    rows = self._reconstruct_rows_from_ocr(ocr_data)

                    found_header = False
                    for header_idx, row in enumerate(rows[:5]):
                        if looks_like_invoice_header(row):
                            page_invoices = build_invoices_from_table(
                                rows, header_idx, filename,
                                shared_fields={"source": source},
                                normalize_date_fn=self._normalize_date
                            )
                            if page_invoices:
                                all_invoices.extend(page_invoices)
                            found_header = True
                            break

                    if not found_header and looks_like_headerless_batch_table(rows):
                        # No header labels - fall back to the same
                        # pattern-based reconstruction used for the
                        # email-body path. OCR noise means this is more
                        # error-prone here than on born-digital text, but
                        # it's still strictly additive - only runs when the
                        # header-based path found nothing on this page. See
                        # app.utils.table_extraction.build_invoices_from_headerless_batches.
                        page_invoices = build_invoices_from_headerless_batches(
                            rows, filename,
                            shared_fields={"source": source},
                            normalize_date_fn=self._normalize_date
                        )
                        if page_invoices:
                            all_invoices.extend(page_invoices)
                except Exception as e:
                    logger.warning(f"OCR table reconstruction failed for a page, continuing with plain text: {str(e)}")

            doc.close()
        except Exception as e:
            logger.error(f"OCR extraction failed: {str(e)}")

        # Document-level fields (UTR, vendor) often sit in surrounding prose
        # above/below the table (e.g. "...vide UTR no.XXXX...") rather than
        # in a table column of their own, so - same as the Payment Advice
        # and line-pattern paths - they're pulled from the full OCR'd text
        # once and applied to every row that doesn't already have them from
        # the table itself.
        if all_invoices:
            utr_no = self._extract_utr(full_text)
            vendor_name = self._extract_vendor_name(full_text)
            for record in all_invoices:
                if utr_no and not record.get("utr_no"):
                    record["utr_no"] = utr_no
                if vendor_name and not record.get("vendor_name"):
                    record["vendor_name"] = vendor_name

        return all_invoices, full_text

    def _reconstruct_rows_from_ocr(self, ocr_data: Dict) -> List[List[str]]:
        """Group OCR word boxes into rows (by vertical position) and cells
        within each row (by horizontal gaps between words), approximating a
        table structure from an image with no real text layer."""
        words = []
        n = len(ocr_data.get("text", []))
        for i in range(n):
            text = (ocr_data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(ocr_data.get("conf", ["-1"] * n)[i])
            except (ValueError, TypeError):
                conf = -1
            if conf < 30:  # skip low-confidence noise that tends to garble cells
                continue
            words.append({
                "text": text,
                "left": ocr_data["left"][i],
                "top": ocr_data["top"][i],
                "width": ocr_data["width"][i],
                "height": ocr_data["height"][i],
            })

        if not words:
            return []

        # Group words into lines by vertical center, tolerant of small misalignment
        words.sort(key=lambda w: w["top"])
        lines = [[words[0]]]
        line_center = words[0]["top"] + words[0]["height"] / 2

        for w in words[1:]:
            w_center = w["top"] + w["height"] / 2
            if abs(w_center - line_center) <= (w["height"] * 0.7):
                lines[-1].append(w)
            else:
                lines.append([w])
                line_center = w_center

        # Within each line, sort left-to-right and split into cells on horizontal gaps
        rows = []
        for line in lines:
            line.sort(key=lambda w: w["left"])
            cells = []
            current_words = [line[0]["text"]]
            prev_right = line[0]["left"] + line[0]["width"]
            avg_char_width = max(line[0]["width"] / max(len(line[0]["text"]), 1), 8)

            for w in line[1:]:
                gap = w["left"] - prev_right
                if gap > avg_char_width * 2.5:  # wide gap = new column
                    cells.append(" ".join(current_words))
                    current_words = [w["text"]]
                else:
                    current_words.append(w["text"])
                prev_right = w["left"] + w["width"]
                avg_char_width = max(w["width"] / max(len(w["text"]), 1), 8)

            cells.append(" ".join(current_words))
            rows.append(cells)

        return rows

    # -- Raw text extraction (no field parsing) ------------------------------

    def _extract_with_pymupdf(self, document_content: bytes) -> str:
        """Extract text using PyMuPDF (fitz)."""
        try:
            doc = fitz.open(stream=document_content, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            return text
        except Exception as e:
            logger.error(f"PyMuPDF extraction failed: {str(e)}")
            return ""

    def _extract_with_pdfplumber(self, document_content: bytes) -> str:
        """Extract text using pdfplumber (better for tables)."""
        try:
            text = ""
            with pdfplumber.open(io.BytesIO(document_content)) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""
            return text
        except Exception as e:
            logger.error(f"pdfplumber extraction failed: {str(e)}")
            return ""

    def _extract_with_ocr(self, document_content: bytes) -> str:
        """Extract text using Tesseract OCR."""
        try:
            doc = fitz.open(stream=document_content, filetype="pdf")
            text = ""

            for page_num in range(len(doc)):
                page = doc[page_num]
                # Render page to image at higher resolution (4x = ~288 DPI) for
                # much better OCR accuracy on dense tables/small fonts. Default
                # get_pixmap() renders at 72 DPI which is too low for tables
                # with small text and causes misreads (e.g. dates).
                zoom_matrix = fitz.Matrix(4, 4)
                pix = page.get_pixmap(matrix=zoom_matrix)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                page_text = pytesseract.image_to_string(img)
                text += page_text + "\n"

            doc.close()
            return text
        except Exception as e:
            logger.error(f"OCR extraction failed: {str(e)}")
            return ""

    # -- Single-invoice, plain-text field parsing (only the 8 fields) -------

    def _parse_invoice_text(self, text: str, filename: str, source: str) -> Dict[str, Any]:
        """Parse extracted text to find ONLY the 8 canonical invoice fields.
        Anything that cannot be confidently identified is left as None -
        nothing here is ever guessed."""
        extracted: Dict[str, Any] = {
            "source_filename": filename,
            "source": source,
            "processing_status": "PROCESSED",
        }

        try:
            text = self._clean_text(text)

            extracted["invoice_number"] = self._extract_invoice_number(text)
            extracted["invoice_date"] = self._extract_date(text, ["invoice date", "inv date", "bill date", "dated"])
            extracted["invoice_amount"] = self._extract_invoice_amount(text)
            extracted["total_payment"] = self._extract_total_payment(text)
            extracted["utr_no"] = self._extract_utr(text)
            extracted["vendor_name"] = self._extract_vendor_name(text)

            if not any([extracted["invoice_number"], extracted["invoice_amount"], extracted["vendor_name"]]):
                extracted["processing_status"] = "NOT_INVOICE"
                logger.warning(f"Document may not be an invoice: {filename}")

            logger.info(
                f"Extracted invoice data - Invoice #: {extracted.get('invoice_number', 'N/A')}, "
                f"Vendor: {extracted.get('vendor_name', 'N/A')}, "
                f"Invoice Amount: {extracted.get('invoice_amount', 'N/A')}, "
                f"Date: {extracted.get('invoice_date', 'N/A')}"
            )

        except Exception as e:
            logger.error(f"Error parsing invoice text: {str(e)}")
            extracted["processing_status"] = "FAILED"
            extracted["error"] = str(e)

        return extracted

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text WHILE PRESERVING LINE BREAKS.
        Keyword-proximity extraction (e.g. 'date: ... ' followed by a
        newline) depends on real newlines to know where to stop looking -
        collapsing everything to one line let it silently grab values from
        unrelated parts of the document."""
        if not text:
            return ""
        lines = text.split('\n')
        cleaned_lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines]
        text = '\n'.join(line for line in cleaned_lines if line)
        # Fix common OCR errors
        text = text.replace('|', 'I')
        return text.strip()

    def _extract_invoice_number(self, text: str) -> Optional[str]:
        """Extract invoice number, strongest/most explicit label first.

        The label word itself (e.g. "Number", "No") is matched literally in
        each pattern rather than left to an optional `\\s*#?\\s*:?\\s*` gap -
        with re.IGNORECASE, a loose gap lets the capture group swallow the
        label word itself (e.g. "Invoice Number: INV-2001" would otherwise
        capture "Number" instead of "INV-2001", since the [A-Z0-9...] class
        also matches lowercase letters under IGNORECASE).
        """
        patterns = INVOICE_NUMBER_STRONG_PATTERNS + INVOICE_NUMBER_WEAK_PATTERNS

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                invoice_num = match.group(1).strip()
                # A real invoice number reliably contains a digit - this
                # also rejects an accidental match on a label word itself
                # (e.g. "Number", "To") if one ever slips through.
                if len(invoice_num) >= 3 and any(c.isdigit() for c in invoice_num):
                    return invoice_num

        return None

    def _extract_date(self, text: str, keywords: List[str]) -> Optional[str]:
        """Extract a date only when it appears near one of the given
        keywords. Deliberately has NO blind fallback that grabs "any date
        in the document" - that would be guessing, which is not allowed
        for a required field."""
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',  # YYYY-MM-DD
            r'\d{1,2}\.\d{1,2}\.\d{2,4}',  # DD.MM.YYYY (or YY) - common in Indian docs
            r'\d{2}/\d{2}/\d{4}',  # MM/DD/YYYY
            r'\d{2}-\d{2}-\d{4}',  # MM-DD-YYYY
            r'\d{1,2}-[A-Za-z]{3}-\d{2,4}',  # DD-MMM-YY / DD-MMM-YYYY
            r'\d{1,2}\s+[A-Za-z]{3}\s+\d{4}',  # DD MMM YYYY
            r'\d{1,2}\s+[A-Za-z]+\s+\d{4}'  # DD Month YYYY
        ]

        for keyword in keywords:
            # Find text near the keyword, bounded to the same line.
            keyword_pattern = rf'{keyword}[:\s]+(.*?)(?:\n|$)'
            match = re.search(keyword_pattern, text, re.IGNORECASE)
            if match:
                context = match.group(1)
                for date_pattern in date_patterns:
                    date_match = re.search(date_pattern, context)
                    if date_match:
                        return self._normalize_date(date_match.group(0))

        return None

    def _normalize_date(self, date_str: str) -> str:
        """Normalize date to YYYY-MM-DD format."""
        try:
            date_str = date_str.strip()

            # Try YYYY-MM-DD
            if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                return date_str

            # Dot-separated is used throughout Indian invoices/remittances and
            # is always day-first: DD.MM.YYYY or DD.MM.YY
            dot_match = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$', date_str)
            if dot_match:
                day, month, year = dot_match.groups()
                if len(year) == 2:
                    year = f"20{year}"
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            # DD-MMM-YY / DD-MMM-YYYY (e.g. "2-Feb-26")
            mon_match = re.match(r'^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})$', date_str)
            if mon_match:
                day, mon_name, year = mon_match.groups()
                if len(year) == 2:
                    year = f"20{year}"
                try:
                    from datetime import datetime as _dt
                    parsed = _dt.strptime(f"{day}-{mon_name}-{year}", "%d-%b-%Y")
                    return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    return date_str

            # MM/DD/YYYY or MM-DD-YYYY
            match = re.match(r'^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$', date_str)
            if match:
                month, day, year = match.groups()
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            # For other formats, return as-is for now
            return date_str

        except Exception:
            return date_str

    def _extract_vendor_name(self, text: str) -> Optional[str]:
        """Extract vendor name, strongest/most explicit label first."""
        patterns = [
            r'vendor\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'beneficiary\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'payee\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'from\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'vendor\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'seller\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'company\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|$)'
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Clean up common suffixes
                name = re.sub(r'\s+(?:ltd|inc|corp|llc|pvt| ltd| pvt ltd)\.?$', '', name, flags=re.IGNORECASE)
                return name if len(name) > 2 else None

        return None

    def _amount_near_label(self, text: str, label: str) -> Optional[str]:
        """Find a number that appears close to `label`, bounded to the same
        line and a short distance after the label - so it can't reach
        across into an unrelated value elsewhere in the document. Returns
        the raw numeric string (comma-stripped), or None."""
        pattern = rf'{re.escape(label)}[ \t:]{{0,3}}[^\d\n]{{0,20}}([\d,]+\.?\d*)'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).replace(',', '').strip()
        return None

    def _iter_amounts_near_label(self, text: str, label: str):
        """Same matching as _amount_near_label, but returns every match in
        the text (used for the weak-label fallback, where we need to check
        each candidate against the line-item exclusion zones)."""
        pattern = rf'{re.escape(label)}[ \t:]{{0,3}}[^\d\n]{{0,20}}([\d,]+\.?\d*)'
        return re.finditer(pattern, text, re.IGNORECASE)

    def _find_line_item_ranges(self, text: str) -> List[tuple]:
        """Return character-offset ranges in `text` that look like a
        per-line-item table (e.g. a "Description | Qty | Rate | Amount"
        header and the rows under it), so weak keyword extraction can skip
        them and avoid mistaking a per-line "Amount" column for the
        invoice-level total."""
        ranges = []
        lines = text.split('\n')

        line_offsets = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line) + 1  # +1 for the '\n' consumed by split()

        for i, line in enumerate(lines):
            lower = line.lower()
            hint_hits = sum(1 for hint in LINE_ITEM_HEADER_HINTS if hint in lower)
            if hint_hits < 2:
                continue

            # This line looks like a line-item table header. Treat it and
            # the following rows (up to the next blank line, or 15 lines,
            # whichever comes first) as the line-item table body.
            start = line_offsets[i]
            end_line = min(i + 15, len(lines) - 1)
            for j in range(i + 1, len(lines)):
                if not lines[j].strip() or j >= i + 15:
                    end_line = j
                    break
            end = line_offsets[end_line] + len(lines[end_line]) if end_line < len(lines) else offset
            ranges.append((start, end))

        return ranges

    @staticmethod
    def _in_ranges(pos: int, ranges: List[tuple]) -> bool:
        return any(start <= pos <= end for start, end in ranges)

    def _extract_invoice_amount(self, text: str) -> Optional[str]:
        """Find the invoice-level total amount.

        1. Prefer STRONG, unambiguous invoice-level labels (Invoice
           Amount, Invoice Value, Grand Total, Bill Amount, ...).
        2. Only if none of those are found, fall back to weak generic
           labels ("Total"/"Amount") - but skip any occurrence that falls
           inside what looks like a per-line-item table, since that
           "Amount" almost certainly refers to a single line, not the
           whole invoice.
        """
        for label in STRONG_INVOICE_AMOUNT_LABELS:
            amount = self._amount_near_label(text, label)
            if amount is not None:
                return amount

        line_item_ranges = self._find_line_item_ranges(text)
        for label in WEAK_INVOICE_AMOUNT_LABELS:
            for match in self._iter_amounts_near_label(text, label):
                if not self._in_ranges(match.start(), line_item_ranges):
                    return match.group(1).replace(',', '').strip()

        return None

    def _extract_total_payment(self, text: str) -> Optional[str]:
        """Find the total amount actually paid/remitted. Only explicit
        labels are used - there is no generic fallback, since guessing this
        field wrong directly changes the (calculated) Payment Done value."""
        for label in TOTAL_PAYMENT_LABELS:
            amount = self._amount_near_label(text, label)
            if amount is not None:
                return amount
        return None

    def _extract_row_footer_amount(self, rows: List[List[str]], labels: List[str]) -> Optional[str]:
        """Scan a table's raw rows (list of cell-string lists, as taken
        straight from <tr>/<td>) for a summary row where the label and the
        amount are two cells of the SAME row - e.g. ['NET PAYABLE',
        '13,670,590.51'] - rather than a per-invoice column.

        Email-body invoice tables commonly show per-invoice columns (date,
        number, amount) and then a single trailing totals row like this
        instead of a dedicated 'Total Payment' column, so this is checked
        separately from the column-header mapping in
        app.utils.table_extraction.build_invoices_from_table (which drops
        this row entirely, since it has no invoice number/date)."""
        for row in rows:
            row_text = " ".join((cell or "").strip() for cell in row if (cell or "").strip())
            if not row_text:
                continue
            for label in labels:
                match = re.search(
                    rf'\b{re.escape(label)}\b[:\s]{{0,3}}([\d,]+\.?\d*)',
                    row_text, re.IGNORECASE
                )
                if match:
                    return match.group(1).replace(',', '').strip()
        return None

    def _extract_utr(self, text: str) -> Optional[str]:
        """Extract a UTR / bank transaction reference number.

        Checks the common "(UTR): <value>" abbreviation form first, since
        real-world phrasing like "Unique Transaction Reference Number
        (UTR): KKBKR..." would otherwise let a label like "transaction
        reference" match too early and capture the word "Number" (the rest
        of the label) instead of the actual value.
        """
        paren_match = re.search(r'\(UTR\)\s*:?\s*([A-Za-z0-9]{6,25})', text, re.IGNORECASE)
        if paren_match:
            value = paren_match.group(1).strip()
            if any(c.isdigit() for c in value):
                return value

        for label in UTR_LABELS:
            pattern = rf'\b{re.escape(label)}\b[:.\s]{{0,3}}([A-Za-z0-9]{{6,25}})'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                # A real UTR reliably contains a digit - this also rejects
                # an accidental match on a trailing label word (e.g.
                # "Number") if the label itself is a substring of a longer
                # phrase, the same class of issue fixed in
                # _extract_invoice_number.
                if any(c.isdigit() for c in value):
                    return value

        return None

    def _extract_all_utrs(self, text: str) -> List[str]:
        """Like _extract_utr, but returns EVERY UTR-shaped value found in
        the text (using whichever label pattern first produces a match),
        not just the first. Used by _extract_kv_block_invoices to check
        whether a document mentions one UTR (safe to backfill onto blocks
        with no UTR of their own) or several different ones (unsafe to
        guess - see that method's docstring)."""
        if not text:
            return []

        paren_matches = [
            m.group(1).strip()
            for m in re.finditer(r'\(UTR\)\s*:?\s*([A-Za-z0-9]{6,25})', text, re.IGNORECASE)
        ]
        paren_matches = [v for v in paren_matches if any(c.isdigit() for c in v)]
        if paren_matches:
            return paren_matches

        for label in UTR_LABELS:
            pattern = rf'\b{re.escape(label)}\b[:.\s]{{0,3}}([A-Za-z0-9]{{6,25}})'
            values = [m.strip() for m in re.findall(pattern, text, re.IGNORECASE)]
            values = [v for v in values if any(c.isdigit() for c in v)]
            if values:
                return values

        return []

    def _extract_all_vendor_names(self, text: str) -> List[str]:
        """Like _extract_vendor_name, but returns EVERY vendor-name-shaped
        value found in the text (using whichever label pattern first
        produces a match), not just the first. Used by
        _extract_kv_block_invoices for the same "only backfill if
        unambiguous" reason as _extract_all_utrs."""
        if not text:
            return []

        patterns = [
            r'vendor\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'beneficiary\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'payee\s+name\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'from\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'vendor\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'seller\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|phone|$)',
            r'company\s*:?\s*([A-Z][A-Za-z\s&]+?)(?:\n|address|$)'
        ]
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                names = []
                for raw in matches:
                    name = raw.strip()
                    name = re.sub(r'\s+(?:ltd|inc|corp|llc|pvt| ltd| pvt ltd)\.?$', '', name, flags=re.IGNORECASE)
                    if len(name) > 2:
                        names.append(name)
                if names:
                    return names

        return []