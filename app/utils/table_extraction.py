
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Optional, Callable


HEADER_KEYWORDS = {
    "invoice_amount": [
        "total invoice value", "invoice value", "gross invoice value", "invoice amount",
        "invoice amt", "inv amt", "bill amount", "gross amount", "grand total",
        "amount due", "balance due", "total due",
    ],
    "total_payment": [
        "total payment", "amount paid", "payment amount", "paid amount",
        "amount remitted", "total paid",
    ],
    "utr_no": [
        "utr no", "utr number", "utr ref", "rtgs utr", "neft utr",
        "transaction reference", "transaction ref", "utr","Unique Transaction Reference Number (UTR)",
    ],
    "invoice_date": ["invoice date", "inv date", "bill date", "bill dt", "dated"],
    "invoice_number": [
        "invoice number", "invoice no", "invoice #", "inv no", "inv #",
        "bill number", "bill no", "bill reference", "bill ref",
    ],
    "vendor_name": [
        "vendor name", "beneficiary name", "payee name", "beneficiary",
        "vendor", "seller", "company name",
    ],
}


FIELD_PRIORITY = [
    "invoice_amount", "total_payment", "utr_no", "invoice_date",
    "invoice_number", "vendor_name",
]

# Fields whose raw text should be parsed as a number
NUMERIC_FIELDS = {"invoice_amount", "total_payment"}
# Fields whose raw text should be normalized as a date
DATE_FIELDS = {"invoice_date"}

# Words that mark a row as a footer/summary/adjustment line rather than an
# actual invoice (e.g. "LESS: IT TDS on Goods 194Q (0.1%)", "Grand Total")
FOOTER_KEYWORDS = [
    "less:", "less ", "tds", "grand total", "sub total", "subtotal",
    "total:", "discount", "freight", "shipping", "rounding", "adjustment",
]


def _normalize_header_cell(cell: str) -> str:
    """Normalize a header cell for keyword matching by lowercasing and
    stripping everything except letters/digits. This makes header
    matching tolerant of spacing/punctuation differences between vendors'
    PDF layouts - e.g. 'Bill No', 'BillNo', 'Bill No.' and 'Bill Number'
    all normalize to a form containing 'billno', and 'Gross Amount' /
    'GrossAmount' both normalize to 'grossamount'."""
    return re.sub(r'[^a-z0-9]', '', (cell or '').lower())


def _looks_like_invoice_number(value: str) -> bool:
    """Reject values that are clearly a footer/summary label rather than a
    real invoice number (e.g. 'LESS: IT TDS on Goods 194Q (0.1%)')."""
    if not value:
        return False
    value_lower = value.lower()
    if any(kw in value_lower for kw in FOOTER_KEYWORDS):
        return False
    # A real invoice number is short-ish and has no more than one or two
    # spaces (codes like "INV 2024" are fine; sentences are not)
    if value.count(" ") > 2 or len(value) > 40:
        return False
    return any(c.isdigit() for c in value)


def map_table_headers(header_row: List[str]) -> Dict[int, str]:
    """Map column index -> canonical field name based on header cell text.
    Each canonical field is only assigned to one column (first/best match).

    Matching is tried two ways so header variants like 'Bill No.' /
    'BillNo' both match the same 'bill no' keyword: first the original
    (space-preserving) substring check, then a punctuation/space-stripped
    normalized check (see _normalize_header_cell) as a fallback.
    """
    col_map: Dict[int, str] = {}
    used_fields = set()

    for idx, cell in enumerate(header_row or []):
        cell_clean = (cell or "").strip().lower()
        if not cell_clean:
            continue
        cell_norm = _normalize_header_cell(cell)
        for field in FIELD_PRIORITY:
            if field in used_fields:
                continue
            keywords = HEADER_KEYWORDS[field]
            if any(kw in cell_clean for kw in keywords) or \
               any(_normalize_header_cell(kw) in cell_norm for kw in keywords):
                col_map[idx] = field
                used_fields.add(field)
                break

    return col_map



PAYMENT_ADVICE_HEADER_KEYWORDS = {
    "invoice_number": ["billno", "billnumber", "invoiceno", "invoicenumber"],
    "invoice_date": ["billdate"],
    "invoice_amount": ["grossamount", "grossamt", "invoiceamount", "invoiceamt"],
    "net_payment": ["netpayment", "netamt", "netamount", "paymentdone"],
}
PAYMENT_ADVICE_FIELD_PRIORITY = [
    "net_payment", "invoice_amount", "invoice_number", "invoice_date",
]


def map_payment_advice_headers(header_row: List[str]) -> Dict[int, str]:
    """Same idea as map_table_headers, but for the Payment Advice column
    set. Only ever produces the canonical fields plus the intermediate
    'net_payment' marker (consumed by build_payment_advice_invoices_from_pages,
    never stored directly - see app.utils.validation)."""
    col_map: Dict[int, str] = {}
    used_fields = set()

    for idx, cell in enumerate(header_row or []):
        cell_norm = _normalize_header_cell(cell)
        if not cell_norm:
            continue
        for field in PAYMENT_ADVICE_FIELD_PRIORITY:
            if field in used_fields:
                continue
            if any(kw in cell_norm for kw in PAYMENT_ADVICE_HEADER_KEYWORDS[field]):
                col_map[idx] = field
                used_fields.add(field)
                break

    return col_map


def looks_like_payment_advice_header(row: List[str]) -> bool:
    """A Payment Advice table is identified by having BOTH a Bill No
    column and a Net Payment column together. This combination doesn't
    occur in a normal invoice table (which has no 'Net Payment' concept),
    so it's a reliable, vendor-agnostic signature - never based on vendor
    name or a hardcoded document."""
    if not row:
        return False
    fields_found = set(map_payment_advice_headers(row).values())
    return "invoice_number" in fields_found and "net_payment" in fields_found


def build_payment_advice_invoices_from_pages(
    pages: List[List[List[str]]],
    filename: str,
    shared_fields: Optional[Dict[str, Any]] = None,
    normalize_date_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, Any]]:
    """Turn a Payment Advice's per-page tables into one canonical
    extraction dict per bill (one Bill No = one record - see module/task
    context). `pages` is a list of pages, each page a flat list of table
    rows (already extracted, e.g. via pdfplumber's page.extract_tables(),
    concatenated if a page had more than one table).

    Handles:
    - The header row being repeated on every page (skipped after the
      first occurrence).
    - A Bill No wrapped across two lines within one cell (e.g.
      "WBBEL2510004\\n583") - joined into one token.
    - A Bill No split across a PAGE break, where the tail fragment lands
      as its own row on the next page with every other column blank
      (e.g. "...2510035016DIS" ends one page, "CO" starts the next) -
      merged into the previous row.
    - The trailing "Total ..." row, which is dropped (not a bill).

    Returns [] if no Payment Advice header is found, so the caller can
    fall back to normal invoice extraction.
    """
    shared_fields = shared_fields or {}

    header_row = None
    col_map: Dict[int, str] = {}
    combined_rows: List[List[Any]] = []

    for page_rows in pages:
        for row in page_rows or []:
            if not row:
                continue

            if header_row is None:
                if looks_like_payment_advice_header(row):
                    header_row = row
                    col_map = map_payment_advice_headers(row)
                continue

            # Header repeated at the top of a later page - not a data row.
            if looks_like_payment_advice_header(row):
                continue

            first_cell = (row[0] or "").strip()
            rest_has_content = any((c or "").strip() for c in row[1:])

            if not first_cell and not rest_has_content:
                continue

            if first_cell and not rest_has_content and combined_rows:
                # Continuation fragment of the previous row's Bill No,
                # split across a page break.
                combined_rows[-1][0] = f"{combined_rows[-1][0] or ''}\n{first_cell}"
                continue

            combined_rows.append(list(row))

    if header_row is None or not col_map:
        return []

    bill_idx = next((idx for idx, f in col_map.items() if f == "invoice_number"), None)
    if bill_idx is None:
        return []

    invoices: List[Dict[str, Any]] = []

    for row in combined_rows:
        raw_bill_no = (row[bill_idx] if bill_idx < len(row) else "") or ""
        bill_no = raw_bill_no.replace("\n", "").strip()

        # Drop the footer/summary row (e.g. "Total 13,295,807.02 ...") and
        # any other row with no real bill identifier - never a real bill.
        if not bill_no or bill_no.lower() in ("total", "grand total", "subtotal", "sub total"):
            continue
        if not any(c.isdigit() for c in bill_no):
            continue

        record: Dict[str, Any] = {
            "source_filename": filename,
            "processing_status": "PROCESSED",
        }
        record.update(shared_fields)
        record["invoice_number"] = bill_no

        for idx, field in col_map.items():
            if field == "invoice_number" or idx >= len(row):
                continue
            raw_value = (row[idx] or "").replace("\n", "").strip()
            if not raw_value:
                continue

            if field == "invoice_date":
                record["invoice_date"] = normalize_date_fn(raw_value) if normalize_date_fn else raw_value
            elif field == "invoice_amount":
                amount = _parse_amount(raw_value)
                if amount is not None:
                    record["invoice_amount"] = amount
            elif field == "net_payment":
                amount = _parse_amount(raw_value)
                if amount is not None:
                    record["_net_payment"] = amount

        invoices.append(record)

    # Total Payment for a Payment Advice = SUM of every bill's individual
    # Net Payment - the same value on every record produced from this one
    # document (never the individual amount).
    net_payments = [r["_net_payment"] for r in invoices if r.get("_net_payment") is not None]
    total_net_payment = sum(net_payments) if net_payments else None

    for r in invoices:
        net_payment = r.pop("_net_payment", None)
        if net_payment is not None:
            # Consumed by InvoiceValidator.calculate_payment_done() as the
            # actual amount paid against this specific bill - kept out of
            # the canonical field names until validation time so it can
            # never be confused with a normal invoice's calculated
            # Yes/No Payment Done.
            r["payment_done_amount"] = net_payment
        if total_net_payment is not None:
            r["total_payment"] = total_net_payment

    return invoices


def looks_like_invoice_header(row: List[str]) -> bool:
    """Heuristic: does this row look like the header of a table that lists
    one invoice per row (e.g. a remittance advice)? Requires at least one
    identifying column (invoice number/date) AND the invoice-level amount
    column - using STRONG labels only, so a generic per-line-item table
    (Description/Qty/Rate/Amount) never satisfies this."""
    if not row:
        return False
    col_map = map_table_headers(row)
    fields_found = set(col_map.values())
    has_id = "invoice_number" in fields_found or "invoice_date" in fields_found
    has_amount = "invoice_amount" in fields_found
    return has_id and has_amount


def _parse_amount(value: str) -> Optional[Decimal]:
    """Parse a table-cell amount string to a Decimal (money is never
    handled as float in this codebase - see app.utils.validation),
    supporting the negative-amount conventions common in Indian
    ledgers/remittances in addition to a plain leading minus:
        -15924.00       (leading minus)
        15,924.00-      (trailing minus)
        (15,924.00)     (parentheses)
    The minus sign is never dropped - a negative input always produces a
    negative output.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    negative = False
    if s.startswith('(') and s.endswith(')'):
        negative = True
        s = s[1:-1].strip()

    s = re.sub(r'(?i)^\s*(rs\.?|inr|usd|₹|\$)\s*', '', s).strip()

    if s.endswith('-'):
        negative = True
        s = s[:-1].strip()
    if s.startswith('-'):
        negative = True
        s = s[1:].strip()

    cleaned = re.sub(r'[^\d.]', '', s.replace(',', ''))
    if not cleaned or cleaned == '.':
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def build_invoices_from_table(
    rows: List[List[str]],
    header_row_index: int,
    filename: str,
    shared_fields: Optional[Dict[str, Any]] = None,
    normalize_date_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, Any]]:
    """Turn a table into a list of canonical invoice extraction dicts, one
    per data row, using the header row to determine which column holds
    which of the 8 required fields. Unmapped columns are ignored - only
    the canonical fields are ever produced.

    Rows with neither an invoice number nor an invoice date are skipped
    (this drops footer/total rows like "487.483" with no identifying info).
    """
    shared_fields = shared_fields or {}
    if header_row_index >= len(rows):
        return []

    header_row = rows[header_row_index]
    col_map = map_table_headers(header_row)
    if not col_map:
        return []

    invoices: List[Dict[str, Any]] = []

    invoices: List[Dict[str, Any]] = []

    for row in rows[header_row_index + 1:]:
        if not row or all(not (c or "").strip() for c in row):
            break

        record: Dict[str, Any] = {
            "source_filename": filename,
            "processing_status": "PROCESSED",
        }
        record.update(shared_fields)

        for idx, field in col_map.items():
            if idx >= len(row):
                continue
            raw_value = (row[idx] or "").strip()
            if not raw_value:
                continue

            if field in DATE_FIELDS:
                record[field] = normalize_date_fn(raw_value) if normalize_date_fn else raw_value
            elif field in NUMERIC_FIELDS:
                amount = _parse_amount(raw_value)
                if amount is not None:
                    record[field] = amount
            else:
                record[field] = raw_value

        # Reject an invoice_number that's actually a footer/summary label
        if record.get("invoice_number") and not _looks_like_invoice_number(str(record["invoice_number"])):
            record.pop("invoice_number", None)

        # Skip footer/summary rows that carry no identifying info at all
        if not record.get("invoice_number") and not record.get("invoice_date"):
            continue

        invoices.append(record)

    return invoices



_HEADERLESS_DATE_RE = re.compile(r'^\d{1,2}[-/](?:[A-Za-z]{3,9}|\d{1,2})[-/]\d{2,4}$')


def _looks_like_headerless_date(value: str) -> bool:
    return bool(_HEADERLESS_DATE_RE.match((value or "").strip()))


def _looks_like_headerless_code(value: str) -> bool:
    """A bill/reference/UTR-style code: letters and digits jammed together
    with no spaces or punctuation - e.g. 'UPRKT2510051648',
    'KKBKR52026021813360279'. Deliberately excludes pure-numeric and
    pure-alphabetic strings, and anything with spaces/punctuation (which
    would more likely be a sentence or a formatted amount)."""
    v = (value or "").strip()
    if not v or " " in v or len(v) < 6 or len(v) > 30:
        return False
    if not v.isalnum():
        return False
    return any(c.isalpha() for c in v) and any(c.isdigit() for c in v)


def _looks_like_headerless_amount(value: str) -> bool:
    """A plain monetary figure, Indian-comma-grouping tolerant, with an
    optional 1-2 digit decimal part - e.g. '51,332.46', '5,01,808.11'."""
    v = (value or "").strip()
    if not v:
        return False
    stripped = v.strip('()')
    if stripped.endswith('-'):
        stripped = stripped[:-1]
    if stripped.startswith('-'):
        stripped = stripped[1:]
    stripped = stripped.replace(',', '')
    if not stripped:
        return False
    return stripped.replace('.', '', 1).isdigit()


def looks_like_headerless_batch_table(rows: List[List[str]]) -> bool:
    """Does this table look like the header-less (date, bill/ref code,
    amount) shape described above? Requires at least 2 rows that are EACH
    exactly a clean (one date, one code, one amount) triple - nothing more
    in the row - before trusting a table with no header labels at all,
    since this is inherently riskier to auto-detect than an explicitly
    labeled header."""
    matches = 0
    for row in rows:
        non_empty = [(c or "").strip() for c in row if (c or "").strip()]
        if len(non_empty) != 3:
            continue
        dates = [c for c in non_empty if _looks_like_headerless_date(c)]
        codes = [c for c in non_empty if _looks_like_headerless_code(c)]
        amounts = [c for c in non_empty if _looks_like_headerless_amount(c)]
        if len(dates) == 1 and len(codes) == 1 and len(amounts) == 1:
            matches += 1
            if matches >= 2:
                return True
    return False


def build_invoices_from_headerless_batches(
    rows: List[List[str]],
    filename: str,
    shared_fields: Optional[Dict[str, Any]] = None,
    normalize_date_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, Any]]:
    """Turn a header-less (date, bill/ref code, amount) table into one
    canonical extraction dict per bill (see looks_like_headerless_batch_table
    for the shape this handles).

    A bill LINE is a row that is exactly (one date, one bill/ref code, one
    amount) and nothing else. A batch-CLOSING row has no bill code of its
    own but carries 2+ amount-looking cells (a raw subtotal AND the rounded
    final amount actually paid) plus a UTR-style code - it closes out every
    bill collected since the previous closing row (or the start of the
    table), assigning ITS OWN utr_no/total_payment to just that batch, since
    one table can contain several separately-remitted batches. Any trailing
    bills never closed by such a row are still returned, just without a
    UTR/total_payment - never guessed at.
    """
    shared_fields = shared_fields or {}
    invoices: List[Dict[str, Any]] = []
    pending_batch: List[Dict[str, Any]] = []

    for row in rows:
        non_empty = [(c or "").strip() for c in row if (c or "").strip()]
        if not non_empty:
            continue

        dates = [c for c in non_empty if _looks_like_headerless_date(c)]
        codes = [c for c in non_empty if _looks_like_headerless_code(c)]
        amounts = [c for c in non_empty if _looks_like_headerless_amount(c)]

        if len(non_empty) == 3 and len(dates) == 1 and len(codes) == 1 and len(amounts) == 1:
            record: Dict[str, Any] = {
                "source_filename": filename,
                "processing_status": "PROCESSED",
            }
            record.update(shared_fields)
            record["invoice_number"] = codes[0]
            record["invoice_date"] = normalize_date_fn(dates[0]) if normalize_date_fn else dates[0]
            amount = _parse_amount(amounts[0])
            if amount is not None:
                record["invoice_amount"] = amount
                
                record["payment_done_amount"] = amount
            pending_batch.append(record)
            continue

        if len(amounts) >= 2 and codes and pending_batch:
            utr_no = codes[-1]
            total_paid = _parse_amount(amounts[-1])
            for record in pending_batch:
                record["utr_no"] = utr_no
                if total_paid is not None:
                    record["total_payment"] = total_paid
            invoices.extend(pending_batch)
            pending_batch = []
            continue

        # Anything else (blank separator rows, unrecognized rows) is
        # ignored - never guessed at.

    invoices.extend(pending_batch)
    return invoices