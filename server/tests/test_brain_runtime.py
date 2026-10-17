from types import SimpleNamespace

from brain import runtime


def test_runtime_attaches_real_demo_owners_and_separates_code_facts(monkeypatch, tmp_path):
    monkeypatch.setenv('TWIN_STATE_DIR', str(tmp_path))
    monkeypatch.delenv('TWIN_REPO_ROOT', raising=False)
    session = SimpleNamespace(room='runtime-test')
    runtime.attach(session)
    assert session._mind_runtime_ready
    assert session.code_map.files['auth/login.py']['owner'] == 'Avery Chen'
    assert session.code_fact_scope != session.decision_scope
    facts = session.memory.current_facts(scope=session.code_fact_scope)
    assert len(facts) == 2
    assert any('JWT' in fact['claim'] and 'auth/login.py' in fact['source'] for fact in facts)
    runtime.attach(session)
    assert session.memory.current_facts() == facts
    assert callable(session.repo_connector)
