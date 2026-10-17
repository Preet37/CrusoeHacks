"""Per-workspace Connect (token + OAuth) for Jira / Slack / GitHub. httpx is mocked; no network."""
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

import app
from brain import repo_connector
from integrations import credentials, jira, oauth, slack

JIRA_TOKEN = "jira-secret-api-token"
SLACK_TOKEN = "xoxb-slack-secret-token"
GH_TOKEN = "ghp_githubsecrettoken123"
PARAMS = {"workspace": "acme", "code": "invite"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TWIN_DB_PATH", str(tmp_path / "company.sqlite3"))
    monkeypatch.setenv("WORKSPACES", "acme:invite,beta:other")
    monkeypatch.setenv("PUBLIC_WEB_URL", "https://twin.example")
    monkeypatch.setenv("PUBLIC_SERVER_URL", "https://api.twin.example")
    monkeypatch.setenv("OAUTH_STATE_SECRET", "state-secret")
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY", "SLACK_WEBHOOK_URL",
              "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "GITHUB_TOKEN", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET",
              "ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(jira, "_status_cache", {"key": None, "at": 0.0, "value": None})
    monkeypatch.setattr(slack, "_POSTED", set())
    calls: list[httpx.Request] = []
    state = {"slack_member": True, "slack_scopes": "chat:write,channels:read"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host, path = request.url.host, request.url.path
        auth = request.headers.get("authorization", "")
        if host == "acme.atlassian.net" or host == "env.atlassian.net":
            expected = "Basic " + base64.b64encode(f"me@acme.com:{JIRA_TOKEN}".encode()).decode()
            if host == "acme.atlassian.net" and auth != expected:
                return httpx.Response(401, json={"errorMessages": ["bad creds"]})
            if path == "/rest/api/3/myself":
                return httpx.Response(200, json={"displayName": "Acme Me"})
            if path.startswith("/rest/api/3/project/"):
                return httpx.Response(200, json={"key": "ACME", "name": "Acme"})
            if path == "/rest/api/3/issue":
                return httpx.Response(201, json={"key": "ACME-1"})
            return httpx.Response(200, json=[])
        if host == "api.atlassian.com":
            if path == "/oauth/token/accessible-resources":
                return httpx.Response(200, json=[{"id": "cloud-1", "url": "https://acme.atlassian.net",
                                                  "name": "acme"}])
            if auth != "Bearer at-access":
                return httpx.Response(401, json={})
            if path.endswith("/rest/api/3/myself"):
                return httpx.Response(200, json={"displayName": "OAuth Me"})
            if path.endswith("/rest/api/3/project/search"):
                return httpx.Response(200, json={"values": [{"key": "OAP"}]})
            if path.endswith("/rest/api/3/issue"):
                return httpx.Response(201, json={"key": "OAP-9"})
            return httpx.Response(200, json=[])
        if host == "auth.atlassian.com" and path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "at-access", "refresh_token": "at-refresh",
                                             "expires_in": 3600})
        if host == "slack.com":
            if path == "/api/auth.test":
                ok = auth == f"Bearer {SLACK_TOKEN}"
                return httpx.Response(200, json={"ok": ok, "team": "Acme", "user": "twinbot",
                                                 **({} if ok else {"error": "invalid_auth"})},
                                      headers={"x-oauth-scopes": state["slack_scopes"]})
            if path == "/api/conversations.list":
                return httpx.Response(200, json={"ok": True, "channels": [
                    {"id": "C1", "name": "general", "is_member": True},
                    {"id": "C2", "name": "standup", "is_member": state["slack_member"]}]})
            if path == "/api/chat.postMessage":
                return httpx.Response(200, json={"ok": True})
            if path == "/api/oauth.v2.access":
                return httpx.Response(200, json={"ok": True, "access_token": "xoxb-oauth", "team": {"name": "Acme"}})
        if host == "api.github.com" and path == "/user":
            if auth in (f"Bearer {GH_TOKEN}", "Bearer gho_oauth"):
                return httpx.Response(200, json={"login": "acme-dev"})
            return httpx.Response(401, json={})
        if host == "github.com" and path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_oauth"})
        if host == "hooks.slack.test":
            return httpx.Response(200, text="ok")
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    for mod in (oauth, jira, slack):
        monkeypatch.setattr(mod, "_TRANSPORT", transport)
    return {"calls": calls, "state": state, "client": TestClient(app.app)}


def _connect(client, provider, body, params=PARAMS):
    return client.post(f"/integrations/{provider}/connect", params=params, json=body)


JIRA_BODY = {"site_url": "acme.atlassian.net", "email": "me@acme.com", "api_token": JIRA_TOKEN,
             "project_key": "acme"}


def test_jira_token_connect_verifies_stores_and_hides_secret(env):
    client = env["client"]
    resp = _connect(client, "jira", JIRA_BODY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["jira"] == {"connected": True, "via": "token", "account": "Acme Me",
                                   "site": "https://acme.atlassian.net", "project": "ACME"}
    assert any(c.url.path == "/rest/api/3/myself" for c in env["calls"])
    stored = credentials.get("acme", "jira")
    assert stored["api_token"] == JIRA_TOKEN and stored["project"] == "ACME"
    integ = client.get("/integrations", params=PARAMS)
    text = integ.text
    assert JIRA_TOKEN not in text and JIRA_TOKEN not in resp.text
    j = integ.json()["jira"]
    assert j["connected"] and j["via"] == "token" and j["stored"] and j["account"] == "Acme Me"
    assert j["oauth_available"] is False
    # Another workspace does not see acme's connection.
    other = client.get("/integrations", params={"workspace": "beta", "code": "other"}).json()["jira"]
    assert other["connected"] is False and other["stored"] is False


def test_jira_bad_token_rejected_and_not_stored(env):
    resp = _connect(env["client"], "jira", {**JIRA_BODY, "api_token": "wrong"})
    assert resp.status_code == 400 and "rejected" in resp.json()["error"]
    assert "wrong" not in resp.text
    assert credentials.get("acme", "jira") is None


def test_jira_site_must_be_atlassian(env):
    resp = _connect(env["client"], "jira", {**JIRA_BODY, "site_url": "https://evil.example"})
    assert resp.status_code == 400 and credentials.get("acme", "jira") is None


def test_connect_requires_invite_code(env):
    assert _connect(env["client"], "jira", JIRA_BODY, {"workspace": "acme", "code": "bad"}).status_code == 403
    assert _connect(env["client"], "jira", JIRA_BODY, {}).status_code == 403
    assert _connect(env["client"], "nope", JIRA_BODY).status_code == 404


def test_slack_token_connect_checks_auth_and_membership(env):
    client = env["client"]
    env["state"]["slack_member"] = False
    resp = _connect(client, "slack", {"bot_token": SLACK_TOKEN, "channel": "#standup"})
    assert resp.status_code == 400 and "Invite the bot" in resp.json()["error"]
    env["state"]["slack_scopes"] = "chat:write,chat:write.public,channels:read"
    resp = _connect(client, "slack", {"bot_token": SLACK_TOKEN, "channel": "#standup"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["slack"]["channel"] == "standup"
    bad = _connect(client, "slack", {"bot_token": "xoxb-wrong", "channel": "standup"})
    assert bad.status_code == 400
    integ = client.get("/integrations", params=PARAMS)
    assert SLACK_TOKEN not in integ.text
    s = integ.json()["slack"]
    assert s["connected"] and s["via"] == "token" and s["channel"] == "standup" and s["stored"]


def test_github_token_connect_and_private_clone_url(env):
    resp = _connect(env["client"], "github", {"token": GH_TOKEN})
    assert resp.status_code == 200 and resp.json()["github"]["account"] == "acme-dev"
    assert GH_TOKEN not in env["client"].get("/integrations", params=PARAMS).text
    url = "https://github.com/acme/private.git"
    assert repo_connector._auth_url(url) == url  # no workspace in scope: public clone
    with credentials.use_workspace("acme"):
        assert repo_connector._auth_url(url) == f"https://x-access-token:{GH_TOKEN}@github.com/acme/private.git"
    with credentials.use_workspace("beta"):
        assert repo_connector._auth_url(url) == url


def test_workspace_jira_overrides_env(env, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "env@x.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "env-token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    credentials.save("acme", "jira", {"via": "token", "site": "https://acme.atlassian.net",
                                      "email": "me@acme.com", "api_token": JIRA_TOKEN, "project": "ACME"})
    with credentials.use_workspace("acme"):
        issue = asyncio.run(jira.create_issue("T", "b"))
    assert issue == {"key": "ACME-1", "url": "https://acme.atlassian.net/browse/ACME-1"}
    post = [c for c in env["calls"] if c.method == "POST" and c.url.path == "/rest/api/3/issue"][-1]
    assert post.url.host == "acme.atlassian.net"
    assert json.loads(post.content)["fields"]["project"] == {"key": "ACME"}
    # No workspace (or one without stored creds): env fallback, the demo keeps working.
    with credentials.use_workspace("beta"):
        issue = asyncio.run(jira.create_issue("T", "b"))
    assert issue["url"].startswith("https://env.atlassian.net/")


def test_workspace_jira_oauth_uses_api_atlassian_bearer(env):
    credentials.save("acme", "jira", {"via": "oauth", "access_token": "at-access", "refresh_token": "r",
                                      "expires_at": 9_999_999_999, "cloudid": "cloud-1",
                                      "site": "https://acme.atlassian.net", "project": "OAP"})
    with credentials.use_workspace("acme"):
        assert jira.configured()
        issue = asyncio.run(jira.create_issue("T", "b"))
    assert issue == {"key": "OAP-9", "url": "https://acme.atlassian.net/browse/OAP-9"}
    post = [c for c in env["calls"] if c.method == "POST" and c.url.path.endswith("/issue")][-1]
    assert str(post.url) == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue"
    assert post.headers["authorization"] == "Bearer at-access"


def test_workspace_jira_oauth_refreshes_expired_token(env, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "cid")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "csecret")
    credentials.save("acme", "jira", {"via": "oauth", "access_token": "old", "refresh_token": "r1",
                                      "expires_at": 1, "cloudid": "cloud-1",
                                      "site": "https://acme.atlassian.net", "project": "OAP"})
    with credentials.use_workspace("acme"):
        asyncio.run(jira.create_issue("T", "b"))
    stored = credentials.get("acme", "jira")
    assert stored["access_token"] == "at-access" and stored["refresh_token"] == "at-refresh"


def test_workspace_slack_overrides_env_webhook(env, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/env")
    credentials.save("acme", "slack", {"via": "token", "bot_token": SLACK_TOKEN, "channel": "standup",
                                       "channel_id": "C2"})
    meeting = {"id": "m-1", "workspace": None, "room": "acme-standup", "title": "Standup",
               "summary": "We met.", "tickets": []}
    out = asyncio.run(slack.post_meeting_recap({**meeting, "workspace": "acme"}, wait=False))
    assert out == {"ok": True}
    post = env["calls"][-1]
    assert post.url.path == "/api/chat.postMessage"
    assert post.headers["authorization"] == f"Bearer {SLACK_TOKEN}"
    assert json.loads(post.content)["channel"] == "C2"
    # A meeting without a connected workspace still uses the env webhook.
    out = asyncio.run(slack.post_meeting_recap({**meeting, "id": "m-2"}, wait=False))
    assert out == {"ok": True} and env["calls"][-1].url.host == "hooks.slack.test"


def test_disconnect_removes_stored(env):
    client = env["client"]
    _connect(client, "github", {"token": GH_TOKEN})
    assert client.get("/integrations", params=PARAMS).json()["github"]["stored"]
    resp = client.post("/integrations/github/disconnect", params=PARAMS)
    assert resp.json() == {"ok": True, "removed": True}
    g = client.get("/integrations", params=PARAMS).json()["github"]
    assert g["stored"] is False and g["connected"] is False
    assert client.post("/integrations/github/disconnect", params={"workspace": "acme", "code": "x"}).status_code == 403


def _location(resp) -> tuple[str, dict]:
    loc = resp.headers["location"]
    parts = urlsplit(loc)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_oauth_start_builds_authorize_urls(env, monkeypatch):
    client = env["client"]
    assert client.get("/oauth/slack/start", params=PARAMS, follow_redirects=False).status_code == 404
    for p in ("SLACK", "ATLASSIAN", "GITHUB"):
        monkeypatch.setenv(f"{p}_CLIENT_ID", f"{p.lower()}-id")
        monkeypatch.setenv(f"{p}_CLIENT_SECRET", f"{p.lower()}-secret")
    assert client.get("/integrations", params=PARAMS).json()["slack"]["oauth_available"] is True

    resp = client.get("/oauth/slack/start", params={**PARAMS, "channel": "standup"}, follow_redirects=False)
    assert resp.status_code == 302
    base, q = _location(resp)
    assert base == "https://slack.com/oauth/v2/authorize"
    assert q["client_id"] == "slack-id" and q["scope"] == "chat:write,chat:write.public,channels:read"
    assert q["redirect_uri"] == "https://api.twin.example/oauth/slack/callback"
    body = oauth.verify_state(q["state"], "slack")
    assert body["w"] == "acme" and body["x"] == {"channel": "standup"}
    assert "slack-secret" not in resp.headers["location"]

    base, q = _location(client.get("/oauth/jira/start", params=PARAMS, follow_redirects=False))
    assert base == "https://auth.atlassian.com/authorize"
    assert q["audience"] == "api.atlassian.com" and q["response_type"] == "code"
    assert q["scope"] == "read:jira-work write:jira-work read:jira-user offline_access"
    assert q["redirect_uri"] == "https://api.twin.example/oauth/jira/callback"

    base, q = _location(client.get("/oauth/github/start", params=PARAMS, follow_redirects=False))
    assert base == "https://github.com/login/oauth/authorize" and q["scope"] == "repo read:user"

    bad = client.get("/oauth/slack/start", params={"workspace": "acme", "code": "x"}, follow_redirects=False)
    assert bad.status_code == 403


def test_oauth_callback_rejects_bad_state(env, monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    client = env["client"]
    assert client.get("/oauth/slack/callback", params={"code": "c", "state": "junk"},
                      follow_redirects=False).status_code == 400
    good = oauth.sign_state("acme", "slack")
    raw, _, mac = good.partition(".")
    forged = base64.urlsafe_b64encode(json.dumps({"w": "beta", "p": "slack", "exp": 9_999_999_999}).encode()
                                      ).decode().rstrip("=") + "." + mac
    assert client.get("/oauth/slack/callback", params={"code": "c", "state": forged},
                      follow_redirects=False).status_code == 400
    # A state for another provider is not accepted either.
    assert client.get("/oauth/github/callback", params={"code": "c", "state": good},
                      follow_redirects=False).status_code == 400
    assert credentials.get("acme", "slack") is None and credentials.get("beta", "slack") is None


def test_oauth_callback_jira_stores_tokens_and_redirects(env, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "cid")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "csecret")
    client = env["client"]
    resp = client.get("/oauth/jira/callback", params={"code": "auth-code", "state": oauth.sign_state("acme", "jira")},
                      follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://twin.example/dashboard?connected=jira"
    stored = credentials.get("acme", "jira")
    assert stored["cloudid"] == "cloud-1" and stored["refresh_token"] == "at-refresh"
    assert stored["project"] == "OAP" and stored["via"] == "oauth"
    exchange = next(c for c in env["calls"] if c.url.host == "auth.atlassian.com")
    sent = json.loads(exchange.content)
    assert sent["code"] == "auth-code" and sent["redirect_uri"] == "https://api.twin.example/oauth/jira/callback"
    integ = client.get("/integrations", params=PARAMS)
    assert "at-access" not in integ.text and "at-refresh" not in integ.text
    assert integ.json()["jira"]["via"] == "oauth" and integ.json()["jira"]["connected"]


def test_oauth_callback_slack_and_github(env, monkeypatch):
    for p in ("SLACK", "GITHUB"):
        monkeypatch.setenv(f"{p}_CLIENT_ID", "id")
        monkeypatch.setenv(f"{p}_CLIENT_SECRET", "secret")
    client = env["client"]
    r = client.get("/oauth/slack/callback", params={"code": "c", "state": oauth.sign_state("acme", "slack",
                                                                                           {"channel": "eng"})},
                   follow_redirects=False)
    assert r.headers["location"].endswith("connected=slack")
    assert credentials.get("acme", "slack")["bot_token"] == "xoxb-oauth"
    assert credentials.get("acme", "slack")["channel"] == "eng"
    r = client.get("/oauth/github/callback", params={"code": "c", "state": oauth.sign_state("acme", "github")},
                   follow_redirects=False)
    assert r.headers["location"].endswith("connected=github")
    assert credentials.github_token("acme") == "gho_oauth"
    # Provider-side cancel: back to the dashboard with a reason, nothing stored.
    r = client.get("/oauth/github/callback", params={"error": "access_denied",
                                                     "state": oauth.sign_state("beta", "github")},
                   follow_redirects=False)
    assert "connect_error=github" in r.headers["location"] and credentials.get("beta", "github") is None
