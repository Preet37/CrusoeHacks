"""Every ```json example in PROTOCOL.md must round-trip through protocol.parse/dump."""
import json
import re
from pathlib import Path

import pytest

import protocol
from protocol import Audio, Draw, Status, StopAudio

PROTOCOL_MD = Path(__file__).resolve().parents[2] / "PROTOCOL.md"


def _examples() -> list[dict]:
    text = PROTOCOL_MD.read_text()
    out = []
    for block in re.findall(r"```json\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            line = re.sub(r"\s*//.*$", "", line).strip()
            if line:
                out.append(json.loads(line))
    return out


EXAMPLES = _examples()


def test_found_examples_for_every_type():
    types = {e["type"] for e in EXAMPLES}
    assert types == {m.model_fields["type"].default for m in protocol.ALL_MESSAGES}
    assert len(EXAMPLES) >= 20


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: f"{e['type']}:{e.get('op', e.get('state', ''))}")
def test_round_trip(example):
    model = protocol.parse(json.dumps(example))
    assert model.type == example["type"]
    assert protocol.dump(model) == example
    # dict and bytes inputs work too
    assert protocol.dump(protocol.parse(example)) == example
    assert protocol.dump(protocol.parse(json.dumps(example).encode())) == example


def test_type_defaults_and_none_handling():
    assert protocol.dump(Status(state="speaking")) == {"type": "status", "state": "speaking"}
    assert protocol.dump(StopAudio()) == {"type": "stop_audio"}
    a = protocol.dump(Audio(say_id="s_1", seq=0, text="hi", last=True))
    assert a["data"] is None and "sample_rate" not in a
    assert protocol.dump(Draw(op="clear")) == {"type": "draw", "op": "clear"}
    assert protocol.PROTOCOL_VERSION == 1
    assert isinstance(protocol.now_ms(), int) and protocol.now_ms() > 1_700_000_000_000


@pytest.mark.parametrize("raw", [
    "not json",
    "[1,2]",
    '{"text":"no type"}',
    '{"type":"nope"}',
    '{"type":"status","state":"dancing"}',
    '{"type":"control","action":"explode"}',
    '{"type":"user_text","text":"missing speaker"}',
    '{"type":"draw","op":"add_box","id":"a","kind":"spaceship"}',
    '{"type":"agent_event","event_id":"e","agent":"x","state":"weird"}',
])
def test_invalid_messages_raise_value_error(raw):
    with pytest.raises(ValueError):
        protocol.parse(raw)


def test_dump_passes_dicts_through():
    d = {"type": "status", "state": "thinking"}
    assert protocol.dump(d) is d
