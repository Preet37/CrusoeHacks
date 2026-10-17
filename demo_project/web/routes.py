"""Thin handlers; a real HTTP framework would call these functions."""

from auth.login import LoginError, login
from billing.invoices import issue_invoice


def login_route(payload: dict, find_user, secret: str) -> dict:
    try:
        token = login(payload["email"], payload["password"], find_user, secret)
    except (KeyError, LoginError):
        return {"status": 401, "error": "Invalid credentials"}
    return {"status": 200, "token": token}


def invoice_route(token: str, secret: str, customer, amount_cents: int, send_email) -> dict:
    invoice = issue_invoice(token, secret, customer, amount_cents, send_email)
    return {"status": 201, "invoice_id": invoice.id}
