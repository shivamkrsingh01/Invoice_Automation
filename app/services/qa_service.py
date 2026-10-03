

from google import genai
from google.genai import types
from app.config.settings import settings
from app.utils.logging_config import logger

MODEL_NAME = "gemini-3.1-flash-lite"
MAX_INVOICES_FOR_CONTEXT = 200

SYSTEM_INSTRUCTION = """You answer questions about a company's invoice data.

You are given a list of invoices below and a question. Answer using ONLY
the data provided - never guess or make up figures. If the data doesn't
contain enough information to answer, say so plainly instead of guessing.

Keep answers short and direct. Use plain text, not markdown tables. When
an amount is missing from the data, treat it as unknown, not zero."""


def _format_invoice_line(inv: dict) -> str:
    """One invoice, one line - compact enough that a few hundred of these
    still fit comfortably in the model's context window."""
    return (
        f"- Invoice {inv.get('invoice_number') or 'unknown'} | "
        f"Vendor: {inv.get('vendor_name') or 'unknown'} | "
        f"Date: {inv.get('invoice_date') or 'unknown'} | "
        f"Amount: {inv.get('invoice_amount') if inv.get('invoice_amount') is not None else 'unknown'} | "
        f"Total payment: {inv.get('total_payment') if inv.get('total_payment') is not None else 'unknown'} | "
        f"Total net payment: {inv.get('total_net_payment') if inv.get('total_net_payment') is not None else 'unknown'} | "
        f"Paid: {inv.get('payment_done') if inv.get('payment_done') is not None else 'unknown'} | "
        f"Source: {inv.get('source') or 'unknown'}"
    )


class QAService:
    def __init__(self):
        if not settings.gemini_api_key:
            raise ValueError(
                "Gemini is not configured - set GEMINI_API_KEY in .env "
                "(get a free key at https://aistudio.google.com/apikey)"
            )
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def answer_question(self, question: str, postgres_service) -> str:
        result = postgres_service.list_invoices(limit=MAX_INVOICES_FOR_CONTEXT, offset=0)
        invoices = result["rows"]

        if not invoices:
            return "There are no invoices stored yet, so I can't answer questions about them."

        invoice_lines = "\n".join(_format_invoice_line(inv) for inv in invoices)
        prompt = f"Invoices ({len(invoices)} total):\n{invoice_lines}\n\nQuestion: {question}"

        try:
            response = self.client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.1,  # factual lookups, not creative writing
                    max_output_tokens=500,
                ),
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini Q&A request failed: {str(e)}")
            raise