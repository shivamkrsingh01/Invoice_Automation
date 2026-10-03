

import re
from typing import Optional, Any
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dateutil import parser
from app.utils.logging_config import logger


class InvoiceValidator:
    """Validation and normalization utilities for the canonical 8-field
    invoice record. Extraction and normalization are kept separate on
    purpose: extractors just find raw text/numbers, this class turns that
    into clean, typed values (Decimal for money, date for dates)."""

    @staticmethod
    def normalize_amount(amount: Any) -> Optional[Decimal]:
        """Normalize amount values to Decimal. Monetary values use Decimal
        (not float) throughout, per business requirement."""
        if amount is None:
            return None

        try:
            if isinstance(amount, Decimal):
                return amount

            if isinstance(amount, bool):
                return None

            if isinstance(amount, (int, float)):
                # Route through str() to avoid binary-float artifacts
                # (e.g. Decimal(0.1) != Decimal("0.1")).
                return Decimal(str(amount))

            if isinstance(amount, str):
                cleaned = amount.strip()
                for symbol in (',', '₹', '$', '€', '£', 'Rs.', 'Rs', 'INR'):
                    cleaned = cleaned.replace(symbol, '')
                cleaned = cleaned.strip()

                if not cleaned:
                    return None

                # Support negative-amount conventions seen in Indian
                # ledgers/remittances in addition to a plain leading
                # minus, without ever dropping the sign:
                #   15924.00-   (trailing minus)
                #   (15924.00)  (parentheses)
                negative = False
                if cleaned.startswith('(') and cleaned.endswith(')'):
                    negative = True
                    cleaned = cleaned[1:-1].strip()
                if cleaned.endswith('-'):
                    negative = True
                    cleaned = cleaned[:-1].strip()

                if not cleaned:
                    return None

                value = Decimal(cleaned)
                return -value if negative else value

            return None

        except (InvalidOperation, ValueError, TypeError) as e:
            logger.warning(f"Failed to normalize amount '{amount}': {str(e)}")
            return None

    @staticmethod
    def normalize_date(date_value: Any) -> Optional[date]:
        """Normalize date values to a Python date object."""
        if date_value is None:
            return None

        try:
            if isinstance(date_value, datetime):
                return date_value.date()

            if isinstance(date_value, date):
                return date_value

            if isinstance(date_value, str):
                date_value = date_value.strip()
                if not date_value:
                    return None


                iso_match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', date_value)
                if iso_match:
                    year, month, day = (int(g) for g in iso_match.groups())
                    return date(year, month, day)


                parsed_date = parser.parse(date_value, dayfirst=True)
                return parsed_date.date()

            return None

        except (ValueError, TypeError, OverflowError) as e:
            logger.warning(f"Failed to normalize date '{date_value}': {str(e)}")
            return None

    @staticmethod
    def clean_string(value: Any) -> Optional[str]:
        """Clean string values. Blank/whitespace-only strings become None
        so missing values are always represented as None, never ''."""
        if value is None:
            return None

        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None

        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def calculate_payment_done(
        invoice_amount: Optional[Decimal],
        total_payment: Optional[Decimal],
        payment_done_amount: Optional[Decimal] = None
    ) -> Optional[Decimal]:
        """Compute the Payment Done value - ALWAYS a real monetary Decimal
        (e.g. Decimal("-15924.00"), Decimal("13670590.51")), NEVER
        "Yes"/"No". `invoice_amount` is accepted for call-signature
        compatibility but is no longer used to derive a Yes/No verdict -
        this applies to every invoice, Payment Advice or not.

        Only `payment_done_amount` is used - the actual amount paid
        against this specific bill, when the document states one (e.g. a
        Payment Advice's per-bill Net Payment column).

        If the document gives no paid amount for this invoice, the result
        is None (stored as a real SQL NULL, displayed as the literal text
        "Null" - see PostgresStorageService.to_display_value).

        CHANGED: this used to fall back to `total_payment` when
        `payment_done_amount` was missing (copying a document-level or
        batch-level total onto every invoice). That fallback was removed
        on request - a batch total is not proof that a specific invoice
        was paid that amount, so payment_done is now left empty unless the
        document itself states a paid amount for the invoice.

        The result is quantized to exactly 2 decimal places (standard
        money rounding), the same precision the old formatted-string
        version used - only the type changed (Decimal, not str), so this
        can be stored/compared/summed like invoice_amount and
        total_payment instead of needing separate string handling.
        """
        if payment_done_amount is None:
            return None
        return payment_done_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def validate_invoice_data(extracted_data: dict) -> dict:
        """Validate and normalize raw extracted data into the 8 canonical
        fields only. Anything else in extracted_data (e.g. internal
        bookkeeping keys like source_filename/processing_status) is
        deliberately ignored here and never makes it into the stored
        record."""
        normalized = {
            "utr_no": InvoiceValidator.clean_string(extracted_data.get('utr_no')),
            "invoice_number": InvoiceValidator.clean_string(extracted_data.get('invoice_number')),
            "invoice_date": InvoiceValidator.normalize_date(extracted_data.get('invoice_date')),
            "invoice_amount": InvoiceValidator.normalize_amount(extracted_data.get('invoice_amount')),
            "total_payment": InvoiceValidator.normalize_amount(extracted_data.get('total_payment')),
            # Document/table-level NET payment (after TDS, debits, discounts).
            # Same value on every invoice that belongs to that table.
            "total_net_payment": InvoiceValidator.normalize_amount(extracted_data.get('total_net_payment')),
            "vendor_name": InvoiceValidator.clean_string(extracted_data.get('vendor_name')),
            # Assigned upstream by the pipeline (which channel/document type
            # this came from) - just cleaned here, never guessed.
            "source": InvoiceValidator.clean_string(extracted_data.get('source')),
        }


        payment_done_amount = InvoiceValidator.normalize_amount(
            extracted_data.get('payment_done_amount')
        )
        normalized["payment_done"] = InvoiceValidator.calculate_payment_done(
            normalized["invoice_amount"], normalized["total_payment"], payment_done_amount
        )

        return normalized

    @staticmethod
    def is_supported_file_type(filename: str, content_type: Optional[str] = None) -> bool:
        """Check if file type is supported for invoice processing."""
        if not filename:
            return False

        supported_extensions = {'.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.docx', '.xlsx', '.xls'}

        filename_lower = filename.lower()
        for ext in supported_extensions:
            if filename_lower.endswith(ext):
                return True

        if content_type:
            content_type_lower = content_type.lower()
            supported_content_types = [
                'application/pdf',
                'image/jpeg',
                'image/jpg',
                'image/png',
                'image/tiff',
                'image/bmp',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/msword',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'application/vnd.ms-excel'
            ]
            if content_type_lower in supported_content_types:
                return True

        return False