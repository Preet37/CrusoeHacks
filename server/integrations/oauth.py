"""Connect Slack / Jira / GitHub to a workspace: token verification and OAuth flows.

Token connect (no app setup): the user pastes a token on the dashboard, we verify it
against the provider, then store it per workspace (integrations/credentials.py).

OAuth connect (when the server has an OAuth app configured):
  Slack     SLACK_CLIENT_ID / SLACK_CLIENT_SECRET          (oauth/v2, bot token)
  Jira      ATLASSIAN_CLIENT_ID / ATLASSIAN_CLIENT_SECRET  (3LO, offline_access refresh token)
  GitHub    GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
redirect_uri = PUBLIC_SERVER_URL + /oauth/<provider>/callback. The `state` is an HMAC-signed
(OAUTH_STATE_SECRET, else a random per-process key), 15-minute token binding the workspace.

Errors raised here (ConnectError) are safe to show users: they never include a token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

import config  # noqa: F401 - loads server/.env
from integrations import credentials

log = logging.getLogger("twin.oauth")

TIMEOUT_S = 15.0
STATE_TTL_S = 15 * 60
# Tests swap in an httpx.MockTransport; production uses the default transport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_PROCESS_SECRET = secrets.token_bytes(32)

SLACK_SCOPES = "chat:write,chat:write.public,channels:read"
ATLASSIAN_SCOPES = "read:jira-work write:jira-work read:jira-user offline_access"
GITHUB_SCOPES = "repo read:user"

ENV_KEYS = {"slack": "SLACK", "jira": "ATLASSIAN", "github": "GITHUB"}
AUTHORIZE = {"slack": "https://slack.com/oauth/v2/authorize",
             "jira": "https://auth.atlassian.com/authorize",
             "github": "https://github.com/login/oauth/authorize"}
SLACK_API = "https://slack.com/api"
ATLASSIAN_TOKEN = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_API = "https://api.atlassian.com"
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


class ConnectError(Exception):
    """Connect failed; the message is safe for the dashboard."""


def _http(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, transport=_TRANSPORT, **kw)


async def _call(client: httpx.AsyncClient, method: str, url: str, what: str, **kw) -> httpx.Response:
    try:
        return await client.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise ConnectError(f"{what} unreachable ({type(e).__name__})") from None


def _json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return None


# ------------------------------------------------------------------ OAuth config + state

def client_creds(provider: str) -> tuple[str, str]:
    prefix = ENV_KEYS.get(provider, "")
    return (os.getenv(f"{prefix}_CLIENT_ID", "").strip(), os.getenv(f"{prefix}_CLIENT_SECRET", "").strip())


def oauth_available(provider: str) -> bool:
    return provider in ENV_KEYS and all(client_creds(provider))


def _secret() -> bytes:
    explicit = os.getenv("OAUTH_STATE_SECRET", "").strip()
    return explicit.encode() if explicit else _PROCESS_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_state(workspace: str, provider: str, extra: dict | None = None) -> str:
    body = {"w": workspace, "p": provider, "n": secrets.token_urlsafe(8),
            "exp": int(time.time()) + STATE_TTL_S, "x": extra or {}}
    raw = _b64(json.dumps(body, separators=(",", ":")).encode())
    mac = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def verify_state(state: str, provider: str) -> dict:
    """The state's payload, or ConnectError for a forged/expired/mismatched state."""
    raw, _, mac = (state or "").partition(".")
    expected = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    if not raw or not hmac.compare_digest(expected, mac):
        raise ConnectError("Invalid OAuth state")
    try:
        body = json.loads(_unb64(raw))
    except (ValueError, TypeError):
        raise ConnectError("Invalid OAuth state") from None
    if not isinstance(body, dict) or body.get("p") != provider or not body.get("w"):
        raise ConnectError("Invalid OAuth state")
    if int(body.get("exp") or 0) < time.time():
        raise ConnectError("OAuth state expired; click Connect again")
    return body


def authorize_url(provider: str, workspace: str, redirect_uri: str, extra: dict | None = None) -> str:
    client_id, _ = client_creds(provider)
    state = sign_state(workspace, provider, extra)
    if provider == "slack":
        q = {"client_id": client_id, "scope": SLACK_SCOPES, "redirect_uri": redirect_uri, "state": state}
    elif provider == "jira":
        q = {"audience": "api.atlassian.com", "client_id": client_id, "scope": ATLASSIAN_SCOPES,
             "redirect_uri": redirect_uri, "state": state, "response_type": "code", "prompt": "consent"}
    elif provider == "github":
        q = {"client_id": client_id, "scope": GITHUB_SCOPES, "redirect_uri": redirect_uri, "state": state}
    else:
        raise ConnectError(f"Unknown provider {provider}")
    return f"{AUTHORIZE[provider]}?{urlencode(q)}"


# ------------------------------------------------------------------ token connect

def _site(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise ConnectError("Jira site URL is required (https://your-team.atlassian.net)")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.atlassian\.net", host):
        raise ConnectError("Use your Jira Cloud site, like https://your-team.atlassian.net")
    return f"https://{host}"


async def _jira_project(client: httpx.AsyncClient, project: str | None) -> str:
    if project:
        resp = await _call(client, "GET", f"/rest/api/3/project/{project}", "Jira")
        if resp.status_code >= 400:
            raise ConnectError(f"Jira project {project} not found or not visible to this account")
        return project
    resp = await _call(client, "GET", "/rest/api/3/project/search", "Jira", params={"maxResults": 1})
    values = (_json(resp) or {}).get("values") if resp.status_code < 400 else None
    if not values:
        raise ConnectError("No Jira project visible to this account; enter a project key")
    return str(values[0].get("key"))


async def connect_jira_token(workspace: str, body: dict) -> dict:
    site = _site(body.get("site_url") or body.get("site") or "")
    email = str(body.get("email") or "").strip()
    token = str(body.get("api_token") or "").strip()
    project = str(body.get("project_key") or "").strip().upper()
    if not email or not token:
        raise ConnectError("Email and API token are required")
    async with _http(base_url=site, auth=(email, token), headers={"Accept": "application/json"}) as client:
        resp = await _call(client, "GET", "/rest/api/3/myself", "Jira")
        if resp.status_code in (401, 403):
            raise ConnectError("Jira rejected the email or API token")
        if resp.status_code >= 400:
            raise ConnectError(f"Jira HTTP {resp.status_code}")
        me = _json(resp) or {}
        project = await _jira_project(client, project)
    data = {"via": "token", "site": site, "email": email, "api_token": token, "project": project,
            "account": me.get("displayName") or email}
    credentials.save(workspace, "jira", data)
    return credentials.public_view(data)


async def _slack_channel(client: httpx.AsyncClient, token: str, channel: str, scopes: set[str]) -> dict:
    want = channel.strip().lstrip("#")
    cursor = None
    for _ in range(10):
        params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = await _call(client, "GET", f"{SLACK_API}/conversations.list", "Slack", params=params,
                           headers={"Authorization": f"Bearer {token}"})
        data = _json(resp) or {}
        if not data.get("ok"):
            raise ConnectError(f"Slack conversations.list: {str(data.get('error') or resp.status_code)[:80]}")
        for ch in data.get("channels") or []:
            if isinstance(ch, dict) and want in (ch.get("name"), ch.get("id")):
                if not ch.get("is_member") and (ch.get("is_private") or "chat:write.public" not in scopes):
                    raise ConnectError(f"Invite the bot to #{ch.get('name')} first (/invite @your-bot)")
                return ch
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise ConnectError(f"Slack channel #{want} not found (or the bot cannot see it)")


async def connect_slack_token(workspace: str, body: dict) -> dict:
    token = str(body.get("bot_token") or "").strip()
    channel = str(body.get("channel") or "").strip().lstrip("#") or "standup"
    if not token.startswith("xoxb-"):
        raise ConnectError("Use a Slack bot token (starts with xoxb-)")
    async with _http() as client:
        resp = await _call(client, "POST", f"{SLACK_API}/auth.test", "Slack",
                           headers={"Authorization": f"Bearer {token}"})
        auth = _json(resp) or {}
        if not auth.get("ok"):
            raise ConnectError(f"Slack rejected the token ({str(auth.get('error') or resp.status_code)[:60]})")
        scopes = {s.strip() for s in resp.headers.get("x-oauth-scopes", "").split(",") if s.strip()}
        ch = await _slack_channel(client, token, channel, scopes)
    data = {"via": "token", "bot_token": token, "channel": ch.get("name") or channel,
            "channel_id": ch.get("id"), "team": auth.get("team"), "account": auth.get("user") or auth.get("team")}
    credentials.save(workspace, "slack", data)
    return credentials.public_view(data)


async def _github_user(client: httpx.AsyncClient, token: str) -> dict:
    resp = await _call(client, "GET", f"{GITHUB_API}/user", "GitHub",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if resp.status_code in (401, 403):
        raise ConnectError("GitHub rejected the token")
    if resp.status_code >= 400:
        raise ConnectError(f"GitHub HTTP {resp.status_code}")
    return _json(resp) or {}


async def connect_github_token(workspace: str, body: dict) -> dict:
    token = str(body.get("token") or "").strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        raise ConnectError("Paste a GitHub personal access token")
    async with _http() as client:
        user = await _github_user(client, token)
    data = {"via": "token", "token": token, "account": user.get("login")}
    credentials.save(workspace, "github", data)
    return credentials.public_view(data)


TOKEN_CONNECT = {"jira": connect_jira_token, "slack": connect_slack_token, "github": connect_github_token}


async def connect_token(provider: str, workspace: str, body: dict) -> dict:
    fn = TOKEN_CONNECT.get(provider)
    if fn is None:
        raise ConnectError(f"Unknown provider {provider}")
    return await fn(workspace, body if isinstance(body, dict) else {})


# ------------------------------------------------------------------ OAuth callback

async def _atlassian_site(client: httpx.AsyncClient, access: str) -> dict:
    resp = await _call(client, "GET", f"{ATLASSIAN_API}/oauth/token/accessible-resources", "Atlassian",
                       headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    sites = _json(resp) if resp.status_code < 400 else None
    sites = [s for s in sites or [] if isinstance(s, dict) and s.get("id")]
    if not sites:
        raise ConnectError("No Jira site was granted to Twin")
    return sites[0]


async def finish_oauth(provider: str, code: str, state: str, redirect_uri: str) -> str:
    """Exchange the code, store tokens for the state's workspace; returns the workspace."""
    body = verify_state(state, provider)
    workspace, extra = str(body["w"]), body.get("x") or {}
    if not code:
        raise ConnectError("Provider returned no authorization code")
    client_id, client_secret = client_creds(provider)
    if not client_id or not client_secret:
        raise ConnectError(f"{provider} OAuth is not configured on the server")
    async with _http() as client:
        if provider == "slack":
            resp = await _call(client, "POST", f"{SLACK_API}/oauth.v2.access", "Slack", data={
                "client_id": client_id, "client_secret": client_secret, "code": code,
                "redirect_uri": redirect_uri})
            data = _json(resp) or {}
            if not data.get("ok") or not data.get("access_token"):
                raise ConnectError(f"Slack OAuth failed ({str(data.get('error') or resp.status_code)[:60]})")
            team = (data.get("team") or {}).get("name")
            stored = {"via": "oauth", "bot_token": data["access_token"],
                      "channel": str(extra.get("channel") or "standup").lstrip("#"),
                      "team": team, "account": team}
        elif provider == "jira":
            resp = await _call(client, "POST", ATLASSIAN_TOKEN, "Atlassian", json={
                "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
                "code": code, "redirect_uri": redirect_uri})
            tok = _json(resp) or {}
            if resp.status_code >= 400 or not tok.get("access_token"):
                raise ConnectError("Atlassian OAuth failed")
            access = tok["access_token"]
            site = await _atlassian_site(client, access)
            api = f"{ATLASSIAN_API}/ex/jira/{site['id']}"
            async with _http(base_url=api, headers={"Authorization": f"Bearer {access}",
                                                    "Accept": "application/json"}) as jc:
                me = _json(await _call(jc, "GET", "/rest/api/3/myself", "Jira")) or {}
                project = await _jira_project(jc, str(extra.get("project_key") or "").strip().upper() or None)
            stored = {"via": "oauth", "access_token": access, "refresh_token": tok.get("refresh_token"),
                      "expires_at": int(time.time()) + int(tok.get("expires_in") or 3600),
                      "cloudid": site["id"], "site": str(site.get("url") or "").rstrip("/"),
                      "project": project, "account": me.get("displayName") or site.get("name")}
        elif provider == "github":
            resp = await _call(client, "POST", GITHUB_TOKEN_URL, "GitHub", headers={"Accept": "application/json"},
                               data={"client_id": client_id, "client_secret": client_secret, "code": code,
                                     "redirect_uri": redirect_uri})
            tok = _json(resp) or {}
            if not tok.get("access_token"):
                raise ConnectError("GitHub OAuth failed")
            user = await _github_user(client, tok["access_token"])
            stored = {"via": "oauth", "token": tok["access_token"], "account": user.get("login")}
        else:
            raise ConnectError(f"Unknown provider {provider}")
    credentials.save(workspace, provider, stored)
    return workspace


async def refresh_atlassian(workspace: str, stored: dict) -> dict:
    """Rotate an expiring Atlassian access token (refresh tokens rotate too)."""
    client_id, client_secret = client_creds("jira")
    if not client_id or not client_secret or not stored.get("refresh_token"):
        raise ConnectError("Atlassian OAuth is not configured")
    async with _http() as client:
        resp = await _call(client, "POST", ATLASSIAN_TOKEN, "Atlassian", json={
            "grant_type": "refresh_token", "client_id": client_id, "client_secret": client_secret,
            "refresh_token": stored["refresh_token"]})
    tok = _json(resp) or {}
    if resp.status_code >= 400 or not tok.get("access_token"):
        raise ConnectError("Atlassian token refresh failed; reconnect Jira")
    updated = {**stored, "access_token": tok["access_token"],
               "refresh_token": tok.get("refresh_token") or stored["refresh_token"],
               "expires_at": int(time.time()) + int(tok.get("expires_in") or 3600)}
    updated.pop("saved_at", None)
    credentials.save(workspace, "jira", updated)
    return updated
