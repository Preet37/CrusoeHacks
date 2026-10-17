"""Offline checks for Listener -> Conductor -> Diagrammer using fake models."""

import asyncio
import json
import unittest
from unittest.mock import patch

from board import BoardState
from agents.conductor import Conductor
from agents.diagrammer import Diagrammer, StaleBoardError
from agents.listener import Listener


class FakeSession:
    def __init__(self):
        self.board = BoardState()
        self.messages = []
        self.updates = []

    async def broadcast(self, message):
        self.messages.append(message)

    def push_update(self, text):
        self.updates.append(text)


class MindPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inflight_lines_remain_pending_and_events_form_tree(self):
        session = FakeSession()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        peak = 0
        calls = []

        async def chat(model, messages, **kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            payload = json.loads(messages[-1]["content"])
            calls.append(payload["new_lines"])
            try:
                if len(calls) == 1:
                    first_started.set()
                    await release_first.wait()
                    return json.dumps([{"type": "system_fact", "text": "web talks to auth",
                                        "entities": ["web", "auth"]}])
                return json.dumps([{"type": "system_fact", "text": "auth uses Redis",
                                    "entities": ["auth", "Redis"]}])
            finally:
                active -= 1

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            facts = json.loads(messages[-1]["content"])["facts"]
            if "web talks" in facts[0]["text"]:
                await handlers["add_box"](id="web", label="Web app", x=100, y=300, kind="client")
                await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
                await handlers["connect"](id="e_web_auth", from_id="web", to_id="auth", label="login")
            else:
                await handlers["add_box"](id="redis", label="Redis", x=400, y=550, kind="datastore")
                await handlers["connect"](id="e_auth_redis", from_id="auth", to_id="redis", label="sessions")
            return "done"

        listener = Listener(session, debounce_seconds=0)
        with patch("agents.listener.llm.chat", chat), patch("agents.diagrammer.llm.run_json_ops", tools):
            listener.submit("Preet", "the web app talks to auth")
            await asyncio.wait_for(first_started.wait(), 1)
            listener.submit("Preet", "auth also uses Redis")
            release_first.set()
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(peak, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], [{"speaker": "Preet", "text": "auth also uses Redis"}])
        self.assertEqual(set(session.board.boxes), {"web", "auth", "redis"})
        self.assertEqual(set(session.board.edges), {"e_web_auth", "e_auth_redis"})
        self.assertEqual(len(session.updates), 2)
        self.assertEqual(len([m for m in session.messages if m["type"] == "draw"]), 5)
        started = [m for m in session.messages if m["type"] == "agent_event" and m["state"] == "started"]
        for agent in ("Listener", "Conductor", "Diagrammer"):
            self.assertEqual(len([e for e in started if e["agent"] == agent]), 2)
        for listener_event in [e for e in started if e["agent"] == "Listener"]:
            conductor = next(e for e in started if e["parent_id"] == listener_event["event_id"])
            diagrammer = next(e for e in started if e["parent_id"] == conductor["event_id"])
            self.assertEqual(diagrammer["agent"], "Diagrammer")
            for event in (listener_event, conductor, diagrammer):
                self.assertTrue(any(e.get("event_id") == event["event_id"] and e.get("state") == "done"
                                    for e in session.messages))

    async def test_invalid_and_excess_draw_calls_do_not_escape_board(self):
        session = FakeSession()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            self.assertEqual(max_ops, 8)
            with self.assertRaises(ValueError):
                await handlers["add_box"](id="bad", label="Bad", x=1001, y=0, kind="service")
            for index in range(12):
                await handlers["add_box"](id=f"box{index}", label=f"Box {index}",
                                          x=index * 20, y=100, kind="service")
            return None

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            draws = await Diagrammer().handle(session, [{"type": "system_fact", "text": "many boxes"}])
        self.assertEqual(len(draws), 7)
        self.assertEqual(len(session.board.boxes), 7)
        self.assertEqual(len(session.updates), 1)
        self.assertNotIn("bad", session.board.boxes)
        self.assertEqual(len([m for m in session.messages if m["type"] == "draw"]), 7)

    async def test_malformed_json_plan_falls_back_to_native_before_any_op(self):
        session = FakeSession()
        calls = []

        async def invalid_json(*args, **kw):
            calls.append("json")
            raise ValueError("invalid JSON ops")

        async def native(model, messages, definitions, handlers, max_steps, **kw):
            calls.append("native")
            self.assertEqual(max_steps, 8)
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", invalid_json), \
                patch("agents.diagrammer.llm.run_tools", native):
            await Diagrammer().handle(session, [{"type": "system_fact", "text": "auth exists"}])
        self.assertEqual(calls, ["json", "json", "native"])
        self.assertEqual(set(session.board.boxes), {"auth"})

    async def test_json_error_after_an_attempt_does_not_replay_or_retry_native(self):
        session = FakeSession()
        native_called = False

        async def partial(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
            raise ValueError("later operation invalid")

        async def native(*args, **kw):
            nonlocal native_called
            native_called = True

        with patch("agents.diagrammer.llm.run_json_ops", partial), \
                patch("agents.diagrammer.llm.run_tools", native):
            with self.assertRaisesRegex(ValueError, "later operation invalid"):
                await Diagrammer().handle(session, [{"type": "system_fact", "text": "auth exists"}])
        self.assertFalse(native_called)
        self.assertEqual(set(session.board.boxes), {"auth"})
        self.assertEqual(len(session.updates), 1)

    async def test_network_error_does_not_retry_with_native(self):
        session = FakeSession()
        native_called = False

        async def network_error(*args, **kw):
            raise RuntimeError("network unavailable")

        async def native(*args, **kw):
            nonlocal native_called
            native_called = True

        with patch("agents.diagrammer.llm.run_json_ops", network_error), \
                patch("agents.diagrammer.llm.run_tools", native):
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                await Diagrammer().handle(session, [{"type": "system_fact", "text": "auth exists"}])
        self.assertFalse(native_called)
        self.assertEqual(session.board.boxes, {})

    async def test_quiet_window_batches_burst_of_final_lines(self):
        session = FakeSession()
        calls = []

        async def chat(model, messages, **kw):
            calls.append(json.loads(messages[-1]["content"])["new_lines"])
            return "[]"

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, debounce_seconds=0.03)
            listener.submit("Preet", "the web app")
            await asyncio.sleep(0.015)
            listener.submit("Preet", "calls auth")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual([line["text"] for line in calls[0]], ["the web app", "calls auth"])

    async def test_explicit_component_rename_routes_even_if_model_calls_it_a_task(self):
        session = FakeSession()
        session.board.apply({"op": "add_box", "id": "auth", "label": "Auth", "x": 400,
                             "y": 300, "kind": "service"})

        async def chat(*args, **kw):
            return '[{"type":"task","text":"rename auth to Auth (sessions)","entities":["auth"]}]'

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["rename"](id="auth", label="Auth (sessions)")

        with patch("agents.listener.llm.chat", chat), patch("agents.diagrammer.llm.run_json_ops", tools):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "rename auth to Auth (sessions)")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual(session.board.boxes["auth"]["label"], "Auth (sessions)")
        self.assertEqual(len([m for m in session.messages if m["type"] == "draw"]), 1)

    async def test_listener_retries_truncated_json_once(self):
        session = FakeSession()
        output = iter(['[{"type":"system_fact"', "[]"])
        tokens = []

        async def chat(model, messages, **kw):
            self.assertFalse(kw["extra_body"]["chat_template_kwargs"]["enable_thinking"])
            tokens.append(kw["max_tokens"])
            return next(output)

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "hello")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual(tokens, [1600, 2400])
        self.assertTrue(any(m.get("state") == "done" for m in session.messages))

    async def test_noop_does_not_broadcast_or_queue_update(self):
        session = FakeSession()
        session.board.apply({"op": "add_box", "id": "auth", "label": "Auth", "x": 400,
                             "y": 300, "kind": "service"})

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["rename"](id="auth", label="Auth")
            return "done"

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            draws = await Diagrammer().handle(session, [{"type": "system_fact", "text": "auth exists"}])
        self.assertEqual(draws, [])
        self.assertEqual(session.updates, [])
        self.assertFalse(any(m["type"] == "draw" for m in session.messages))

    async def test_failure_and_cancellation_have_terminal_events(self):
        failed_session = FakeSession()

        async def failed_chat(*args, **kw):
            raise RuntimeError("offline failure")

        with patch("agents.listener.llm.chat", failed_chat):
            listener = Listener(failed_session, debounce_seconds=0)
            listener.submit("Preet", "auth uses db")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual([m["state"] for m in failed_session.messages if m["type"] == "agent_event"],
                         ["started", "failed"])

        cancelled_session = FakeSession()
        started = asyncio.Event()

        async def slow_chat(*args, **kw):
            started.set()
            await asyncio.Event().wait()

        with patch("agents.listener.llm.chat", slow_chat):
            listener = Listener(cancelled_session, debounce_seconds=0)
            listener.submit("Preet", "auth uses db")
            await asyncio.wait_for(started.wait(), 1)
            await listener.close()
        self.assertEqual([m["state"] for m in cancelled_session.messages if m["type"] == "agent_event"],
                         ["started", "cancelled"])

    async def test_conductor_routes_only_system_facts(self):
        session = FakeSession()

        class FakeDiagrammer:
            def __init__(self):
                self.calls = []

            async def handle(self, session, facts, parent_id):
                self.calls.append((facts, parent_id))

        diagrammer = FakeDiagrammer()
        await Conductor(diagrammer).handle(session, [
            {"type": "question", "text": "Why?"},
            {"type": "system_fact", "text": "auth uses db"},
        ])
        self.assertEqual(len(diagrammer.calls), 1)
        self.assertEqual(diagrammer.calls[0][0], [{"type": "system_fact", "text": "auth uses db"}])

    async def test_partial_draw_failure_still_queues_one_update_and_marks_tree_failed(self):
        session = FakeSession()

        async def chat(*args, **kw):
            return '[{"type":"system_fact","text":"auth exists","entities":["auth"]}]'

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
            raise RuntimeError("model failed after drawing")

        with patch("agents.listener.llm.chat", chat), patch("agents.diagrammer.llm.run_json_ops", tools):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "auth exists")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual(set(session.board.boxes), {"auth"})
        self.assertEqual(len(session.updates), 1)
        events = [m for m in session.messages if m["type"] == "agent_event"]
        self.assertEqual({m["agent"] for m in events if m["state"] == "failed"},
                         {"Listener", "Conductor", "Diagrammer"})

    async def test_cancelled_diagram_job_marks_entire_tree_cancelled(self):
        session = FakeSession()
        drawing = asyncio.Event()

        async def chat(*args, **kw):
            return '[{"type":"system_fact","text":"auth exists","entities":["auth"]}]'

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
            drawing.set()
            await asyncio.Event().wait()

        with patch("agents.listener.llm.chat", chat), patch("agents.diagrammer.llm.run_json_ops", tools):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "auth exists")
            await asyncio.wait_for(drawing.wait(), 1)
            await listener.close()
        self.assertEqual(set(session.board.boxes), {"auth"})
        self.assertEqual(len(session.updates), 1)
        events = [m for m in session.messages if m["type"] == "agent_event"]
        self.assertEqual({m["agent"] for m in events if m["state"] == "cancelled"},
                         {"Listener", "Conductor", "Diagrammer"})

    async def test_cancellation_finishes_draw_broadcast_before_job_stops(self):
        drawing = asyncio.Event()
        release = asyncio.Event()

        class SlowSession(FakeSession):
            async def broadcast(self, message):
                if message["type"] == "draw":
                    drawing.set()
                    await release.wait()
                await super().broadcast(message)

        session = SlowSession()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            task = asyncio.create_task(Diagrammer().handle(
                session, [{"type": "system_fact", "text": "auth exists"}]))
            await asyncio.wait_for(drawing.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(len([m for m in session.messages if m["type"] == "draw"]), 1)
        self.assertEqual(len(session.updates), 1)

    async def test_failed_draw_broadcast_reconciles_with_snapshot(self):
        class FailingSession(FakeSession):
            async def broadcast(self, message):
                if message["type"] == "draw":
                    raise RuntimeError("socket failed")
                await super().broadcast(message)

        session = FailingSession()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            with self.assertRaisesRegex(RuntimeError, "socket failed"):
                await Diagrammer().handle(session, [{"type": "system_fact", "text": "auth exists"}])
        self.assertIn("auth", session.board.boxes)
        self.assertEqual(len(session.updates), 1)
        snapshots = [m for m in session.messages if m["type"] == "board_snapshot"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0], session.board.snapshot())

    async def test_clear_during_model_call_rejects_stale_draw(self):
        session = FakeSession()
        model_started = asyncio.Event()
        model_continue = asyncio.Event()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            model_started.set()
            await model_continue.wait()
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            task = asyncio.create_task(Diagrammer().handle(
                session, [{"type": "system_fact", "text": "auth exists"}]))
            await asyncio.wait_for(model_started.wait(), 1)
            session.board.apply({"op": "clear"})
            model_continue.set()
            with self.assertRaises(StaleBoardError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(session.board.boxes, {})
        self.assertEqual(session.updates, [])
        self.assertFalse(any(m["type"] == "draw" for m in session.messages))
        self.assertTrue(any(m.get("state") == "cancelled" for m in session.messages))

    async def test_clear_during_draw_broadcast_reconciles_snapshot(self):
        drawing = asyncio.Event()
        release = asyncio.Event()

        class SlowSession(FakeSession):
            async def broadcast(self, message):
                if message["type"] == "draw":
                    drawing.set()
                    await release.wait()
                await super().broadcast(message)

        session = SlowSession()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            task = asyncio.create_task(Diagrammer().handle(
                session, [{"type": "system_fact", "text": "auth exists"}]))
            await asyncio.wait_for(drawing.wait(), 1)
            session.board.apply({"op": "clear"})
            release.set()
            with self.assertRaises(StaleBoardError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(session.board.boxes, {})
        snapshots = [m for m in session.messages if m["type"] == "board_snapshot"]
        self.assertEqual(snapshots[-1], session.board.snapshot())
        self.assertEqual(session.updates, [])

    async def test_cancel_and_clear_during_draw_broadcast_reconcile(self):
        drawing = asyncio.Event()
        release = asyncio.Event()

        class SlowSession(FakeSession):
            async def broadcast(self, message):
                if message["type"] == "draw":
                    drawing.set()
                    await release.wait()
                await super().broadcast(message)

        session = SlowSession()

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            task = asyncio.create_task(Diagrammer().handle(
                session, [{"type": "system_fact", "text": "auth exists"}]))
            await asyncio.wait_for(drawing.wait(), 1)
            session.board.apply({"op": "clear"})
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(session.board.boxes, {})
        self.assertEqual([m["type"] for m in session.messages if m["type"] in ("draw", "board_snapshot")],
                         ["draw", "board_snapshot"])
        self.assertEqual(session.updates, [])

    async def test_stale_batch_does_not_drop_new_line_queued_during_model_call(self):
        session = FakeSession()
        old_model_started = asyncio.Event()
        resume_old = asyncio.Event()
        line_batches = []

        async def chat(model, messages, **kw):
            lines = json.loads(messages[-1]["content"])["new_lines"]
            line_batches.append(lines)
            fact = "auth exists" if len(line_batches) == 1 else "Redis exists"
            return json.dumps([{"type": "system_fact", "text": fact, "entities": [fact.split()[0]]}])

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            fact = json.loads(messages[-1]["content"])["facts"][0]["text"]
            if fact == "auth exists":
                old_model_started.set()
                await resume_old.wait()
                await handlers["add_box"](id="auth", label="Auth", x=400, y=300, kind="service")
            else:
                await handlers["add_box"](id="redis", label="Redis", x=400, y=300, kind="datastore")

        with patch("agents.listener.llm.chat", chat), patch("agents.diagrammer.llm.run_json_ops", tools):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "auth exists")
            await asyncio.wait_for(old_model_started.wait(), 1)
            listener.submit("Preet", "Redis exists")
            session.board.apply({"op": "clear"})
            resume_old.set()
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual([batch[0]["text"] for batch in line_batches], ["auth exists", "Redis exists"])
        self.assertEqual(set(session.board.boxes), {"redis"})
        self.assertEqual(len(session.updates), 1)
        terminal = [m for m in session.messages if m["type"] == "agent_event" and m["state"] != "started"]
        self.assertEqual(len([m for m in terminal if m["state"] == "cancelled"]), 3)
        self.assertEqual(len([m for m in terminal if m["state"] == "done"]), 3)

    async def test_diagrammer_clear_always_sends_draw_then_snapshot(self):
        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["clear"]()

        for populated in (False, True):
            session = FakeSession()
            if populated:
                session.board.apply({"op": "add_box", "id": "auth", "label": "Auth",
                                     "x": 400, "y": 300, "kind": "service"})
            with patch("agents.diagrammer.llm.run_json_ops", tools):
                await Diagrammer().handle(session, [{"type": "system_fact", "text": "clear"}])
            wire = [m for m in session.messages if m["type"] in ("draw", "board_snapshot")]
            self.assertEqual([m["type"] for m in wire], ["draw", "board_snapshot"])
            self.assertEqual(wire[0]["op"], "clear")
            self.assertEqual(wire[1], session.board.snapshot())
            self.assertEqual(len(session.updates), int(populated))

    async def test_cancelled_clear_still_sends_snapshot(self):
        publishing = asyncio.Event()
        release = asyncio.Event()

        class SlowSession(FakeSession):
            async def broadcast(self, message):
                if message["type"] == "draw" and message["op"] == "clear":
                    publishing.set()
                    await release.wait()
                await super().broadcast(message)

        session = SlowSession()
        session.board.apply({"op": "add_box", "id": "auth", "label": "Auth",
                             "x": 400, "y": 300, "kind": "service"})

        async def tools(model, messages, definitions, handlers, max_ops, **kw):
            await handlers["clear"]()

        with patch("agents.diagrammer.llm.run_json_ops", tools):
            task = asyncio.create_task(Diagrammer().handle(
                session, [{"type": "system_fact", "text": "clear"}]))
            await asyncio.wait_for(publishing.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        wire = [m for m in session.messages if m["type"] in ("draw", "board_snapshot")]
        self.assertEqual([m["type"] for m in wire], ["draw", "board_snapshot"])
        self.assertEqual(session.board.boxes, {})
