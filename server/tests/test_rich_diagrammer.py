import asyncio
import json

import pytest

import config
import llm
from agents.diagrammer import Diagrammer, StaleBoardError, repair_draw
from board import BoardState


class Session:
    def __init__(self):
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, text):
        self.updates.append(text)


@pytest.mark.asyncio
async def test_rich_plan_uses_one_call_and_topic_frame(monkeypatch):
    calls = []

    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(kwargs)
        await handlers["add_mermaid"](id="login_flow", code="sequenceDiagram\n Browser->>Auth: login", x=100, y=150)
        await handlers["add_chart"](id="latency", chart="bar", title="Latency", labels=["v1", "v2"],
                                    series=[{"name": "p95", "values": [420, 180]}], x=500, y=150)
        await handlers["add_mock"](id="login_ui", title="Login", html="<div><input placeholder='Email'><button>Sign in</button></div>", x=200, y=500)

    monkeypatch.setattr(llm, "run_json_ops", plan)
    session = Session()
    await Diagrammer().handle(session, [{"type": "system_fact", "text": "Login flow and metrics"}],
                               topic_id="t_auth", title="Authentication", topic_changed=True)
    assert len(calls) == 1
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert session.board.active_frame == "t_auth"
    assert all(session.board.shapes[name]["frame"] == "t_auth" for name in ("login_flow", "latency", "login_ui"))
    assert len(session.updates) == 1
    assert [m["op"] for m in session.sent if m["type"] == "draw"] == ["frame", "focus", "add_mermaid", "add_chart", "add_mock"]


def mermaid(session):
    session.board.apply({"op": "add_mermaid", "id": "flow", "code": "bad syntax", "x": 0, "y": 0})


@pytest.mark.asyncio
async def test_mermaid_repair_is_once_even_with_two_clients(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def chat(*args, **kwargs):
        calls.append(1)
        started.set()
        await release.wait()
        return json.dumps({"code": "flowchart LR\n A-->B"})

    monkeypatch.setattr(llm, "chat", chat)
    session = Session()
    mermaid(session)
    message = {"id": "flow", "op": "add_mermaid", "error": "bad syntax"}
    pending = asyncio.create_task(repair_draw(session, message))
    await started.wait()
    assert await repair_draw(session, message) is False
    release.set()
    assert await pending is True
    assert len(calls) == 1
    assert session.board.shapes["flow"]["code"] == "flowchart LR\n A-->B"
    assert sum(m["type"] == "draw" and m["op"] == "update" for m in session.sent) == 1
    assert await repair_draw(session, message) is False


@pytest.mark.asyncio
async def test_clear_during_mermaid_repair_prevents_stale_update(monkeypatch):
    session = Session()
    mermaid(session)

    async def chat(*args, **kwargs):
        session.board.apply({"op": "clear"})
        return '{"code":"flowchart LR; A-->B"}'

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(StaleBoardError):
        await repair_draw(session, {"id": "flow", "error": "syntax"})
    assert session.board.shapes == {}
    assert not any(m["type"] == "draw" for m in session.sent)


@pytest.mark.asyncio
async def test_invalid_mock_plan_repaired_before_any_semantic_draw(monkeypatch):
    session = Session()
    calls = []
    async def chat(model, *args, **kwargs):
        assert model == config.MODEL_SMART
        calls.append(kwargs)
        # The earlier valid box must not leak out before the unsafe mock is fixed.
        assert not session.board.boxes
        html = '<form><input></form>' if len(calls) == 1 else '<div><input></div>'
        return json.dumps({'ops': [
            {'op': 'add_box', 'id': 'auth', 'label': 'Auth', 'x': 10, 'y': 10, 'kind': 'service'},
            {'op': 'add_mock', 'id': 'screen', 'title': 'Login', 'html': html, 'x': 200, 'y': 200},
        ]})
    monkeypatch.setattr(llm, 'chat', chat)
    await Diagrammer().handle(session, [{'type': 'task', 'text': 'Show login screen'}])
    assert len(calls) == 2
    assert session.board.shapes['screen']['html'] == '<div><input></div>'
    assert [m['op'] for m in session.sent if m['type'] == 'draw'] == ['add_box', 'add_mock']
    assert len(session.updates) == 1


@pytest.mark.asyncio
async def test_measurement_cannot_silently_complete_with_empty_plan(monkeypatch):
    calls = []
    async def chat(model, *args, **kwargs):
        assert model == config.MODEL_SMART
        calls.append(1)
        if len(calls) == 1:
            return '{"ops":[]}'
        return json.dumps({'ops': [{'op': 'add_chart', 'id': 'latency', 'chart': 'bar',
                                   'title': 'p95 latency (ms)', 'labels': ['before', 'after'],
                                   'series': [{'name': 'p95', 'values': [420, 180]}], 'x': 0, 'y': 0}]})
    monkeypatch.setattr(llm, 'chat', chat)
    session = Session()
    text = 'Our p95 latency went from 420 to 180 milliseconds after the cache.'
    await Diagrammer().handle(session, [{'type': 'claim', 'text': text}], source_lines=[{'text': text}])
    assert len(calls) == 2
    assert session.board.shapes['latency']['series'][0]['values'] == [420, 180]


@pytest.mark.asyncio
@pytest.mark.parametrize('first_plan', ['{"ops":[]}',
                                         '{"ops":[{"op":"focus","id":"t_auth"}]}'])
async def test_plain_system_fact_repairs_empty_or_topic_only_plan_before_content(monkeypatch,
                                                                                 first_plan):
    session = Session()
    calls = []

    async def chat(*args, **kwargs):
        calls.append(1)
        # Topic frame/focus may already exist, but no factual box may be
        # applied before the complete replacement plan is validated.
        assert session.board.boxes == {}
        if len(calls) == 1:
            return first_plan
        return json.dumps({'ops': [{'op': 'add_box', 'id': 'redis', 'label': 'Redis',
                                   'x': 200, 'y': 300, 'kind': 'datastore'}]})

    async def unexpected_native(*args, **kwargs):
        raise AssertionError('the repaired JSON plan should not need native fallback')

    monkeypatch.setattr(llm, 'chat', chat)
    monkeypatch.setattr(llm, 'run_tools', unexpected_native)
    await Diagrammer().handle(session, [{'type': 'system_fact', 'text': 'Redis exists'}],
                               topic_id='t_auth', title='Auth', topic_changed=True)
    assert len(calls) == 2
    assert session.board.boxes['redis']['label'] == 'Redis'
    assert [m['op'] for m in session.sent if m['type'] == 'draw'] == [
        'frame', 'focus', 'add_box']
    assert len(session.updates) == 1


@pytest.mark.asyncio
async def test_fact_with_two_empty_json_plans_and_empty_native_fallback_fails_visibly(monkeypatch):
    session = Session()
    calls = []

    async def chat(*args, **kwargs):
        calls.append('json')
        return '{"ops":[]}'

    async def native(*args, **kwargs):
        calls.append('native')
        return None

    monkeypatch.setattr(llm, 'chat', chat)
    monkeypatch.setattr(llm, 'run_tools', native)
    with pytest.raises(ValueError, match='no content operation'):
        await Diagrammer().handle(session, [{'type': 'system_fact', 'text': 'Auth uses Redis'}])
    # Nano gets one bounded JSON repair, then Super gets its own bounded repair
    # before native fallback; none applies a partial board operation.
    assert calls == ['json', 'json', 'json', 'json', 'native']
    assert session.board.boxes == {}
    assert any(m.get('agent') == 'Diagrammer' and m.get('state') == 'failed'
               for m in session.sent)


@pytest.mark.asyncio
async def test_simple_component_burst_uses_nano_with_compact_tools(monkeypatch):
    session = Session()
    calls = []

    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append((model, {tool['function']['name'] for tool in tools}, kwargs))
        assert 'board' not in json.loads(messages[-1]['content'])
        await handlers['add_box'](id='web', label='Web app', x=80, y=200, kind='client')
        await handlers['add_box'](id='auth', label='Auth', x=350, y=200, kind='service')
        await handlers['connect'](id='web_auth', from_id='web', to_id='auth', label='login')

    monkeypatch.setattr(llm, 'run_json_ops', plan)
    await Diagrammer().handle(session, [{'type': 'system_fact',
                                         'text': 'Web app calls Auth'}],
                               source_lines=[{'text': 'The web app calls Auth.'}])
    assert len(calls) == 1
    assert calls[0][0] == config.MODEL_FAST
    assert calls[0][1] == {'add_box', 'connect', 'rename', 'remove', 'highlight'}
    assert calls[0][2]['max_tokens'] == 850
    assert set(session.board.boxes) == {'web', 'auth'}
    assert set(session.board.edges) == {'web_auth'}


@pytest.mark.asyncio
async def test_fast_followup_repairs_missing_connection_before_adding_box(monkeypatch):
    session = Session()
    session.board.apply({'op': 'add_box', 'id': 'auth', 'label': 'Auth',
                         'x': 200, 'y': 200, 'kind': 'service'})
    calls = []

    async def chat(model, messages, **kwargs):
        assert model == config.MODEL_FAST
        calls.append(1)
        assert 'redis' not in session.board.boxes
        ops = [{'op': 'add_box', 'id': 'redis', 'label': 'Redis',
                'x': 500, 'y': 200, 'kind': 'datastore'}]
        if len(calls) == 2:
            ops.append({'op': 'connect', 'id': 'auth_redis', 'from_id': 'auth',
                        'to_id': 'redis', 'label': 'sessions'})
        return json.dumps({'ops': ops})

    monkeypatch.setattr(llm, 'chat', chat)
    text = 'Auth also uses Redis for sessions.'
    await Diagrammer().handle(session, [{'type': 'system_fact', 'text': text}],
                               source_lines=[{'text': text}])
    assert len(calls) == 2
    assert [m['op'] for m in session.sent if m['type'] == 'draw'] == ['add_box', 'connect']
    assert session.board.edges['auth_redis']['to_id'] == 'redis'


@pytest.mark.asyncio
async def test_invalid_nano_plan_falls_back_to_super_before_side_effects(monkeypatch):
    session = Session()
    calls = []

    async def plan(model, messages, tools, handlers, **kwargs):
        calls.append(model)
        if model == config.MODEL_FAST:
            assert not session.board.boxes
            raise ValueError('invalid fast plan')
        await handlers['add_box'](id='auth', label='Auth', x=300, y=300, kind='service')

    monkeypatch.setattr(llm, 'run_json_ops', plan)
    await Diagrammer().handle(session, [{'type': 'system_fact', 'text': 'Auth exists'}])
    assert calls == [config.MODEL_FAST, config.MODEL_SMART]
    assert set(session.board.boxes) == {'auth'}
