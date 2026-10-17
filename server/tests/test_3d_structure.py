"""Context-sensitive 3D quality gate for physical assemblies."""

import copy

import pytest

from board import _three_touching, validate_3d_structure


def part(id, shape, pos, scale=(0.4, 0.4, 0.4), *, label=None, rot=None):
    item = {"id": id, "shape": shape, "pos": list(pos), "scale": list(scale),
            "color": "#4488cc"}
    if label is not None:
        item["label"] = label
    if rot is not None:
        item["rot"] = list(rot)
    return item


ASSEMBLY = {"objects": [
    part("base", "cylinder", (0, 0, 0), (1, 0.4, 1), label="Base"),
    part("link1", "box", (0, 0.9, 0), (0.3, 1.6, 0.3), label="Link 1"),
    part("joint1", "sphere", (0, 1.7, 0), label="Joint 1"),
    part("link2", "box", (0.3, 1.9, 0), (1, 0.3, 0.3), label="Link 2"),
    part("joint2", "sphere", (0.8, 1.9, 0), label="Joint 2"),
    part("link3", "box", (1.35, 2.3, 0), (1.1, 0.3, 0.3), label="Link 3"),
    part("joint3", "sphere", (1.9, 2.3, 0), label="Joint 3"),
]}


def test_connected_robot_with_three_joints_passes_without_mutation():
    scene = copy.deepcopy(ASSEMBLY)
    validate_3d_structure(scene, "Show me a three-joint robot arm", title="Robot arm")
    validate_3d_structure(scene, "Draw a robot arm with 3 joints")
    assert scene == ASSEMBLY


def test_physical_object_requires_four_structural_parts():
    scene = {"objects": ASSEMBLY["objects"][:3]}
    with pytest.raises(ValueError, match="at least 4 named parts"):
        validate_3d_structure(scene, "Show me a robot arm")


def test_spheres_only_rejected_even_when_connected():
    scene = {"objects": [part(f"p{i}", "sphere", (i * 0.35, 0, 0)) for i in range(4)]}
    with pytest.raises(ValueError, match="cannot use only spheres"):
        validate_3d_structure(scene, "Show me a robot arm")


def test_floating_part_and_disconnected_groups_rejected():
    floating = copy.deepcopy(ASSEMBLY)
    floating["objects"].append(part("floating", "box", (8, 8, 8)))
    with pytest.raises(ValueError, match="floating parts.*floating"):
        validate_3d_structure(floating, "Build a robot arm")

    paired = {"objects": [
        part("base1", "box", (0, 0, 0)), part("link1", "box", (0.3, 0, 0)),
        part("base2", "box", (5, 0, 0)), part("link2", "box", (5.3, 0, 0)),
    ]}
    with pytest.raises(ValueError, match="disconnected groups"):
        validate_3d_structure(paired, "A machine")


def test_requested_joint_count_is_exact():
    scene = copy.deepcopy(ASSEMBLY)
    scene["objects"][-1]["id"] = "end_effector"
    scene["objects"][-1]["label"] = "End effector"
    with pytest.raises(ValueError, match="requested 3 joints; found 2"):
        validate_3d_structure(scene, "Show me a 3-joint robot arm")
    # A reference to one numbered joint is not an overall count request.
    validate_3d_structure(ASSEMBLY, "Move joint 2 on this robot arm")


def test_rotated_link_bounds_and_small_rounding_gap():
    link = part("link", "box", (0, 0, 0), (0.2, 2, 0.2), rot=(0, 0, 1.57079632679))
    end = part("end", "sphere", (1.15, 0, 0))
    assert _three_touching(link, end)
    assert not _three_touching(part("upright", "box", (0, 0, 0), (0.2, 2, 0.2)), end)


def test_simple_primitives_and_external_gltf_are_exempt_from_assembly_rules():
    simple = {"objects": [part("sphere", "sphere", (0, 0, 0))]}
    validate_3d_structure(simple, "Show one 3D sphere")
    showcase = {"objects": [part("box", "box", (0, 0, 0)),
                            part("sphere", "sphere", (5, 0, 0))]}
    validate_3d_structure(showcase, "Show these two geometric primitives")
    validate_3d_structure({"gltf": "https://example.com/model.glb"},
                          "Show a robot arm with three joints")


def test_schema_still_validated_before_quality_gate():
    with pytest.raises(ValueError, match="finite"):
        validate_3d_structure({"objects": [part("bad", "box", (0, float("nan"), 0))]},
                              "A machine")
