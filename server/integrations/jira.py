"""Jira Cloud REST v3 client for Twin's ticket pipeline (PC-23).

Env (server/.env): JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY.
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


def configured() -> bool:
    return all(_env())


def _client() -> httpx.AsyncClient:
    base, email, token, _ = _env()
    return httpx.AsyncClient(base_url=base, auth=(email, token), timeout=TIMEOUT_S,
                             transport=_TRANSPORT,
                             headers={"Accept": "application/json"})


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


async def status() -> dict:
    """Connection status for the dashboard; cached for 60 s per configuration."""
    base, email, _token, project = _env()
    out = {"connected": False, "site": base or None, "project": project or None,
           "account": None, "error": None}
    if not configured():
        out["error"] = "Jira not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)"
        return out
    key = (base, email, project)
    now = time.monotonic()
    if _status_cache["key"] == key and now - _status_cache["at"] < STATUS_TTL_S:
        return dict(_status_cache["value"])
    try:
        async with _client() as client:
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
    base, _email, _token, project = _env()
    title = " ".join((title or "").split())[:250] or "Untitled ticket from Twin"
    footer = f"Created by Twin from meeting {meeting or 'session'}"
    fields: dict = {"project": {"key": project}, "summary": title,
                    "issuetype": {"name": "Task"},
                    "description": (doc([*description.get("content", []), paragraph(_text(footer))])
                                    if isinstance(description, dict) else _adf(body, footer)),
                    "labels": [str(l).replace(" ", "-") for l in (labels or ["twin"])]}
    async with _client() as client:
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
