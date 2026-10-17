"""Email content passed to the app's delivery provider."""

from db.models import Invoice


def invoice_message(invoice: Invoice) -> str:
    dollars = invoice.amount_cents / 100
    return f"Invoice {invoice.id} for ${dollars:.2f} is ready."
