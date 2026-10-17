import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from voice import tts


class Response:
    def __init__(self, status=200, retry_after=None):
        self.status_code = status
        self.content = b"RIFF" + bytes(40) if status == 200 else b""
        self.text = "rate limited" if status == 429 else ""
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


@pytest.fixture(autouse=True)
def provider_settings(monkeypatch):
    monkeypatch.setattr(tts.config, "NVIDIA_API_KEY", "fake")
    monkeypatch.setattr(tts, "PROVIDER_INTERVAL_S", .02)
    monkeypatch.setattr(tts, "RETRY_429_BACKOFF_S", (.03, .04))
    monkeypatch.setattr(tts, "MAX_RETRY_AFTER_S", .05)
    monkeypatch.setattr(tts, "SYNTH_BUDGET_S", .6)
    tts._CACHE.clear()


@pytest.mark.asyncio
async def test_concurrent_rooms_and_prewarm_share_one_paced_provider(monkeypatch):
    starts = []
    active = 0
    peak = 0

    class Client:
        async def post(self, *args, **kwargs):
            nonlocal active, peak
            starts.append(asyncio.get_running_loop().time())
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.003)
            active -= 1
            return Response()

    monkeypatch.setattr(tts, "get_client", lambda: Client())
    emitted = []
    async def emit(message):
        emitted.append(message)
    await asyncio.gather(
        tts.speak_text("Here is room one. This is its second sentence.", "room1", emit),
        tts.speak_text("Here is room two. This is another second sentence.", "room2", emit),
        tts.prewarm(["Warm this cached phrase."]),
    )
    assert len(starts) == 5 and peak == 1
    assert all(b - a >= .018 for a, b in zip(starts, starts[1:]))
    assert len(emitted) == 4 and all(item["data"] for item in emitted)


@pytest.mark.asyncio
async def test_retry_after_is_capped_and_other_requests_wait_for_retry():
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append((kwargs["files"]["text"][1], asyncio.get_running_loop().time()))
            return Response(429, "999") if len(calls) == 1 else Response()
    client = Client()
    results = await asyncio.gather(tts.synthesize("first", client), tts.synthesize("second", client))
    assert all(results)
    assert [text for text, _ in calls] == ["first", "first", "second"]
    assert .045 <= calls[1][1] - calls[0][1] < .3


@pytest.mark.asyncio
async def test_cancelled_retry_preserves_shared_cooldown():
    started = asyncio.Event()
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append(asyncio.get_running_loop().time())
            if len(calls) == 1:
                started.set()
                return Response(429, "999")
            return Response()
    client = Client()
    task = asyncio.create_task(tts.synthesize("cancel retry", client))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await tts.synthesize("next after cancellation", client)
    assert len(calls) == 2 and calls[1] - calls[0] >= .045


@pytest.mark.asyncio
async def test_cancelling_owner_and_waiter_releases_gate():
    started = asyncio.Event()
    hold = asyncio.Event()
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append(kwargs["files"]["text"][1])
            if len(calls) == 1:
                started.set()
                await hold.wait()
            return Response()
    client = Client()
    owner = asyncio.create_task(tts.synthesize("owner", client))
    await started.wait()
    waiter = asyncio.create_task(tts.synthesize("cancelled waiter", client))
    await asyncio.sleep(0)
    for task in (waiter, owner):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await asyncio.wait_for(tts.synthesize("next owner", client), .3)
    assert calls == ["owner", "next owner"]


@pytest.mark.asyncio
async def test_cached_phrase_bypasses_busy_gate_and_queued_duplicates_recheck_cache():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append(1)
            started.set()
            await release.wait()
            return Response()
    client = Client()
    tts._CACHE[(tts.VOICE, "cached")] = b"RIFFcached"
    first = asyncio.create_task(tts.synthesize("duplicate", client))
    await started.wait()
    second = asyncio.create_task(tts.synthesize("duplicate", client))
    assert await asyncio.wait_for(tts.synthesize("cached", client), .01) == b"RIFFcached"
    release.set()
    assert all(await asyncio.gather(first, second))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_queue_budget_falls_back_without_network_request(monkeypatch):
    monkeypatch.setattr(tts, "SYNTH_BUDGET_S", .01)
    class Client:
        async def post(self, *args, **kwargs):
            raise AssertionError("timed out waiter must never post")
    async with tts._provider_gate().lock:
        assert await tts.synthesize("queue timeout", Client()) is None


def test_gate_can_be_used_from_successive_event_loops():
    async def run():
        gate = tts._provider_gate()
        async def acquire():
            async with gate.lock:
                await asyncio.sleep(.001)
        await asyncio.gather(acquire(), acquire())
        assert tts._provider_gate() is gate
        return gate
    assert asyncio.run(run()) is not asyncio.run(run())


def test_retry_after_parses_dates_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setattr(tts.time, "time", lambda: 1000)
    future = format_datetime(datetime.fromtimestamp(1002, timezone.utc), usegmt=True)
    assert tts._retry_delay(Response(429, future), .03) == .05
    assert tts._retry_delay(Response(429, "bad date"), .03) == .03
    assert tts._retry_delay(Response(429, "-12"), .03) == .03


@pytest.mark.asyncio
async def test_persistent_rate_limit_has_bounded_attempts():
    calls = []
    class Client:
        async def post(self, *args, **kwargs):
            calls.append(1)
            return Response(429)
    assert await tts.synthesize("always limited", Client()) is None
    assert len(calls) == 3
