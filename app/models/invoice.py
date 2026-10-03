

from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_validator


class InvoiceRecord(BaseModel):
    """Canonical invoice record.

    This is the ONLY shape used throughout the pipeline (extraction ->
    validation -> storage). It intentionally contains just the 8 fields the
    business needs - no line items, no per-vendor extra fields, no other
    invoice metadata.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    utr_no: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    invoice_amount: Optional[Decimal] = None
    total_payment: Optional[Decimal] = None
    total_net_payment: Optional[Decimal] = None
    payment_done: Optional[Decimal] = None
    vendor_name: Optional[str] = None
    source: Optional[str] = None



    @field_validator("utr_no", "invoice_number", "vendor_name", "source")
    @classmethod
    def _strip_strings(cls, v):
        """Clean string fields by stripping whitespace and normalizing an
        empty result to None (missing values must be None, not "")."""
        if v is None:
            return v
        v = v.strip()
        return v or None

    def is_likely_invoice(self) -> bool:
        """Determine if this looks like invoice data at all, based on how
        many of the identifying fields were found. Requires at least 2 of:
        invoice number, invoice date, vendor name, invoice amount."""
        key_fields = [
            self.invoice_number,
            self.invoice_date,
            self.vendor_name,
            self.invoice_amount,
        ]
        present_fields = sum(1 for field in key_fields if field is not None)
        return present_fields >= 2

    def to_dict(self) -> dict:
        """Convert to a plain dict for storage. Missing values stay as
        Python None (never "N/A"/"Unknown"/"Not Found")."""
        return {
            "utr_no": self.utr_no,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "invoice_amount": self.invoice_amount,
            "total_payment": self.total_payment,
            "total_net_payment": self.total_net_payment,
            "payment_done": self.payment_done,
            "vendor_name": self.vendor_name,
            "source": self.source,
        }