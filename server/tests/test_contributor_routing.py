import asyncio
from types import SimpleNamespace

import pytest
from agents.conductor import Conductor
from board import BoardState


@pytest.mark.asyncio
async def test_contribution_published_while_diagram_still_running():
    started, release = asyncio.Event(), asyncio.Event()
    messages = []
    async def broadcast(message):
        messages.append(message)
    session = SimpleNamespace(board=BoardState(), twin_mode='on_request',
                              active_topic={'topic_id': 't_arch'}, broadcast=broadcast)
    class Draw:
        async def handle(self, *args, **kwargs):
            started.set()
            await release.wait()
    class Contribution:
        async def handle(self, *args, **kwargs):
            await started.wait()
            return dict(show='Architecture', current='No repo connected.', impact=[],
                        risks=[], question='Which boundary should we review?', sources=[])
    job = asyncio.create_task(Conductor(diagrammer=Draw(), contributor=Contribution()).handle(
        session, [{'type': 'system_fact', 'text': 'Draw the architecture'}],
        topic_id='t_arch', source_lines=[{'text': 'Twin, draw the architecture'}]))
    try:
        for _ in range(100):
            if any(m['type'] == 'contribution' for m in messages):
                break
            await asyncio.sleep(.001)
        assert any(m['type'] == 'contribution' for m in messages)
        assert not job.done()
        assert session.latest_contribution['question'].endswith('?')
    finally:
        release.set()
        await job


@pytest.mark.asyncio
async def test_off_mode_does_not_start_contributor():
    class Draw:
        async def handle(self, *args, **kwargs):
            return []
    class Contribution:
        async def handle(self, *args, **kwargs):
            raise AssertionError('off mode must not call Contributor')
    async def broadcast(message):
        pass
    session = SimpleNamespace(board=BoardState(), twin_mode='off', broadcast=broadcast)
    await Conductor(diagrammer=Draw(), contributor=Contribution()).handle(
        session, [{'type': 'system_fact', 'text': 'Draw the architecture'}],
        source_lines=[{'text': 'Twin, draw the architecture'}])
    assert not hasattr(session, 'latest_contribution')
