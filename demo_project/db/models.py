"""Records corresponding to users, customers and invoices in Postgres."""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    email: str
    salt: bytes
    password_hash: bytes


@dataclass(frozen=True)
class Customer:
    id: str
    email: str


@dataclass(frozen=True)
class Invoice:
    id: str
    customer_id: str
    amount_cents: int
    created_by: str
