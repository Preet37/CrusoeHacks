from types import SimpleNamespace

import pytest

from agents.diagrammer import Diagrammer, StaleBoardError
from agents.repo_diagram import draw_repo, repo_architecture_request, repo_ops
from board import BoardState


def code_map(count=12):
    paths = [f"src/module{i}.py" for i in range(count)]
    return SimpleNamespace(files={path: {"imports": paths[i+1:i+2] + ["missing.py"],
                                        "imported_by": paths[max(0, i-1):i],
                                        "owner": "Avery" if i == 0 else None}
                                  for i, path in enumerate(paths)})


def test_request_recognizes_live_repo_question():
    assert repo_architecture_request([], [{"text": "Twin sketch what the repo looks like, its architecture and workflow"}])
    assert repo_architecture_request([{"text": "walk me through the repository and how it works"}])
    assert repo_architecture_request([{"text": "draw a robot arm"}]) is None


def test_graph_only_uses_indexed_files_and_import_edges():
    mapped = code_map()
    board = BoardState()
    ops = repo_ops(mapped, board, None)
    boxes = {op["id"]: op["label"].splitlines()[0] for op in ops if op["op"] == "add_box"}
    assert len(boxes) == 12
    assert set(boxes.values()) == set(mapped.files)
    assert any("Owner: Avery" in op.get("label", "") for op in ops)
    for op in ops:
        if op["op"] == "connect":
            assert boxes[op["to_id"]] in mapped.files[boxes[op["from_id"]]]["imports"]
    assert board.boxes == {}  # planning validates a copy


def test_large_graph_is_bounded_without_inventing_edges():
    ops = repo_ops(code_map(100), BoardState(), None)
    assert len([op for op in ops if op["op"] == "add_box"]) == 20
    assert len([op for op in ops if op["op"] == "connect"]) <= 32
    assert repo_ops(None, BoardState(), None) == []


class Session:
    def __init__(self, mapped):
        self.code_map = mapped
        self.board = BoardState()
        self.sent = []
        self.updates = []

    async def broadcast(self, message):
        self.sent.append(message)

    def push_update(self, message):
        self.updates.append(message)


@pytest.mark.asyncio
async def test_diagrammer_draws_repo_without_model_call(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("repository graph must not call an LLM")
    monkeypatch.setattr("llm.chat", forbidden)
    monkeypatch.setattr("llm.run_json_ops", forbidden)
    session = Session(code_map())
    result = await Diagrammer().handle(session, [{"type": "question", "text": "Sketch what the repo looks like"}],
                                      topic_id="repo", title="Repo", topic_changed=True)
    assert len(session.board.boxes) == 12
    assert len(session.board.edges) == 11
    assert any(draw["op"] == "add_box" for draw in result)
    assert session.board.active_frame == "repo"
    assert "mapped" in session.updates[-1]


@pytest.mark.asyncio
async def test_no_index_reports_missing_context_and_draws_no_fake_nodes():
    session = Session(None)
    await Diagrammer().handle(session, [{"text": "Draw the repo architecture"}])
    assert not session.board.boxes
    assert "Connect the repository" in session.updates[-1]


@pytest.mark.asyncio
async def test_clear_during_publish_stops_remaining_graph():
    session = Session(code_map())
    async def publish(*args):
        session.board.apply({"op": "clear"})
    with pytest.raises(StaleBoardError):
        await draw_repo(session, session.board, None, publish, session.board.revision)
    assert not session.board.boxes


def test_overview_without_topic_focuses_parent_containing_all_rows():
    ops = repo_ops(code_map(20), BoardState(), None)
    parent = ops[0]
    assert parent['op'] == 'frame'
    rows = [op for op in ops[1:] if op['op'] == 'frame']
    assert len(rows) == 4
    assert all(row['frame'] == parent['id'] for row in rows)
    assert ops[-1] == {'op': 'focus', 'id': parent['id']}
