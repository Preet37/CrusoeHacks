"""Postgres connection factory; only invoked in a deployed app."""

import os


def database_url() -> str:
    url = os.environ.get("LEDGERLANE_DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("Set LEDGERLANE_DATABASE_URL to a Postgres URL")
    return url


def connect():
    import psycopg  # Optional runtime dependency; offline demo does not connect.

    return psycopg.connect(database_url())
