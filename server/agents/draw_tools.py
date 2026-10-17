"""The v2/v2.1 drawing vocabulary shared by JSON planning and native tools."""

import math

S = {"type": "string"}
ID = {"type": "string", "description": "Stable lowercase slug; reuse existing ids"}
N = {"type": "number", "minimum": 0, "maximum": 1000}
DIM = {"type": "number", "exclusiveMinimum": 0, "maximum": 1000}
POINT = {"type": "array", "items": N, "minItems": 2, "maxItems": 2}
STRINGS = {"type": "array", "items": S}
SERIES = {"type": "array", "items": {"type": "object", "properties": {
    "name": S, "values": {"type": "array", "items": {"anyOf": [
        {"type": "number"}, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
    ]}},
}, "required": ["name", "values"], "additionalProperties": False}}
POSITION = {"x": N, "y": N, "w": DIM, "h": DIM, "frame": ID}
POS3 = {"description": "Local scene coordinates near origin, normally -5..5; NEVER board pixels", "type": "array", "items": {"type": "number", "minimum": -100, "maximum": 100}, "minItems": 3, "maxItems": 3}
ROT3 = {"type": "array", "items": {"type": "number", "minimum": -8 * math.pi, "maximum": 8 * math.pi}, "minItems": 3, "maxItems": 3}
SCALE3 = {"description": "Local object dimensions, normally 0.1..3 scene units", "type": "array", "items": {"type": "number", "exclusiveMinimum": 0, "maximum": 100}, "minItems": 3, "maxItems": 3}
PRIMITIVE3D = {"type": "object", "properties": {
    "id": ID,
    "shape": {"type": "string", "enum": ["box", "cylinder", "sphere", "cone", "torus", "plane"]},
    "pos": POS3, "rot": ROT3, "scale": SCALE3,
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "label": {"type": "string", "maxLength": 100},
}, "required": ["id", "shape", "pos", "scale", "color"], "additionalProperties": False}
SCENE3D = {"type": "object", "properties": {
    "autoRotate": {"type": "boolean"},
    "objects": {"type": "array", "items": PRIMITIVE3D, "minItems": 1, "maxItems": 60},
    "gltf": {"type": "string", "pattern": "^https://", "maxLength": 2048},
}, "anyOf": [{"required": ["objects"]}, {"required": ["gltf"]}], "additionalProperties": False}


def tool(name, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": f"Apply {name} to the shared whiteboard.",
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


def shape(name, fields, required):
    return tool(name, {"id": ID, **POSITION, **fields}, ["id", "x", "y", *required])


DRAW_TOOLS = [
    tool("add_box", {"id": ID, "label": S, "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                     "y": {"type": "integer", "minimum": 0, "maximum": 1000}, "frame": ID,
                     "kind": {"type": "string", "enum": ["service", "datastore", "client", "external", "note"]}},
         ["id", "label", "x", "y", "kind"]),
    tool("connect", {"id": ID, "from_id": ID, "to_id": ID, "label": S, "frame": ID}, ["id", "from_id", "to_id"]),
    tool("rename", {"id": ID, "label": S}, ["id", "label"]),
    tool("remove", {"id": ID}, ["id"]),
    tool("highlight", {"ids": {"type": "array", "items": ID}, "on": {"type": "boolean"}}, ["ids", "on"]),
    tool("clear", {}, []),
    shape("frame", {"title": S}, ["title"]),
    tool("focus", {"id": ID}, ["id"]),
    shape("add_sticky", {"text": S, "color": {"type": "string", "enum": ["yellow", "blue", "green", "pink", "orange"]}}, ["text", "color"]),
    shape("add_text", {"text": S, "size": {"type": "string", "enum": ["s", "m", "l"]}}, ["text"]),
    shape("add_mermaid", {"code": S}, ["code"]),
    shape("add_chart", {"chart": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
                        "title": S, "labels": STRINGS, "series": SERIES}, ["chart", "title", "series"]),
    shape("add_table", {"columns": STRINGS, "rows": {"type": "array", "items": {
        "type": "array", "items": {"type": ["string", "number", "boolean"]}}}}, ["columns", "rows"]),
    shape("add_code", {"language": S, "code": S}, ["language", "code"]),
    shape("add_mock", {"title": S, "html": {"type": "string", "maxLength": 4096}}, ["title", "html"]),
    shape("add_3d", {"title": S, "scene": SCENE3D}, ["title", "scene"]),
    tool("sketch", {"id": ID, "frame": ID, "strokes": {"type": "array", "items": {"type": "array", "items": POINT}},
                    "color": {"type": "string", "enum": ["black", "red", "blue", "green", "orange", "violet"]}}, ["id", "strokes", "color"]),
]

# Updates are validated again against the stored shape type by BoardState.
editable = {}
for definition in DRAW_TOOLS:
    if definition["function"]["name"].startswith("add_") or definition["function"]["name"] in {"frame", "sketch"}:
        editable.update(definition["function"]["parameters"]["properties"])
editable["color"] = S
DRAW_TOOLS.append(tool("update", editable, ["id"]))
