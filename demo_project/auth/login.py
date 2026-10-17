"""Current login path: credentials become a short-lived JWT, not a session."""

import hashlib
import hmac

from auth.jwt_tokens import issue_token
from db.models import User


class LoginError(Exception):
    pass


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)


def login(email: str, password: str, find_user, secret: str) -> str:
    user: User | None = find_user(email)
    if user is None or not hmac.compare_digest(
        password_digest(password, user.salt), user.password_hash
    ):
        raise LoginError("Invalid credentials")
    return issue_token(user.id, ["invoices:write"], secret)
