"""Minimal HS256 JWT for demo use; a meeting may propose replacing it."""

import base64
import hashlib
import hmac
import json
import time


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(user_id: str, scopes: list[str], secret: str, ttl_seconds: int = 900) -> str:
    header = _encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = _encode(json.dumps({"sub": user_id, "scopes": scopes,
                                  "exp": int(time.time()) + ttl_seconds}).encode())
    body = f"{header}.{payload}"
    signature = _encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_token(token: str, secret: str) -> dict:
    try:
        header, payload, signature = token.split(".")
        body = f"{header}.{payload}"
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            raise ValueError("Invalid signature")
        claims = json.loads(_decode(payload))
        if claims["exp"] <= time.time():
            raise ValueError("Expired token")
        return claims
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid token") from exc
