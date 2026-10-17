"""An invoice touches auth, Postgres-backed models, and notifications."""

from auth.permissions import require_scope
from db.models import Customer, Invoice
from notifications.email import invoice_message


def issue_invoice(token: str, secret: str, customer: Customer,
                  amount_cents: int, send_email) -> Invoice:
    actor_id = require_scope(token, "invoices:write", secret)
    if amount_cents <= 0:
        raise ValueError("Invoice amount must be positive")
    invoice = Invoice(id=f"inv-{customer.id}-{amount_cents}",
                      customer_id=customer.id, amount_cents=amount_cents,
                      created_by=actor_id)
    send_email(customer.email, invoice_message(invoice))
    return invoice
