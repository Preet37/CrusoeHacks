"""Tests for server/turn_taking.py (one or more per rule)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import turn_taking as tt
from turn_taking import Decision, TurnState, check_idle, is_not_now, mark_interjection, mentions_name, on_final_text


@pytest.fixture(autouse=True)
def fresh_rules(monkeypatch, tmp_path):
    monkeypatch.delenv("TWIN_NAME", raising=False)
    monkeypatch.setattr(tt, "PLAYBOOK_VALUES_PATH", tmp_path / "missing.json")
    tt.reload()
    yield
    monkeypatch.setattr(tt, "PLAYBOOK_VALUES_PATH", tmp_path / "missing.json")
    tt.reload()


# ---- name mentions

@pytest.mark.parametrize("text", [
    "Twin, what do you think?",
    "what about you twin?",
    "TWIN can you draw that",
    "hey twin.",
    "Twin's take?",
    "twins, any thoughts",
    "tween what do you think",
])
def test_mentions_name_variants(text):
    assert mentions_name(text)


@pytest.mark.parametrize("text", [
    "the call between auth and db",
    "twinkle twinkle little star",
    "",
    "let's add redis",
])
def test_mentions_name_non_matches(text):
    assert not mentions_name(text)


def test_twin_name_env_alias(monkeypatch):
    monkeypatch.setenv("TWIN_NAME", "Echo")
    tt.reload()
    assert "echo" in tt.RULES["name_aliases"]
    assert mentions_name("Echo, thoughts?")
    assert mentions_name("twin?")


# ---- not now

@pytest.mark.parametrize("text", [
    "not now", "Hold on a second", "wait", "Wait, that's wrong", "one sec",
    "give me a sec please", "hang on", "stop", "Twin, wait", "hey, stop", "ok NOT NOW twin",
])
def test_not_now_phrases(text):
    assert is_not_now(text)


@pytest.mark.parametrize("text", [
    "I can't wait to ship this",
    "we should stop the deploy job",
    "the waitlist service",
    "let's add a queue",
])
def test_not_now_false_positives(text):
    assert not is_not_now(text)


def test_not_now_sets_hold():
    s = TurnState()
    d = on_final_text(s, "hold on", 10_000)
    assert d == Decision("hold", "not_now")
    assert s.hold_until_ms == 40_000


# ---- on_final_text

def test_name_responds():
    assert on_final_text(TurnState(), "Twin, does that make sense?", 1000).action == "respond"


def test_unaddressed_is_none():
    assert on_final_text(TurnState(), "auth talks to postgres", 1000).action == "none"


def test_push_to_talk_responds_without_name():
    s = TurnState(push_to_talk_active=True)
    assert on_final_text(s, "what's the plan", 1000) == Decision("respond", "push_to_talk")


def test_muted_blocks_everything():
    s = TurnState(muted=True, push_to_talk_active=True)
    assert on_final_text(s, "Twin, hello?", 1000).action == "none"
    assert on_final_text(s, "not now", 1000).action == "none"
    assert check_idle(s, 100_000, True).action == "none"


def test_addressed_during_hold_responds_and_clears_hold():
    s = TurnState()
    on_final_text(s, "not now", 0)
    assert s.hold_until_ms > 0
    d = on_final_text(s, "ok twin, go ahead", 5_000)
    assert d.action == "respond"
    assert s.hold_until_ms == 0


def test_respond_flag_off_ignores_name():
    tt.RULES["respond_to_questions_addressed"] = False
    assert on_final_text(TurnState(), "twin?", 1000).action == "none"


def test_final_text_counts_as_human_speech():
    s = TurnState()
    on_final_text(s, "random chatter", 7_000)
    assert s.last_human_speech_ms == 7_000


# ---- check_idle

def test_no_updates_is_none():
    assert check_idle(TurnState(), 100_000, False).action == "none"


def test_hold_window_blocks_then_expires():
    s = TurnState()
    on_final_text(s, "hang on", 0)
    d = check_idle(s, 10_000, True)
    assert d == Decision("hold", "not_now")
    assert check_idle(s, 29_999, True).action == "hold"
    assert check_idle(s, 30_000, True).action == "deliver_update"


def test_silence_gating():
    s = TurnState()
    tt.note_human_speech(s, 10_000)
    assert check_idle(s, 11_000, True) == Decision("none", "waiting_for_silence")
    assert check_idle(s, 11_199, True).action == "none"
    assert check_idle(s, 11_200, True).action == "deliver_update"


def test_interjection_cooldown():
    s = TurnState()
    mark_interjection(s, 10_000)
    assert check_idle(s, 20_000, True) == Decision("none", "cooldown")
    assert check_idle(s, 39_999, True).action == "none"
    assert check_idle(s, 40_000, True).action == "deliver_update"


def test_first_interjection_not_blocked_by_cooldown():
    assert check_idle(TurnState(), 5_000, True).action == "deliver_update"


def test_twin_speaking_blocks_updates():
    s = TurnState(twin_speaking=True)
    assert check_idle(s, 100_000, True) == Decision("none", "twin_speaking")
    s.twin_speaking = False
    assert check_idle(s, 100_000, True).action == "deliver_update"


def test_stale_updates():
    s = TurnState()
    assert check_idle(s, 200_000, True, oldest_update_age_ms=90_001) == Decision("none", "stale")
    assert check_idle(s, 200_000, True, oldest_update_age_ms=90_000).action == "deliver_update"


# ---- playbook overrides

def test_playbook_values_override(monkeypatch, tmp_path):
    p = tmp_path / "playbook_values.json"
    p.write_text(json.dumps({
        "silence_before_update_s": 2.5,
        "interjection_cooldown_s": 5,
        "not_now_phrases": ["shush"],
        "not_now_start_only": [],
    }))
    monkeypatch.setattr(tt, "PLAYBOOK_VALUES_PATH", p)
    tt.reload()
    assert tt.RULES["silence_before_update_s"] == 2.5
    assert tt.RULES["not_now_hold_s"] == 30  # untouched default kept
    assert is_not_now("ok shush")
    assert not is_not_now("hold on")
    s = TurnState()
    tt.note_human_speech(s, 0)
    assert check_idle(s, 2_000, True).action == "none"
    assert check_idle(s, 2_500, True).action == "deliver_update"
    mark_interjection(s, 2_500)
    assert check_idle(s, 7_500, True).action == "deliver_update"


def test_broken_playbook_falls_back_to_defaults(monkeypatch, tmp_path):
    p = tmp_path / "playbook_values.json"
    p.write_text("{not json")
    monkeypatch.setattr(tt, "PLAYBOOK_VALUES_PATH", p)
    tt.reload()
    assert tt.RULES["silence_before_update_s"] == 1.2


def test_rules_dict_identity_stable():
    ref = tt.RULES
    tt.reload()
    assert ref is tt.RULES


def test_urgent_update_skips_cooldown_but_waits_for_silence():
    st = TurnState(last_human_speech_ms=0, last_interjection_ms=10_000)
    assert check_idle(st, 12_000, True).action == "none"  # cooldown
    assert check_idle(st, 12_000, True, urgent=True).action == "deliver_update"
    st.last_human_speech_ms = 11_500
    assert check_idle(st, 12_000, True, urgent=True).action == "none"  # still needs a pause
