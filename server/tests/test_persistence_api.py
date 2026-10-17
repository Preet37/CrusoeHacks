from types import SimpleNamespace

from fastapi.testclient import TestClient

import app
from brain.meeting_store import save_meeting
from brain.workspace_store import save_repo


def test_workspace_login_persists_invite_and_dashboard_data(monkeypatch, tmp_path):
    monkeypatch.setenv('TWIN_DB_PATH', str(tmp_path / 'company.sqlite3'))
    monkeypatch.setenv('WORKSPACES', '')
    client = TestClient(app.app)
    params = {'workspace': 'acme', 'code': 'correct-invite'}
    assert client.get('/workspace', params=params).status_code == 200
    save_repo('acme', 'https://github.com/example/demo.git')
    session = SimpleNamespace(workspace='acme', room='acme-standup',
        history=[{'speaker': 'Avery', 'text': 'We decided to add retries.', 'ts': 1}],
        ticket_drafts={'retry': {'id': 'T-1', 'title': 'Add retries', 'owner': 'Avery',
            'body': 'We decided to add retries.', 'status': 'draft',
            'links': {'jira': None, 'slack': None, 'agent': None}}})
    save_meeting(session)
    # A fresh API client reads persisted state, and cannot replace the invite.
    client = TestClient(app.app)
    assert client.get('/workspace', params=params).json()['repo_url'].endswith('demo.git')
    assert len(client.get('/meetings', params=params).json()['meetings']) == 1
    assert client.get('/tickets', params=params).json()['tickets'][0]['owner'] == 'Avery'
    for route in ('/workspace', '/meetings', '/tickets'):
        assert client.get(route, params={**params, 'code': 'wrong'}).status_code == 403
    assert client.get('/meetings', params={'workspace': 'other', 'code': 'other-invite'}).json() == {'meetings': []}


def test_overlapping_workspace_names_and_unscoped_rooms(monkeypatch, tmp_path):
    monkeypatch.setenv('TWIN_DB_PATH', str(tmp_path / 'company.sqlite3'))
    monkeypatch.setenv('WORKSPACES', 'a:first,a-b:second')
    assert app.room_owner('a-b-standup') == 'a-b'
    assert app.room_access_error('a-b-standup', 'a')
    assert app.room_access_error('legacy', 'a')
    assert app.room_access_error('a-b-standup', 'a-b') is None


def test_last_socket_disconnect_saves_meeting(monkeypatch, tmp_path):
    import mouth
    from session import SESSIONS
    monkeypatch.setenv('TWIN_DB_PATH', str(tmp_path / 'company.sqlite3'))
    monkeypatch.setenv('WORKSPACES', 'disconnect:invite')
    monkeypatch.setattr(mouth, 'attach', lambda session: None)
    client = TestClient(app.app)
    with client.websocket_connect('/ws?room=disconnect-review&workspace=disconnect&code=invite') as ws:
        assert ws.receive_json()['type'] == 'status'
        SESSIONS['disconnect-review'].history.append(
            {'speaker': 'Preet', 'text': 'We agreed to add retries.', 'ts': 1})
    saved = client.get('/meetings', params={'workspace': 'disconnect', 'code': 'invite'}).json()['meetings']
    assert len(saved) == 1
    assert saved[0]['room'] == 'disconnect-review'
    assert saved[0]['decisions'] == ['We agreed to add retries.']
