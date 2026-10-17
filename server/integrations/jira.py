"""Jira Cloud REST v3 client for Twin's ticket pipeline (PC-23).

Credentials: the workspace's stored connection (integrations/credentials.py: API token
or Atlassian OAuth 3LO via api.atlassian.com/ex/jira/<cloudid>) first, else env
(server/.env): JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY.
The API token is only ever passed to httpx basic auth; it never appears in
logs, errors or return values.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

import config  # noqa: F401 - loads server/.env

log = logging.getLogger("twin.jira")

TIMEOUT_S = 15.0
ATLASSIAN_API = "https://api.atlassian.com"
STATUS_TTL_S = 60.0
# Tests swap in an httpx.MockTransport; production uses the default transport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_status_cache: dict = {"key": None, "at": 0.0, "value": None}


class JiraError(Exception):
    """A Jira call failed. The message is safe to show users (no credentials)."""


def _env() -> tuple[str, str, str, str]:
    base = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
    return (base, os.getenv("JIRA_EMAIL", "").strip(),
            os.getenv("JIRA_API_TOKEN", "").strip(), os.getenv("JIRA_PROJECT_KEY", "").strip())


def _creds(workspace: str | None = None) -> dict:
    """Credentials for the current workspace: stored (token or OAuth) first, then env.

    Keys: via ("token"|"oauth"|"env"), api (REST base), site (browse base), project,
    auth (email, token) for basic or None, bearer (OAuth access token) or None.
    """
    from integrations import credentials
    stored = credentials.current("jira", workspace)
    if stored and stored.get("via") == "oauth" and stored.get("cloudid") and stored.get("access_token"):
        return {"via": "oauth", "api": f"{ATLASSIAN_API}/ex/jira/{stored['cloudid']}",
                "site": str(stored.get("site") or "").rstrip("/"), "project": str(stored.get("project") or ""),
                "auth": None, "bearer": stored["access_token"], "email": stored.get("account") or "",
                "stored": stored, "workspace": workspace or credentials.current_workspace()}
    if stored and stored.get("site") and stored.get("email") and stored.get("api_token"):
        site = str(stored["site"]).rstrip("/")
        return {"via": "token", "api": site, "site": site, "project": str(stored.get("project") or ""),
                "auth": (stored["email"], stored["api_token"]), "bearer": None, "email": stored["email"]}
    base, email, token, project = _env()
    return {"via": "env", "api": base, "site": base, "project": project,
            "auth": (email, token) if email and token else None, "bearer": None, "email": email}


def configured(workspace: str | None = None) -> bool:
    c = _creds(workspace)
    return bool(c["api"] and c["project"] and (c["auth"] or c["bearer"]))


async def _fresh(workspace: str | None = None) -> dict:
    """_creds(), refreshing an expiring Atlassian OAuth access token first."""
    c = _creds(workspace)
    stored = c.get("stored")
    if c["via"] == "oauth" and stored and float(stored.get("expires_at") or 0) < time.time() + 60 \
            and stored.get("refresh_token") and c.get("workspace"):
        from integrations import oauth
        try:
            await oauth.refresh_atlassian(c["workspace"], stored)
            c = _creds(workspace)
        except Exception as e:  # noqa: BLE001 - try the old token; Jira will answer 401 if it is dead
            log.info("Atlassian token refresh failed: %s", type(e).__name__)
    return c


def _client(c: dict | None = None) -> httpx.AsyncClient:
    c = c or _creds()
    headers = {"Accept": "application/json"}
    if c.get("bearer"):
        headers["Authorization"] = f"Bearer {c['bearer']}"
    return httpx.AsyncClient(base_url=c["api"], auth=c.get("auth"), timeout=TIMEOUT_S,
                             transport=_TRANSPORT, headers=headers)


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    parts: list[str] = []
    if isinstance(data, dict):
        parts += [str(m) for m in data.get("errorMessages") or [] if m]
        errors = data.get("errors")
        if isinstance(errors, dict):
            parts += [f"{k}: {v}" for k, v in errors.items()]
    detail = "; ".join(parts)[:300] or resp.reason_phrase or "request failed"
    return f"Jira HTTP {resp.status_code}: {detail}"


async def _request(client: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response:
    try:
        resp = await client.request(method, path, **kw)
    except httpx.HTTPError as e:
        raise JiraError(f"Jira unreachable ({type(e).__name__})") from None
    if resp.status_code >= 400:
        raise JiraError(_error_text(resp))
    return resp


async def status(workspace: str | None = None) -> dict:
    """Connection status for the dashboard; cached for 60 s per configuration."""
    c = await _fresh(workspace)
    base, project = c["site"], c["project"]
    out = {"connected": False, "site": base or None, "project": project or None,
           "account": None, "error": None, "via": c["via"]}
    if not configured(workspace):
        out["error"] = "Jira not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)"
        return out
    key = (c["via"], c["api"], c["email"], project)
    now = time.monotonic()
    if _status_cache["key"] == key and now - _status_cache["at"] < STATUS_TTL_S:
        return dict(_status_cache["value"])
    try:
        async with _client(c) as client:
            me = (await _request(client, "GET", "/rest/api/3/myself")).json()
            proj = (await _request(client, "GET", f"/rest/api/3/project/{project}")).json()
        out["account"] = me.get("displayName") if isinstance(me, dict) else None
        if isinstance(proj, dict) and proj.get("name"):
            out["project_name"] = proj["name"]
        out["connected"] = True
    except JiraError as e:
        out["error"] = str(e)
    except ValueError:
        out["error"] = "Jira returned invalid JSON"
    _status_cache.update(key=key, at=now, value=dict(out))
    return out


def _text(text: str, link: str | None = None) -> dict:
    node: dict = {"type": "text", "text": str(text)[:2000] or " "}
    if link:
        node["marks"] = [{"type": "link", "attrs": {"href": link}}]
    return node


def paragraph(*parts: dict) -> dict:
    return {"type": "paragraph", "content": [p for p in parts if p]}


def heading(text: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_text(text)]}


def bullet_list(items: list[list[dict]]) -> dict:
    return {"type": "bulletList", "content": [
        {"type": "listItem", "content": [paragraph(*parts)]} for parts in items if parts]}


def code_block(code: str, language: str | None = None) -> dict:
    node: dict = {"type": "codeBlock", "content": [_text(str(code)[:30000])]}
    if language:
        node["attrs"] = {"language": language}
    return node


def doc(content: list[dict]) -> dict:
    return {"type": "doc", "version": 1, "content": content}


def _adf(body: str, footer: str) -> dict:
    paragraphs = [line for line in (body or "").splitlines() if line.strip()] + [footer]
    return doc([paragraph(_text(text)) for text in paragraphs])


async def _find_account(client: httpx.AsyncClient, owner: str) -> str | None:
    try:
        resp = await _request(client, "GET", "/rest/api/3/user/search",
                              params={"query": owner, "maxResults": 5})
        users = resp.json()
    except (JiraError, ValueError) as e:
        log.info("Jira assignee lookup failed: %s", e)
        return None
    for user in users if isinstance(users, list) else []:
        if isinstance(user, dict) and user.get("accountId") and user.get("active", True) \
                and user.get("accountType", "atlassian") == "atlassian":
            return user["accountId"]
    return None


async def create_issue(title: str, body: str, owner: str | None = None,
                       labels: list[str] | None = None, meeting: str | None = None,
                       description: dict | None = None,
                       fallback_assignee: str | None = None) -> dict:
    """Create a Task in JIRA_PROJECT_KEY; returns {"key", "url"}. Raises JiraError.

    `description` is a prebuilt ADF doc (the footer is appended); without it the
    plain `body` lines become paragraphs. `fallback_assignee` (the speaker of the
    decision line) is searched only when `owner` finds no Jira user.
    """
    if not configured():
        raise JiraError("connect Jira")
    c = await _fresh()
    base, project = c["site"], c["project"]
    title = " ".join((title or "").split())[:250] or "Untitled ticket from Twin"
    footer = f"Created by Twin from meeting {meeting or 'session'}"
    fields: dict = {"project": {"key": project}, "summary": title,
                    "issuetype": {"name": "Task"},
                    "description": (doc([*description.get("content", []), paragraph(_text(footer))])
                                    if isinstance(description, dict) else _adf(body, footer)),
                    "labels": [str(l).replace(" ", "-") for l in (labels or ["twin"])]}
    async with _client(c) as client:
        account = None
        for name in (owner, fallback_assignee):
            if account is None and isinstance(name, str) and name.strip():
                account = await _find_account(client, name.strip())
        if account:
            fields["assignee"] = {"id": account}
        resp = await _request(client, "POST", "/rest/api/3/issue", json={"fields": fields})
    try:
        key = resp.json()["key"]
    except (ValueError, KeyError, TypeError):
        raise JiraError("Jira created no issue key") from None
    return {"key": key, "url": f"{base}/browse/{key}"}


async def attach_files(key: str, files: list[tuple[str, bytes, str]]) -> list[str]:
    """Attach [(filename, data, mime)] to an issue; returns the attached filenames. Raises JiraError."""
    if not configured() or not files:
        return []
    async with _client(await _fresh()) as client:
        resp = await _request(client, "POST", f"/rest/api/3/issue/{key}/attachments",
                              headers={"X-Atlassian-Token": "no-check"},
                              files=[("file", (name, data, mime)) for name, data, mime in files])
    try:
        return [str(a.get("filename")) for a in resp.json() if isinstance(a, dict)]
    except (ValueError, TypeError):
        return [name for name, _, _ in files]


def _person(value) -> str | None:
    return str(value.get("displayName") or value.get("emailAddress") or "") or None if isinstance(value, dict) else None


async def get_issue(key: str) -> dict:
    """Issue fields Twin needs: summary, description (ADF), people, status, attachments. Raises JiraError."""
    c = await _fresh()
    base = c["site"]
    async with _client(c) as client:
        resp = await _request(client, "GET", f"/rest/api/3/issue/{key}",
                              params={"fields": "summary,description,assignee,reporter,status,attachment,labels"})
    try:
        fields = resp.json().get("fields") or {}
    except (ValueError, AttributeError):
        raise JiraError("Jira returned invalid JSON") from None
    attachments = [{"id": a.get("id"), "filename": a.get("filename"), "mime": a.get("mimeType"),
                    "size": a.get("size"), "content": a.get("content")}
                   for a in fields.get("attachment") or [] if isinstance(a, dict)]
    return {"key": key, "url": f"{base}/browse/{key}", "summary": fields.get("summary"),
            "description": fields.get("description"), "assignee": _person(fields.get("assignee")),
            "reporter": _person(fields.get("reporter")),
            "status": ((fields.get("status") or {}).get("name") if isinstance(fields.get("status"), dict) else None),
            "labels": fields.get("labels") or [], "attachments": attachments}


async def download(url: str, max_bytes: int = 2_000_000) -> bytes:
    """Download one attachment's content (Jira redirects to its media store)."""
    c = await _fresh()
    if c["via"] == "oauth" and c["site"] and url.startswith(c["site"] + "/"):
        url = c["api"] + url[len(c["site"]):]  # OAuth tokens only work on api.atlassian.com
    async with _client(c) as client:
        resp = await _request(client, "GET", url, follow_redirects=True)
    data = resp.content
    if len(data) > max_bytes:
        raise JiraError("attachment too large")
    return data


async def add_comment(key: str, content: list[dict]) -> None:
    async with _client(await _fresh()) as client:
        await _request(client, "POST", f"/rest/api/3/issue/{key}/comment", json={"body": doc(content)})


async def transition(key: str, prefer: tuple[str, ...] = ("review", "progress")) -> str | None:
    """Move the issue to the first available status whose name contains one of `prefer`. Returns the new status."""
    async with _client(await _fresh()) as client:
        resp = await _request(client, "GET", f"/rest/api/3/issue/{key}/transitions")
        try:
            options = resp.json().get("transitions") or []
        except (ValueError, AttributeError):
            return None
        for word in prefer:
            for t in options:
                name = str((t.get("to") or {}).get("name") or t.get("name") or "")
                if word in name.casefold():
                    await _request(client, "POST", f"/rest/api/3/issue/{key}/transitions",
                                   json={"transition": {"id": t.get("id")}})
                    return name
    return None


def adf_text(node) -> str:
    """Plain text (code blocks fenced) from an ADF document."""
    if isinstance(node, list):
        return "".join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    inner = adf_text(node.get("content") or [])
    if kind == "codeBlock":
        lang = (node.get("attrs") or {}).get("language") or ""
        return f"\n```{lang}\n{inner}\n```\n"
    if kind == "heading":
        return f"\n## {inner}\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in ("paragraph", "bulletList", "orderedList"):
        return inner + "\n"
    return inner
