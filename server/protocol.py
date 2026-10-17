"""Pydantic v2 mirror of PROTOCOL.md (v1 + v2 additions). Every WebSocket message has a `type` field.

Usage:
    msg = protocol.parse(raw_text)          # -> one of the models below, ValueError if bad
    await protocol.send(ws, Status(state="speaking"))
    protocol.dump(model)                    # -> plain dict ready for json.dumps

Optional fields are dropped from the wire when None, except Audio.data, which is
always present (null means "audio failed, speak `text` with speechSynthesis").
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 2  # PROTOCOL.md says 2.1 (adds add_3d); kept int 2 for hello.protocol compat


def now_ms() -> int:
    """Milliseconds since epoch, the unit used for every `ts`."""
    return int(time.time() * 1000)


class Msg(BaseModel):
    """Base for all protocol messages."""

    model_config = ConfigDict(extra="ignore")
    # Field names that must be serialized even when None.
    keep_none: ClassVar[frozenset[str]] = frozenset()


# ---------------------------------------------------------------- browser -> server

class Hello(Msg):
    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    user: str
    room: str = "demo"


class UserText(Msg):
    type: Literal["user_text"] = "user_text"
    source: Optional[Literal["speech", "typed"]] = None
    speaker: str
    text: str
    final: bool = False
    ts: Optional[int] = None


class BargeIn(Msg):
    type: Literal["barge_in"] = "barge_in"
    speaker: Optional[str] = None
    ts: Optional[int] = None


class PushToTalk(Msg):
    type: Literal["push_to_talk"] = "push_to_talk"
    active: bool
    ts: Optional[int] = None


ControlAction = Literal["mute_twin", "unmute_twin", "clear_board", "reset_session", "set_mode", "mic_off", "mic_on"]


class Control(Msg):
    type: Literal["control"] = "control"
    action: ControlAction
    mode: Optional[Literal["off", "on_request", "proactive"]] = None  # with action=set_mode


class DrawError(Msg):
    """v2: a shape failed to render in the browser."""
    type: Literal["draw_error"] = "draw_error"
    id: Optional[str] = None
    op: Optional[str] = None
    error: str = ""


class CalendarJoin(Msg):
    """v2: a scheduled meeting started; Twin joins, sends `brief`, says hi."""
    type: Literal["calendar_join"] = "calendar_join"
    meeting: str
    scheduled: Optional[str] = None


class ConnectRepo(Msg):
    """v2: build the code map from a public GitHub repo."""
    type: Literal["connect_repo"] = "connect_repo"
    url: str


# ---------------------------------------------------------------- server -> browser

StatusState = Literal["listening", "thinking", "speaking", "hand_raised"]


class Status(Msg):
    type: Literal["status"] = "status"
    state: StatusState


class Transcript(Msg):
    type: Literal["transcript"] = "transcript"
    speaker: str
    text: str
    ts: Optional[int] = None


class Audio(Msg):
    type: Literal["audio"] = "audio"
    keep_none: ClassVar[frozenset[str]] = frozenset({"data"})
    say_id: str
    seq: int
    format: str = "wav"
    sample_rate: Optional[int] = None
    text: str
    data: Optional[str] = None  # base64 WAV, or None -> browser falls back to speechSynthesis
    last: bool = False


class StopAudio(Msg):
    type: Literal["stop_audio"] = "stop_audio"
    say_id: Optional[str] = None  # None = stop everything


DrawOp = Literal[
    "add_box", "connect", "rename", "remove", "highlight", "clear",
    # v2
    "frame", "focus", "add_sticky", "add_text", "add_mermaid", "add_chart", "add_table",
    "add_code", "add_mock", "sketch", "update",
    # v2.1
    "add_3d",
]
BoxKind = Literal["service", "datastore", "client", "external", "note"]


class Draw(Msg):
    # Permissive: unknown extra fields are kept and forwarded to the browser.
    model_config = ConfigDict(extra="allow")
    type: Literal["draw"] = "draw"
    op: DrawOp
    id: Optional[str] = None
    label: Optional[str] = None
    x: Optional[Union[int, float]] = None
    y: Optional[Union[int, float]] = None
    kind: Optional[BoxKind] = None
    from_id: Optional[str] = None
    to_id: Optional[str] = None
    ids: Optional[list[str]] = None
    on: Optional[bool] = None
    # v2
    frame: Optional[str] = None
    title: Optional[str] = None
    w: Optional[Union[int, float]] = None
    h: Optional[Union[int, float]] = None
    text: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    code: Optional[str] = None
    chart: Optional[str] = None
    labels: Optional[list[Any]] = None
    series: Optional[list[Any]] = None
    columns: Optional[list[Any]] = None
    rows: Optional[list[Any]] = None
    language: Optional[str] = None
    html: Optional[str] = None
    strokes: Optional[list[Any]] = None
    # v2.1: add_3d scene {"autoRotate"?, "objects"?: [...], "gltf"?: url}
    scene: Optional[dict[str, Any]] = None
    # v2.2: who drew it ("Twin" or a human name). Browsers may send it; server overwrites.
    by: Optional[str] = None


class Box(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    label: str
    x: int
    y: int
    kind: BoxKind
    frame: Optional[str] = None


class Edge(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    from_id: str
    to_id: str
    label: Optional[str] = None
    frame: Optional[str] = None


class BoardSnapshot(Msg):
    type: Literal["board_snapshot"] = "board_snapshot"
    boxes: list[Box] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    shapes: Optional[list[dict]] = None       # v2 shapes (omitted when none)
    active_frame: Optional[str] = None        # v2


AgentState = Literal["started", "done", "failed", "cancelled"]


class AgentEvent(Msg):
    type: Literal["agent_event"] = "agent_event"
    event_id: str
    parent_id: Optional[str] = None
    agent: str
    model: Optional[str] = None
    state: AgentState
    detail: Optional[str] = None
    ts: Optional[int] = None


class Task(Msg):
    type: Literal["task"] = "task"
    id: str
    title: str
    owner: Optional[str] = None
    source: Optional[str] = None


class Metric(Msg):
    type: Literal["metric"] = "metric"
    version: str
    split: str
    name: str
    value: float


class Error(Msg):
    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True


class Topic(Msg):
    """v2: current meeting topic (one whiteboard frame per topic)."""
    type: Literal["topic"] = "topic"
    topic_id: str
    title: str
    active: bool = True
    changed: bool = False


class Person(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    kind: Literal["human", "ai"] = "human"
    speaking: bool = False


class Participants(Msg):
    type: Literal["participants"] = "participants"
    people: list[Person] = Field(default_factory=list)


class Brief(Msg):
    """v2: Twin's pre-meeting brief."""
    type: Literal["brief"] = "brief"
    title: str
    predictions: list[str] = Field(default_factory=list)
    ts: Optional[int] = None


# ---------------------------------------------------------------- v2.2 screen share (both directions)

class ScreenFrame(Msg):
    """v2.2: one JPEG frame of a shared screen. Browser -> server -> other browsers."""
    type: Literal["screen_frame"] = "screen_frame"
    user: Optional[str] = None
    jpeg: str
    w: Optional[int] = None
    h: Optional[int] = None
    ts: Optional[int] = None


class ScreenStop(Msg):
    """v2.2: sharer stopped sharing."""
    type: Literal["screen_stop"] = "screen_stop"
    user: Optional[str] = None


# v2.2 messages are parsed, but kept out of ALL_MESSAGES until PROTOCOL.md's
# `json-proposed` fences are flipped to `json` (test_protocol checks the two match).
V22_MESSAGES = (ScreenFrame, ScreenStop)


# ---------------------------------------------------------------- parse / dump / send

ALL_MESSAGES = (
    Hello, UserText, BargeIn, PushToTalk, Control, DrawError, CalendarJoin, ConnectRepo,
    Status, Transcript, Audio, StopAudio, Draw, BoardSnapshot, AgentEvent, Task, Metric, Error,
    Topic, Participants, Brief,
)

AnyMessage = Annotated[
    Union[
        Hello, UserText, BargeIn, PushToTalk, Control, DrawError, CalendarJoin, ConnectRepo,
        Status, Transcript, Audio, StopAudio, Draw, BoardSnapshot, AgentEvent, Task, Metric, Error,
        Topic, Participants, Brief,
        ScreenFrame, ScreenStop,
    ],
    Field(discriminator="type"),
]
_ADAPTER: TypeAdapter = TypeAdapter(AnyMessage)


def parse(raw: str | bytes | dict) -> Msg:
    """Parse one wire message into its model. Raises ValueError on anything invalid."""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid JSON: {e}") from None
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("message must be a JSON object")
    if "type" not in data:
        raise ValueError("message has no 'type'")
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as e:
        raise ValueError(f"invalid {data.get('type')!r} message: {e.errors(include_url=False)}") from None


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


def dump(model: BaseModel | dict) -> dict:
    """Model -> wire dict. None-valued optionals are dropped (except keep_none fields).

    Dicts pass through unchanged (agents may build messages by hand).
    """
    if isinstance(model, dict):
        return model
    if not isinstance(model, BaseModel):
        raise TypeError(f"cannot dump {type(model).__name__}")
    full = model.model_dump(mode="json")
    out = _strip_none(full)
    for name in getattr(model, "keep_none", ()):
        out[name] = full.get(name)
    return out


def dumps(model: BaseModel | dict) -> str:
    return json.dumps(dump(model), separators=(",", ":"))


async def send(ws, model_or_dict: BaseModel | dict) -> None:
    """Send one message on a starlette WebSocket."""
    await ws.send_text(dumps(model_or_dict))
