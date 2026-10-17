"""Memory lineage and sourced retrieval with fake, local-only project facts."""

import json

import pytest

import config
from agents.librarian import Librarian
from brain.memory import MemoryStore


def test_sourced_supersession_survives_reload_and_keeps_chain(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path)
    old = store.add_fact("Auth stores sessions in Postgres", "meeting/1:ln_1",
                         scope="demo", subject="Auth sessions")
    current = store.supersede(old["id"], "Auth stores sessions in Redis",
                              "The team moved the session store from Postgres to Redis",
                              source="meeting/2:ln_3")
    third = store.supersede(current["id"], "Auth stores encrypted sessions in Redis",
                            "The new decision adds encryption to the Redis session store",
                            source="meeting/3:ln_7")
    reloaded = MemoryStore(path)
    chain = reloaded.chain(old["id"])
    assert [fact["id"] for fact in chain] == [old["id"], current["id"], third["id"]]
    assert [fact["status"] for fact in chain] == ["superseded", "superseded", "current"]
    assert reloaded.search("Auth sessions", scope="demo")[0]["id"] == third["id"]
    assert len(reloaded.events()) == 5
    assert all(fact["source"].startswith("meeting/") for fact in chain)


def test_rejects_unsourced_or_arbitrary_supersession_without_mutation(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    old = store.add_fact("Auth uses JWT", "repo/auth.py:12", subject="Auth")
    other = store.add_fact("Billing uses Stripe", "repo/billing.py:9", subject="Billing")
    with pytest.raises(ValueError):
        store.supersede(old["id"], "Auth uses Redis", "new decision")
    with pytest.raises(ValueError):
        store.supersede("fact_madeup", other, "model guessed")
    with pytest.raises(ValueError):
        store.supersede(old["id"], other, "different subject")
    with pytest.raises(ValueError):
        store.supersede(old["id"], "Auth uses Redis", "different subject",
                        source="meeting/2", subject="Billing")
    assert len(store.facts()) == 2
    assert store.get(old["id"])["status"] == "current"


def test_multiple_store_instances_merge_writes_and_failed_save_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    first, second = MemoryStore(path), MemoryStore(path)
    a = first.add_fact("Auth uses JWT", "repo/auth.py:12")
    b = second.add_fact("Billing uses Stripe", "repo/billing.py:9")
    assert {fact["id"] for fact in first.facts()} == {a["id"], b["id"]}

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(first, "_save", fail_save)
    with pytest.raises(OSError):
        first.add_fact("Auth uses Redis", "meeting/2:ln_3")
    assert {fact["id"] for fact in first.facts()} == {a["id"], b["id"]}
    assert len(MemoryStore(path).events()) == 2


@pytest.mark.asyncio
async def test_two_pass_retrieval_limits_super_context_and_citations(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.json")
    auth = store.add_fact("Auth stores sessions in Redis", "meeting/2:ln_3", scope="demo")
    store.add_fact("Billing charges cards through Stripe", "meeting/1:ln_9", scope="demo")
    calls = []

    async def fake_chat(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        if model == config.MODEL_FAST:
            return json.dumps({"ids": [auth["id"]]})
        assert model == config.MODEL_SMART
        supplied = json.loads(messages[-1]["content"])["facts"]
        assert [fact["id"] for fact in supplied] == [auth["id"]]
        return json.dumps({"answer": "Auth stores sessions in Redis.",
                           "citation_ids": [auth["id"]]})

    monkeypatch.setattr("agents.librarian.llm.chat", fake_chat)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = await Librarian(store).retrieve("Where are auth sessions stored?", scope="demo")
    assert result["citations"] == [{"id": auth["id"], "claim": auth["claim"],
                                    "source": auth["source"]}]
    assert [model for model, _, _ in calls] == [config.MODEL_FAST, config.MODEL_SMART]


@pytest.mark.asyncio
async def test_invalid_model_citation_falls_back_to_sourced_claim(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.json")
    fact = store.add_fact("Auth uses Redis", "meeting/2:ln_3")

    async def fake_chat(model, messages, **kwargs):
        return json.dumps({"answer": "Auth uses Cassandra", "citation_ids": ["fact_fabricated"]})

    monkeypatch.setattr("agents.librarian.llm.chat", fake_chat)
    result = await Librarian(store).retrieve("Auth?", first_pass=lambda q, facts, n: [fact["id"]])
    assert result["citations"][0]["id"] == fact["id"]
    assert "Auth uses Redis" in result["answer"]
    assert "Cassandra" not in result["answer"]


@pytest.mark.asyncio
async def test_malformed_first_pass_and_answer_still_return_only_stored_sources(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.json")
    fact = store.add_fact("Auth uses Redis", "meeting/2:ln_3")

    async def malformed_chat(model, messages, **kwargs):
        if model == config.MODEL_FAST:
            return "not JSON"
        return '{"answer":"Unsupported assertion","citation_ids":[]}'

    monkeypatch.setattr("agents.librarian.llm.chat", malformed_chat)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = await Librarian(store).retrieve("Auth sessions")
    assert result["citations"][0]["id"] == fact["id"]
    assert "Auth uses Redis" in result["answer"]
    assert "Unsupported assertion" not in result["answer"]


@pytest.mark.asyncio
async def test_remember_accepts_only_candidate_id_for_supersession(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.json")
    old = store.add_fact("Auth sessions are in Postgres", "meeting/1:ln_1",
                         subject="Auth sessions")

    async def wrong_id(model, messages, **kwargs):
        return json.dumps({"old_id": "fact_fabricated", "reason": "Postgres to Redis"})

    monkeypatch.setattr("agents.librarian.llm.chat", wrong_id)
    new = await Librarian(store).remember("Auth sessions are in Redis", "meeting/2:ln_3",
                                          subject="Auth sessions")
    assert store.get(old["id"])["status"] == "current"
    assert store.get(new["id"])["source"] == "meeting/2:ln_3"

    async def valid_id(model, messages, **kwargs):
        return json.dumps({"old_id": old["id"], "reason": "The newer meeting moved sessions to Redis"})

    monkeypatch.setattr("agents.librarian.llm.chat", valid_id)
    newer = await Librarian(store).remember("Auth sessions moved to Redis", "meeting/3:ln_2",
                                            subject="Auth sessions")
    assert store.get(old["id"])["superseded_by"] == newer["id"]
