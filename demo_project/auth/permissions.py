"""Business scopes checked by billing routes."""

from auth.jwt_tokens import verify_token


def require_scope(token: str, scope: str, secret: str) -> str:
    claims = verify_token(token, secret)
    if scope not in claims.get("scopes", []):
        raise PermissionError(f"Missing scope: {scope}")
    return claims["sub"]
