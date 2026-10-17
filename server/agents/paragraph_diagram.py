"""One-call deep architecture diagrams for pasted, multi-sentence specifications."""

from __future__ import annotations

import copy
import asyncio
import json
import re
from typing import Any
from uuid import uuid4

import config
import llm


_ARCH = re.compile(r"\b(?:architecture|service|database|postgres|redis|gateway|queue|cache|client|api)\b", re.I)
_KINDS = {"client", "service", "datastore", "external", "note"}


def paragraph_request(moments: list[dict], source_lines: list[dict] | None) -> str | None:
    """Use only one pasted source line; ordinary short conversation stays on its old path."""
    lines = source_lines or moments
    if not lines:
        return None
    text = str(lines[-1].get("text") or "").strip()
    explicit = re.search(r"\b(?:layered|detailed)\s+architecture\b", text, re.I)
    if len(text) < 300 or len(text) > 12000 or (not explicit and len(_ARCH.findall(text)) < 4):
        return None
    return text


def _planned_ops(value: Any, board, topic_id: str | None) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {"layers", "nodes", "edges"}:
        raise ValueError("architecture plan needs only layers, nodes, edges")
    raw_layers, raw_nodes, raw_edges = (value[key] for key in ("layers", "nodes", "edges"))
    if not isinstance(raw_layers, list) or not 2 <= len(raw_layers) <= 20:
        raise ValueError("architecture needs 2 to 20 source layers")
    if not isinstance(raw_nodes, list) or not 8 <= len(raw_nodes) <= 20:
        raise ValueError("architecture needs 8 to 20 named nodes")
    if not isinstance(raw_edges, list) or not 7 <= len(raw_edges) <= 32:
        raise ValueError("architecture needs 7 to 32 labeled edges")
    if len(raw_layers) > 6:
        # Some models create a layer for each subsystem despite the requested
        # limit. Merge adjacent *used* layers; the graph's nodes and edges stay
        # exactly as extracted, and the resulting frames remain legible.
        if any(not isinstance(node, dict) or type(node.get("layer")) is not int
               or not 0 <= node["layer"] < len(raw_layers) for node in raw_nodes):
            raise ValueError("excess layers need numeric node memberships")
        used = sorted({node["layer"] for node in raw_nodes})
        if len(used) < 2:
            raise ValueError("architecture needs multiple used layers")
        group_count = min(5, len(used))
        grouped: list[list[str]] = [[] for _ in range(group_count)]
        remap = {}
        for ordinal, old_index in enumerate(used):
            group = ordinal * group_count // len(used)
            remap[old_index] = group
            raw_layer = raw_layers[old_index]
            title = raw_layer if isinstance(raw_layer, str) else (
                raw_layer.get("title") if isinstance(raw_layer, dict) else None)
            if not isinstance(title, str) or not title.strip():
                raise ValueError("invalid source layer title")
            grouped[group].append(title.strip())
        raw_layers = [" / ".join(group)[:100] for group in grouped]
        raw_nodes = [{**node, "layer": remap[node["layer"]]} for node in raw_nodes]
    # Current model output uses array positions for references, removing the
    # chance of an edge naming a node omitted from the final node list. Accept
    # the original string-id format too, for existing callers and fixtures.
    layers = []
    for index, raw in enumerate(raw_layers):
        if isinstance(raw, str):
            layers.append({"id": f"layer_{index}", "title": raw})
        elif isinstance(raw, dict) and set(raw) == {"title"}:
            layers.append({"id": f"layer_{index}", "title": raw["title"]})
        else:
            layers.append(raw)
    nodes = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ValueError("node must be an object")
        node = dict(raw)
        if set(node) == {"label", "kind", "layer"}:
            node["id"] = f"node_{index}"
        layer = node.get("layer")
        if type(layer) is int:
            if not 0 <= layer < len(layers):
                raise ValueError("node layer index is out of range")
            node["layer"] = layers[layer]["id"]
        nodes.append(node)
    edges = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise ValueError("edge must be an object")
        edge = dict(raw)
        for endpoint in ("from", "to"):
            index = edge.get(endpoint)
            if type(index) is int:
                if not 0 <= index < len(nodes):
                    raise ValueError("architecture edge endpoint is out of range")
                edge[endpoint] = nodes[index]["id"]
        edges.append(edge)
    layer_names = []
    for layer in layers:
        if not isinstance(layer, dict) or set(layer) != {"id", "title"}:
            raise ValueError("layer needs id and title")
        if (not isinstance(layer["id"], str) or not layer["id"].strip()
                or len(layer["id"]) > 100):
            raise ValueError("invalid layer id")
        if layer["id"] in layer_names or not isinstance(layer["title"], str) or not layer["title"].strip():
            raise ValueError("duplicate or unnamed layer")
        layer_names.append(layer["id"])
    node_ids = []
    by_layer = {name: [] for name in layer_names}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label", "kind", "layer"}:
            raise ValueError("node needs id, label, kind, layer")
        if (not isinstance(node["id"], str) or not node["id"].strip()
                or len(node["id"]) > 100
                or node["id"] in node_ids):
            raise ValueError("duplicate or invalid node id")
        if not isinstance(node["label"], str) or not node["label"].strip() or len(node["label"]) > 100:
            raise ValueError("invalid node label")
        if node["kind"] not in _KINDS or node["layer"] not in by_layer:
            raise ValueError("invalid node kind or layer")
        node_ids.append(node["id"])
        by_layer[node["layer"]].append(node)
    if any(not members for members in by_layer.values()):
        raise ValueError("every architecture layer needs a node")
    prefix = f"arch_{uuid4().hex[:8]}"
    ops = []
    height = 900 // len(layers)
    for index, layer in enumerate(layers):
        frame_id = f"{prefix}_l{index + 1}"
        ops.append({"op": "frame", "id": frame_id, "title": layer["title"],
                    "x": 25, "y": 50 + index * height, "w": 950, "h": height - 10,
                    **({"frame": topic_id} if topic_id else {})})
        members = by_layer[layer["id"]]
        for position, node in enumerate(members):
            ops.append({"op": "add_box", "id": f"{prefix}_n{node_ids.index(node['id']) + 1}",
                        "label": node["label"], "kind": node["kind"],
                        "x": round(80 + (position + 0.5) * 840 / len(members)),
                        "y": 250, "frame": frame_id})
    mapped = {node_id: f"{prefix}_n{index + 1}" for index, node_id in enumerate(node_ids)}
    # A model may use the displayed label instead of its JSON id in an edge.
    # Resolve only unambiguous names; never invent a missing component.
    def key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())
    aliases: dict[str, str | None] = {}
    for node in nodes:
        for name in (node["id"], node["label"]):
            normalized = key(name)
            target = node["id"]
            if normalized in aliases and aliases[normalized] != target:
                aliases[normalized] = None
            else:
                aliases[normalized] = target
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict) or set(edge) != {"from", "to", "label"}:
            raise ValueError("edge needs from, to, label")
        if not isinstance(edge["label"], str) or not edge["label"].strip() or len(edge["label"]) > 100:
            raise ValueError("architecture edge needs a short label")
        if not isinstance(edge["from"], str) or not isinstance(edge["to"], str):
            raise ValueError("architecture edge endpoint must be text")
        source = aliases.get(key(edge["from"]))
        target = aliases.get(key(edge["to"]))
        if source is None or target is None or source == target:
            raise ValueError("architecture edge endpoint is missing or self-linked")
        ops.append({"op": "connect", "id": f"{prefix}_e{index + 1}",
                    "from_id": mapped[source], "to_id": mapped[target],
                    "label": edge["label"], **({"frame": topic_id} if topic_id else {})})
    if topic_id:
        ops.append({"op": "focus", "id": topic_id})
    staged = copy.deepcopy(board)
    for op in ops:
        staged.apply(op)
    return ops


async def draw_paragraph(session, board, request: str, topic_id: str | None, publish,
                         expected_revision: int) -> list[dict]:
    """Stage the entire plan, then publish only if the source board is still current."""
    raw = await asyncio.wait_for(llm.chat(config.MODEL_RESPONDER, [
        {"role": "system", "content": "Extract a layered architecture graph only from the supplied specification. "
         "Return ONLY one JSON object with exactly these keys: "
         "layers:[string], nodes:[{label,kind,layer}], edges:[{from,to,label}]. "
         "Use 2-5 named layers (group related components; never one layer per service), "
         "8-20 real named components, and 7-32 labeled relationships. "
         "Node layer is a zero-based integer position in layers. Edge from/to MUST copy the "
         "EXACT label of a node in the final nodes array; never use numeric edge endpoints. "
         "For example if nodes contain labels 'Web Client' and 'API Gateway', an edge is "
         "{\"from\":\"Web Client\",\"to\":\"API Gateway\",\"label\":\"HTTPS\"}. "
         "Kinds are client, service, datastore, external, or note. "
         "First list every named component in the source, then connect only listed nodes. "
         "Check every edge endpoint against the final node labels before responding. "
         "Include every stated relationship. Events and messages are edge labels, unless "
         "the source explicitly names a queue or broker. Do not invent components or edges."},
        {"role": "user", "content": request},
    ], max_tokens=4000, response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}}), timeout=30)
    from agents.diagrammer import StaleBoardError
    if session.board is not board or board.revision != expected_revision:
        raise StaleBoardError("board changed before architecture plan")
    ops = _planned_ops(json.loads(raw), board, topic_id)
    draws = []
    for op in ops:
        if session.board is not board or board.revision != expected_revision:
            raise StaleBoardError("board changed during architecture draw")
        draw = board.apply(op)
        expected_revision = board.revision
        draws.append(draw)
        await publish(session, board, draw, expected_revision)
    return draws
