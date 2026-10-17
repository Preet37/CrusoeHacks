"""Topic tracking and newest-wins behavior with offline model and job fakes."""

import asyncio
import json
import unittest
from unittest.mock import patch

from agents.conductor import Conductor
from agents.listener import Listener
from board import BoardState


class FakeSession:
    def __init__(self):
        self.board = BoardState()
        self.messages = []
        self.updates = []

    async def broadcast(self, message):
        self.messages.append(message)

    def push_update(self, text):
        self.updates.append(text)


def batch(topic_id, title, text, changed=True):
    return json.dumps({"topic_id": topic_id, "title": title,
                       "topic_changed": changed,
                       "moments": [{"type": "system_fact", "text": text,
                                    "entities": [title]}]})


def reflex(*, kind="system_fact", kind_confidence=0.95, topic_changed=0.0,
           wants_visual="none", visual_confidence=0.95, is_echo=0.0):
    return {
        "kind": {"type": "choice", "choice": kind, "confidence": kind_confidence},
        "topic_changed": {"type": "noul", "noul": topic_changed},
        "wants_visual": {"type": "choice", "choice": wants_visual,
                         "confidence": visual_confidence},
        "is_echo": {"type": "noul", "noul": is_echo},
    }


class TopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_conductor_routes_explicit_rich_requests_without_inventing_facts(self):
        session = FakeSession()

        class RecordingDiagrammer:
            def __init__(self):
                self.calls = []

            async def handle(self, session, moments, parent_id=None, **kw):
                self.calls.append((moments, kw.get("source_lines", [])))

        diagrammer = RecordingDiagrammer()
        conductor = Conductor(diagrammer)
        requests = [
            ("Our measured login p95 latency was 420 milliseconds in v1 and 180 milliseconds in v2. Draw a chart of these measurements.",
             "claim"),
            ("Our p95 latency went from 420 to 180 milliseconds after the cache.", "claim"),
            ("What would the new login screen look like?", "question"),
            ("Walk me through the login sequence", "question"),
            ("Mock up a login screen", "task"),
            ("Put the p95 comparison in a table", "task"),
        ]
        for text, kind in requests:
            with self.subTest(text=text):
                moment = {"type": kind, "text": text, "entities": []}
                await conductor.handle(session, [moment], topic_id="t_auth", title="Auth",
                                       source_lines=[{"line_id": "ln_1", "speaker": "Preet",
                                                      "text": text}])
                routed, lines = diagrammer.calls[-1]
                self.assertEqual(routed, [moment])
                self.assertEqual(lines[0]["text"], text)
                self.assertFalse(any(m["type"] == "system_fact" for m in routed))

        await conductor.handle(session, [{"type": "question", "text": "What time is it?"}],
                               topic_id="t_auth", title="Auth",
                               source_lines=[{"text": "What time is it?"}])
        self.assertEqual(len(diagrammer.calls), len(requests))

    async def test_m1_implicit_chart_and_screen_request_draw_real_board_shapes(self):
        session = FakeSession()
        latency = "Our p95 latency went from 420 to 180 milliseconds after the cache."
        screen = "What would the new login screen look like?"
        model_inputs = []

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            return json.dumps({"topic_id": "t_login", "title": "Login",
                               "topic_changed": line == latency,
                               "moments": [{"type": "claim" if line == latency else "question",
                                            "text": line, "entities": []}]})

        async def json_ops(model, messages, definitions, handlers, max_ops, **kw):
            payload = json.loads(messages[-1]["content"])
            model_inputs.append(payload)
            line = payload["source_lines"][0]["text"]
            if line == latency:
                await handlers["add_chart"](id="login_p95", chart="bar",
                                            title="Login p95 latency", labels=["Before cache", "After cache"],
                                            series=[{"name": "p95 (ms)", "values": [420, 180]}],
                                            x=100, y=150)
            elif line == screen:
                await handlers["add_mock"](id="login_screen", title="Login screen",
                                           html="<div><h1>Sign in</h1><input placeholder='Email'><button>Continue</button></div>",
                                           x=100, y=450)

        with patch("agents.listener.llm.chat", chat), \
                patch("agents.diagrammer.llm.run_json_ops", json_ops):
            listener = Listener(session, debounce_seconds=0)
            for line in (latency, screen):
                listener.submit("Preet", line)
                await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual([payload["source_lines"][0]["text"] for payload in model_inputs],
                         [latency, screen])
        self.assertEqual([payload["facts"][0]["type"] for payload in model_inputs],
                         ["claim", "question"])
        self.assertEqual(session.board.shapes["login_p95"]["series"][0]["values"], [420, 180])
        self.assertEqual(session.board.shapes["login_screen"]["op"], "add_mock")
        self.assertEqual([m["op"] for m in session.messages if m["type"] == "draw"],
                         ["frame", "focus", "add_chart", "add_mock"])

    async def test_one_latency_number_does_not_force_a_chart(self):
        session = FakeSession()
        calls = []

        class RecordingDiagrammer:
            async def handle(self, session, moments, parent_id=None, **kw):
                calls.append(moments)

        await Conductor(RecordingDiagrammer()).handle(
            session, [{"type": "claim", "text": "p95 latency was 420 milliseconds.",
                       "entities": []}],
            topic_id="t_login", title="Login",
            source_lines=[{"text": "p95 latency was 420 milliseconds."}])
        self.assertEqual(calls, [])

    async def test_visual_hint_routes_empty_extraction_but_plain_small_talk_does_not(self):
        session = FakeSession()
        calls = []

        class RecordingDiagrammer:
            async def handle(self, session, moments, parent_id=None, **kw):
                calls.append((moments, kw))

        conductor = Conductor(RecordingDiagrammer())
        await conductor.handle(session, [], topic_id="t_auth", title="Auth",
                               source_lines=[{"text": "Could you display this?",
                                              "wants_visual": "chart"}])
        self.assertEqual(calls[0][0][0]["type"], "task")
        self.assertFalse(any(m["type"] == "system_fact" for m in calls[0][0]))
        await conductor.handle(session, [], topic_id="t_auth", title="Auth",
                               source_lines=[{"text": "Okay, cool."}])
        self.assertEqual(len(calls), 1)

    async def test_visual_request_reaches_real_diagrammer_with_source_measurements(self):
        session = FakeSession()
        text = ("Our measured login p95 latency was 420 milliseconds in v1 and "
                "180 milliseconds in v2. Draw a chart of these measurements.")
        captured = []

        async def chat(*args, **kw):
            return json.dumps({"topic_id": "t_login", "title": "Login latency",
                               "topic_changed": True,
                               "moments": [{"type": "claim", "text": text,
                                            "entities": ["v1", "v2"]}]})

        async def json_ops(model, messages, definitions, handlers, max_ops, **kw):
            payload = json.loads(messages[-1]["content"])
            captured.append(payload)
            await handlers["add_chart"](id="login_latency", chart="bar",
                                        title="Login p95 latency", labels=["v1", "v2"],
                                        series=[{"name": "p95 (ms)", "values": [420, 180]}],
                                        x=150, y=200)

        with patch("agents.listener.llm.chat", chat), \
                patch("agents.diagrammer.llm.run_json_ops", json_ops):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", text)
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(captured[0]["facts"][0]["type"], "claim")
        self.assertEqual(captured[0]["source_lines"][0]["text"], text)
        self.assertEqual(session.board.shapes["login_latency"]["series"][0]["values"],
                         [420, 180])
        self.assertEqual([m["op"] for m in session.messages if m["type"] == "draw"],
                         ["frame", "focus", "add_chart"])

    async def test_unchanged_topic_visual_request_survives_empty_extraction(self):
        session = FakeSession()
        session.active_topic = {"topic_id": "t_auth", "title": "Auth"}
        session.topic_catalog = {"t_auth": "Auth"}
        session.board.apply({"op": "frame", "id": "t_auth", "title": "Auth",
                             "x": 0, "y": 0, "w": 1000, "h": 1000})
        session.board.apply({"op": "focus", "id": "t_auth"})
        captured = []

        async def chat(*args, **kw):
            return json.dumps({"topic_id": "t_auth", "title": "Auth",
                               "topic_changed": False, "moments": []})

        async def json_ops(model, messages, definitions, handlers, max_ops, **kw):
            captured.append(json.loads(messages[-1]["content"]))
            await handlers["add_mermaid"](id="login_flow",
                                           code="sequenceDiagram\n Browser->>Auth: login",
                                           x=100, y=150)

        with patch("agents.listener.llm.chat", chat), \
                patch("agents.diagrammer.llm.run_json_ops", json_ops):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "Walk me through the login sequence")
            await asyncio.wait_for(listener.wait_idle(), 1)
            listener.submit("Preet", "Okay, cool.")
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["facts"][0]["type"], "task")
        self.assertEqual(captured[0]["source_lines"][0]["text"],
                         "Walk me through the login sequence")
        self.assertEqual(session.board.shapes["login_flow"]["op"], "add_mermaid")

    async def test_reflex_skips_confident_small_talk_but_fails_open_on_uncertainty(self):
        session = FakeSession()
        seen = []

        async def chat(model, messages, **kw):
            seen.extend(line["text"] for line in json.loads(messages[-1]["content"])["new_lines"])
            return batch("t_auth", "Auth", "auth uses Redis")

        class NoopConductor:
            async def handle(self, *args, **kw):
                return None

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=NoopConductor(), debounce_seconds=0)
            listener.submit("Preet", "nice weather", decision=reflex(kind="small_talk"))
            self.assertIsNone(listener._worker)
            listener.submit("Preet", "auth uses Redis", decision=reflex(
                kind="small_talk", kind_confidence=0.79))
            await asyncio.wait_for(listener.wait_idle(), 1)
            listener.submit("Preet", "partial decision", decision={
                "kind": {"type": "choice", "choice": "small_talk", "confidence": 0.99},
            })
            await asyncio.wait_for(listener.wait_idle(), 1)
            malformed_topic = reflex(kind="small_talk")
            malformed_topic["topic_changed"]["noul"] = float("nan")
            listener.submit("Preet", "uncertain topic", decision=malformed_topic)
            await asyncio.wait_for(listener.wait_idle(), 1)
            uncertain_visual = reflex(kind="small_talk", visual_confidence=0.69)
            listener.submit("Preet", "uncertain visual", decision=uncertain_visual)
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(seen, ["auth uses Redis", "partial decision",
                                "uncertain topic", "uncertain visual"])

    async def test_reflex_visual_hint_overrides_small_talk_and_reaches_diagrammer(self):
        session = FakeSession()
        calls = []

        async def chat(*args, **kw):
            return batch("t_auth", "Auth", "show the auth sequence")

        class RecordingDiagrammer:
            async def handle(self, session, facts, parent_id=None, **kw):
                calls.append(kw)

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=Conductor(RecordingDiagrammer()),
                                debounce_seconds=0)
            listener.submit("Preet", "show the auth sequence", decision=reflex(
                kind="small_talk", wants_visual="sequence"))
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(calls[0]["source_lines"][0]["wants_visual"], "sequence")

    async def test_confident_reflex_topic_switch_cancels_job_before_nano_returns(self):
        session = FakeSession()
        old_job_started = asyncio.Event()
        old_job_cancelled = asyncio.Event()
        new_model_release = asyncio.Event()

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            if line == "billing starts":
                await new_model_release.wait()
                return batch("t_billing", "Billing", line)
            return batch("t_auth", "Auth", line)

        class SlowConductor:
            async def handle(self, session, moments, parent_id=None, **kw):
                if kw["topic_id"] == "t_auth":
                    old_job_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        old_job_cancelled.set()
                        raise

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=SlowConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth exists")
            await asyncio.wait_for(old_job_started.wait(), 1)
            listener.submit("Preet", "billing starts", decision=reflex(topic_changed=0.7))
            await asyncio.wait_for(old_job_cancelled.wait(), 1)
            self.assertEqual(session.active_topic["topic_id"], "t_auth")
            new_model_release.set()
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(session.active_topic["topic_id"], "t_billing")

    async def test_weak_reflex_topic_switch_keeps_current_job_running(self):
        session = FakeSession()
        old_job_started = asyncio.Event()
        release_old_job = asyncio.Event()
        second_model_started = asyncio.Event()
        release_second_model = asyncio.Event()
        cancelled = False

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            if line == "auth also uses Redis":
                second_model_started.set()
                await release_second_model.wait()
            return batch("t_auth", "Auth", line)

        class SlowConductor:
            async def handle(self, session, moments, parent_id=None, **kw):
                nonlocal cancelled
                if moments[0]["text"] == "auth uses Postgres":
                    old_job_started.set()
                    try:
                        await release_old_job.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                        raise

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=SlowConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth uses Postgres")
            await asyncio.wait_for(old_job_started.wait(), 1)
            listener.submit("Preet", "auth also uses Redis", decision=reflex(
                topic_changed=0.69))
            await asyncio.wait_for(second_model_started.wait(), 1)
            self.assertFalse(cancelled)
            release_second_model.set()
            release_old_job.set()
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertFalse(cancelled)

    async def test_reflex_echo_requires_lexical_agreement_and_invalid_scores_fail_open(self):
        class EchoSession(FakeSession):
            def is_echo(self, text):
                return text == "Twin said this"

        session = EchoSession()
        seen = []

        async def chat(model, messages, **kw):
            seen.extend(line["text"] for line in json.loads(messages[-1]["content"])["new_lines"])
            return batch("t_auth", "Auth", "human says auth")

        class NoopConductor:
            async def handle(self, *args, **kw):
                return None

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=NoopConductor(), debounce_seconds=0)
            listener.submit("Preet", "Twin said this", decision=reflex(is_echo=0.9))
            self.assertIsNone(listener._worker)
            listener.submit("Preet", "human says auth", decision=reflex(is_echo=0.9))
            await asyncio.wait_for(listener.wait_idle(), 1)
            listener.submit("Preet", "invalid score", decision=reflex(
                kind="small_talk", kind_confidence=float("nan")))
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(seen, ["human says auth", "invalid score"])

    async def test_session_set_topic_owns_publication_without_duplicate_broadcast(self):
        class SessionWithTopic(FakeSession):
            def __init__(self):
                super().__init__()
                self.topic_calls = []

            async def set_topic(self, topic_id, title):
                self.topic_calls.append((topic_id, title, getattr(self, "active_topic", None)))
                changed = getattr(self, "active_topic", None) != {
                    "topic_id": topic_id, "title": title,
                }
                self.active_topic = {"topic_id": topic_id, "title": title}
                await self.broadcast({"type": "topic", "topic_id": topic_id,
                                      "title": title, "active": True,
                                      "changed": changed})

        class NoopConductor:
            async def handle(self, *args, **kw):
                return None

        async def chat(*args, **kw):
            return batch("t_auth", "Auth", "auth uses Redis")

        session = SessionWithTopic()
        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=NoopConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth uses Redis")
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(session.topic_calls, [("t_auth", "Auth", None)])
        self.assertEqual([m for m in session.messages if m["type"] == "topic"], [{
            "type": "topic", "topic_id": "t_auth", "title": "Auth",
            "active": True, "changed": True,
        }])

    async def test_switch_and_return_reuses_topic_id_and_tags_jobs(self):
        session = FakeSession()
        responses = iter([
            batch("t_auth", "Auth system", "web uses auth"),
            batch("t_billing", "Billing", "billing uses invoices"),
            batch("t_auth_new", "Auth system", "auth uses Redis"),
        ])
        prompts = []

        async def chat(model, messages, **kw):
            prompts.append(json.loads(messages[-1]["content"]))
            return next(responses)

        class RecordingConductor:
            def __init__(self):
                self.calls = []

            async def handle(self, session, moments, parent_id=None, **kw):
                self.calls.append({"moments": moments, "parent_id": parent_id, **kw})

        conductor = RecordingConductor()
        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=conductor, debounce_seconds=0)
            for line in ("web uses auth", "billing uses invoices", "auth uses Redis"):
                listener.submit("Preet", line)
                await asyncio.wait_for(listener.wait_idle(), 1)

        topics = [m for m in session.messages if m["type"] == "topic"]
        self.assertEqual([m["topic_id"] for m in topics], ["t_auth", "t_billing", "t_auth"])
        self.assertEqual([m["changed"] for m in topics], [True, True, True])
        self.assertEqual(session.active_topic, {"topic_id": "t_auth", "title": "Auth system"})
        self.assertEqual([c["topic_id"] for c in conductor.calls],
                         ["t_auth", "t_billing", "t_auth"])
        self.assertEqual(len({c["source_lines"][0]["line_id"] for c in conductor.calls}), 3)
        self.assertTrue(all(c["parent_id"].startswith("ev_") for c in conductor.calls))
        self.assertIn({"topic_id": "t_auth", "title": "Auth system"}, prompts[1]["known_topics"])
        self.assertIn({"topic_id": "t_billing", "title": "Billing"}, prompts[2]["known_topics"])

    async def test_topic_switch_cancels_old_job_without_blocking_new_listener_call(self):
        session = FakeSession()
        first_job_started = asyncio.Event()
        old_cancelled = asyncio.Event()
        seen_lines = []

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            seen_lines.append(line)
            if line == "auth details":
                return batch("t_auth", "Auth", line)
            return batch("t_billing", "Billing", line)

        class SlowConductor:
            async def handle(self, session, moments, parent_id=None, **kw):
                if kw["topic_id"] == "t_auth":
                    first_job_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        old_cancelled.set()
                        raise

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=SlowConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth details")
            await asyncio.wait_for(first_job_started.wait(), 1)
            listener.submit("Preet", "billing details")
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(seen_lines, ["auth details", "billing details"])
        self.assertTrue(old_cancelled.is_set())
        self.assertEqual(session.active_topic["topic_id"], "t_billing")
        events = [m for m in session.messages if m["type"] == "agent_event"
                  and m["agent"] == "Listener"]
        self.assertEqual(sorted(m["state"] for m in events),
                         ["cancelled", "done", "started", "started"])

    async def test_conflicting_reused_topic_id_gets_new_valid_slug(self):
        session = FakeSession()
        replies = iter([
            json.dumps({"topic_id": "t_auth", "title": "Auth", "topic_changed": True,
                        "moments": []}),
            json.dumps({"topic_id": "t_auth", "title": "Billing", "topic_changed": True,
                        "moments": []}),
        ])

        async def chat(*args, **kw):
            return next(replies)

        class NoopConductor:
            async def handle(self, *args, **kw):
                return None

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=NoopConductor(), debounce_seconds=0)
            for line in ("auth", "billing"):
                listener.submit("Preet", line)
                await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual([m["topic_id"] for m in session.messages if m["type"] == "topic"],
                         ["t_auth", "t_billing"])

    async def test_explicit_correction_cancels_same_topic_before_model_reply(self):
        session = FakeSession()
        first_job_started = asyncio.Event()
        cancelled = asyncio.Event()
        second_model_can_reply = asyncio.Event()

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            if line.startswith("Actually"):
                await second_model_can_reply.wait()
            return batch("t_auth", "Auth", line, changed=False)

        class SlowConductor:
            async def handle(self, session, moments, parent_id=None, **kw):
                if moments[0]["text"] == "auth uses Postgres":
                    first_job_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=SlowConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth uses Postgres")
            await asyncio.wait_for(first_job_started.wait(), 1)
            listener.submit("Preet", "Actually, auth uses Redis instead")
            await asyncio.wait_for(cancelled.wait(), 1)
            second_model_can_reply.set()
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(session.active_topic["topic_id"], "t_auth")
        self.assertEqual([m["changed"] for m in session.messages if m["type"] == "topic"],
                         [True, False])

    async def test_additive_same_topic_line_is_parsed_while_prior_draw_job_runs(self):
        session = FakeSession()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_parsed = asyncio.Event()
        cancelled = False

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            if line == "auth also uses Redis":
                second_parsed.set()
            return batch("t_auth", "Auth", line)

        class SlowConductor:
            async def handle(self, session, moments, parent_id=None, **kw):
                nonlocal cancelled
                if moments[0]["text"] == "auth uses Postgres":
                    first_started.set()
                    try:
                        await release_first.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                        raise

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=SlowConductor(), debounce_seconds=0)
            listener.submit("Preet", "auth uses Postgres")
            await asyncio.wait_for(first_started.wait(), 1)
            listener.submit("Preet", "auth also uses Redis")
            await asyncio.wait_for(second_parsed.wait(), 1)
            self.assertFalse(cancelled)
            release_first.set()
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertFalse(cancelled)
        self.assertEqual([m["changed"] for m in session.messages if m["type"] == "topic"],
                         [True, False])

    async def test_conductor_passes_topic_and_source_line_to_diagrammer(self):
        session = FakeSession()

        class RecordingDiagrammer:
            def __init__(self):
                self.calls = []

            async def handle(self, session, facts, parent_id=None, **kw):
                self.calls.append((facts, parent_id, kw))

        diagrammer = RecordingDiagrammer()
        await Conductor(diagrammer).handle(session,
                                           [{"type": "system_fact", "text": "auth uses Redis"}],
                                           parent_id="ev_listener", topic_id="t_auth",
                                           title="Auth", topic_changed=True,
                                           source_lines=[{"line_id": "ln_1", "speaker": "Preet",
                                                          "text": "auth uses Redis"}])
        facts, event_id, metadata = diagrammer.calls[0]
        self.assertEqual(facts[0]["text"], "auth uses Redis")
        self.assertEqual(metadata["topic_id"], "t_auth")
        self.assertTrue(metadata["topic_changed"])
        self.assertEqual(metadata["source_lines"][0]["line_id"], "ln_1")
        started = next(m for m in session.messages if m["type"] == "agent_event"
                       and m["state"] == "started")
        self.assertEqual(event_id, started["event_id"])
        self.assertEqual(started["parent_id"], "ev_listener")
        self.assertIn("topic=t_auth", started["detail"])

    async def test_topic_only_switch_still_reaches_diagrammer_for_focus(self):
        session = FakeSession()
        calls = []

        async def chat(*args, **kw):
            return json.dumps({"topic_id": "t_billing", "title": "Billing",
                               "topic_changed": True, "moments": []})

        class RecordingDiagrammer:
            async def handle(self, session, facts, parent_id=None, **kw):
                calls.append((facts, kw))

        with patch("agents.listener.llm.chat", chat):
            listener = Listener(session, conductor=Conductor(RecordingDiagrammer()),
                                debounce_seconds=0)
            listener.submit("Preet", "let's talk billing")
            await asyncio.wait_for(listener.wait_idle(), 1)
        self.assertEqual(calls[0][0], [])
        self.assertEqual(calls[0][1]["topic_id"], "t_billing")
        self.assertTrue(calls[0][1]["topic_changed"])

    async def test_real_topic_pipeline_creates_frames_and_focuses_returned_topic(self):
        session = FakeSession()
        responses = iter([
            batch("t_auth", "Auth", "auth exists"),
            batch("t_billing", "Billing", "billing exists"),
            json.dumps({"topic_id": "t_auth", "title": "Auth", "topic_changed": True,
                        "moments": []}),
        ])

        async def chat(*args, **kw):
            return next(responses)

        async def json_ops(model, messages, definitions, handlers, max_ops, **kw):
            payload = json.loads(messages[-1]["content"])
            facts = payload["facts"]
            if facts:
                item = "auth" if payload["topic_id"] == "t_auth" else "billing"
                await handlers["add_box"](id=item, label=item.title(), x=150, y=200,
                                          kind="service")

        with patch("agents.listener.llm.chat", chat), \
                patch("agents.diagrammer.llm.run_json_ops", json_ops):
            listener = Listener(session, debounce_seconds=0)
            for line in ("auth exists", "billing exists", "back to auth"):
                listener.submit("Preet", line)
                await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(set(session.board.shapes), {"t_auth", "t_billing"})
        self.assertEqual(set(session.board.boxes), {"auth", "billing"})
        self.assertEqual(session.board.boxes["auth"]["frame"], "t_auth")
        self.assertEqual(session.board.boxes["billing"]["frame"], "t_billing")
        self.assertEqual(session.board.active_frame, "t_auth")
        self.assertEqual([m["op"] for m in session.messages if m["type"] == "draw"],
                         ["frame", "focus", "add_box", "frame", "focus", "add_box", "focus"])
        self.assertEqual(len(session.updates), 2)
        started = [m for m in session.messages if m["type"] == "agent_event"
                   and m["state"] == "started"]
        self.assertEqual(len(started), 9)
        for listener_event in [m for m in started if m["agent"] == "Listener"]:
            conductor = next(m for m in started if m["parent_id"] == listener_event["event_id"])
            diagrammer = next(m for m in started if m["parent_id"] == conductor["event_id"])
            self.assertEqual(diagrammer["agent"], "Diagrammer")

    async def test_real_diagram_job_cancels_before_old_topic_ops_apply(self):
        session = FakeSession()
        old_model_started = asyncio.Event()

        async def chat(model, messages, **kw):
            line = json.loads(messages[-1]["content"])["new_lines"][0]["text"]
            if line == "auth exists":
                return batch("t_auth", "Auth", line)
            return batch("t_billing", "Billing", line)

        async def json_ops(model, messages, definitions, handlers, max_ops, **kw):
            payload = json.loads(messages[-1]["content"])
            if payload["topic_id"] == "t_auth":
                old_model_started.set()
                await asyncio.Event().wait()
            await handlers["add_box"](id="billing", label="Billing", x=150, y=200,
                                      kind="service")

        with patch("agents.listener.llm.chat", chat), \
                patch("agents.diagrammer.llm.run_json_ops", json_ops):
            listener = Listener(session, debounce_seconds=0)
            listener.submit("Preet", "auth exists")
            await asyncio.wait_for(old_model_started.wait(), 1)
            listener.submit("Preet", "billing exists")
            await asyncio.wait_for(listener.wait_idle(), 1)

        self.assertEqual(set(session.board.boxes), {"billing"})
        self.assertEqual(session.board.active_frame, "t_billing")
        self.assertFalse(any(m.get("id") == "auth" for m in session.messages
                             if m["type"] == "draw"))
        cancelled = [m for m in session.messages if m["type"] == "agent_event"
                     and m["state"] == "cancelled"]
        self.assertEqual({m["agent"] for m in cancelled},
                         {"Listener", "Conductor", "Diagrammer"})


class ExplicitSourceRescueTests(unittest.IsolatedAsyncioTestCase):
    async def test_topic_switch_and_return_survive_empty_legacy_reply(self):
        session = FakeSession()
        class ConductorSpy:
            def __init__(self): self.calls = []
            async def handle(self, *args, **kwargs): self.calls.append(kwargs)
        conductor = ConductorSpy()
        listener = Listener(session, conductor, debounce_seconds=0)
        async def empty(*args, **kwargs): return '[]'
        with patch("agents.listener.llm.chat", empty):
            listener.submit("Preet", "OK, different topic: let us talk about billing.")
            await listener.wait_idle()
            self.assertEqual(session.active_topic["topic_id"], "t_billing")
            listener.submit("Preet", "Switching to auth.")
            await listener.wait_idle()
            listener.submit("Preet", "Back to billing.")
            await listener.wait_idle()
        self.assertEqual([c["topic_id"] for c in conductor.calls], ["t_billing", "t_auth", "t_billing"])
        await listener.close()

    async def test_explicit_decision_survives_zero_moments(self):
        session = FakeSession()
        recorded = []
        class ConductorSpy:
            async def handle(self, session, moments, **kwargs): recorded.extend(moments)
        listener = Listener(session, ConductorSpy(), debounce_seconds=0)
        async def empty(*args, **kwargs): return '[]'
        text = "We decided to migrate login to Redis sessions; Avery will own it."
        with patch("agents.listener.llm.chat", empty):
            listener.submit("Preet", text)
            await listener.wait_idle()
        self.assertEqual(recorded, [{"type": "decision", "text": text, "entities": []}])
        await listener.close()
