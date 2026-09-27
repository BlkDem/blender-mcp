"""Tests that run against a real Blender, when one is importable.

Install the Blender Python module to run these::

    pip install bpy==4.2.0     # matches Python 3.11

Without it the whole module skips — the rest of the suite covers the transport
and the MCP surface, but only a real ``bpy`` can tell you whether a primitive
has the right size or whether ``dimensions`` reads stale. Those are exactly the
bugs this file exists to catch.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from tests.support.bpy_gate import require_bpy

bpy = require_bpy()

from blender_mcp import executor, operators  # noqa: E402
from blender_mcp.connection import TransactionState, _push_undo  # noqa: E402
from blender_mcp.protocol import ActionError  # noqa: E402

#: Blender's own default primitive sizes, which this project matches.
EXPECTED_SIZES = {
    "cube": (2.0, 2.0, 2.0),
    "sphere": (2.0, 2.0, 2.0),
    "cylinder": (2.0, 2.0, 2.0),
    "cone": (2.0, 2.0, 2.0),
    "plane": (2.0, 2.0, 0.0),
    "torus": (2.5, 2.5, 0.5),
}


@pytest.fixture(autouse=True)
def empty_scene() -> None:
    """Start every test from a fresh, empty file with a clean undo stack."""
    bpy.ops.wm.read_homefile(use_empty=True)


def create(**params: Any) -> dict[str, Any]:
    return operators.create_object(params)["object"]


# --- primitives --------------------------------------------------------------


@pytest.mark.parametrize("object_type,expected", sorted(EXPECTED_SIZES.items()))
def test_primitives_match_blenders_own_defaults(object_type: str, expected: tuple) -> None:
    obj = create(type=object_type, name=f"T_{object_type}")
    reported = obj["dimensions"]
    assert all(abs(reported[i] - expected[i]) < 0.01 for i in range(3)), (
        f"{object_type}: expected {expected}, got {reported}"
    )


def test_create_reports_the_transform_that_was_asked_for() -> None:
    """dimensions is derived from the evaluated mesh, so it lags a fresh transform.

    This is the regression that made create_object answer with the primitive's
    default size after a non-default scale had already been applied.
    """
    obj = create(type="cube", name="Slab", location=[0, 0, 1], scale=[2, 1, 0.05])
    assert obj["location"] == [0.0, 0.0, 1.0]
    assert obj["scale"] == [2.0, 1.0, 0.05]
    assert obj["dimensions"] == [4.0, 2.0, 0.1]


def test_creating_the_same_name_twice_is_refused() -> None:
    create(type="cube", name="Box")
    with pytest.raises(ActionError) as excinfo:
        create(type="cube", name="Box")
    assert excinfo.value.code == "OBJECT_ALREADY_EXISTS"
    assert "Box.001" not in bpy.data.objects


def test_an_unknown_primitive_is_rejected() -> None:
    with pytest.raises(ActionError) as excinfo:
        create(type="dodecahedron", name="D12")
    assert excinfo.value.code == "INVALID_OBJECT_TYPE"


@pytest.mark.parametrize("field", ["location", "rotation", "scale"])
def test_a_malformed_vector_is_rejected(field: str) -> None:
    with pytest.raises(ActionError) as excinfo:
        create(type="cube", name="X", **{field: [1, 2]})
    assert excinfo.value.code == "INVALID_PARAMETER"


def test_a_missing_name_is_rejected() -> None:
    with pytest.raises(ActionError) as excinfo:
        create(type="cube", name="")
    assert excinfo.value.code == "INVALID_PARAMETER"


def test_objects_can_be_placed_in_a_named_collection() -> None:
    collection = bpy.data.collections.new("Props")
    bpy.context.scene.collection.children.link(collection)
    obj = create(type="torus", name="Ring", collection="Props")
    assert obj["collection"] == ["Props"]


def test_an_unknown_collection_is_rejected() -> None:
    with pytest.raises(ActionError) as excinfo:
        create(type="cube", name="X", collection="Nowhere")
    assert excinfo.value.code == "OBJECT_NOT_FOUND"


# --- rotation ----------------------------------------------------------------


def test_rotation_is_degrees_in_and_degrees_out() -> None:
    obj = create(type="cube", name="Rot", rotation=[0, 45, 90])
    assert obj["rotation"] == [0.0, 45.0, 90.0]
    assert abs(bpy.data.objects["Rot"].rotation_euler.z - 1.5707963) < 1e-5


def test_rotation_survives_a_round_trip_without_drift() -> None:
    """90 must come back as 90, not 90.000003: models compare before and after."""
    obj = create(type="cube", name="Rot", rotation=[12.5, -30.25, 90])
    assert obj["rotation"] == [12.5, -30.25, 90.0]


# --- update ------------------------------------------------------------------


def test_update_changes_only_the_fields_it_is_given() -> None:
    create(type="cube", name="Box", location=[1, 2, 3], rotation=[10, 20, 30], scale=[2, 2, 2])
    updated = operators.update_object({"name": "Box", "location": [0, 0, 0]})["object"]
    assert updated["location"] == [0.0, 0.0, 0.0]
    assert updated["rotation"] == [10.0, 20.0, 30.0]
    assert updated["scale"] == [2.0, 2.0, 2.0]


def test_update_dimensions_derives_the_scale() -> None:
    create(type="cube", name="Box")
    updated = operators.update_object({"name": "Box", "dimensions": [4, 2, 1]})["object"]
    assert updated["dimensions"] == [4.0, 2.0, 1.0]
    assert updated["scale"] == [2.0, 1.0, 0.5]


def test_update_dimensions_composes_with_an_existing_scale() -> None:
    create(type="cube", name="Box", scale=[2, 2, 2])
    updated = operators.update_object({"name": "Box", "dimensions": [4, 2, 1]})["object"]
    assert updated["dimensions"] == [4.0, 2.0, 1.0]


def test_update_dimensions_must_be_positive() -> None:
    create(type="cube", name="Box")
    with pytest.raises(ActionError) as excinfo:
        operators.update_object({"name": "Box", "dimensions": [0, 1, 1]})
    assert excinfo.value.code == "INVALID_PARAMETER"


def test_visibility_hides_in_both_the_viewport_and_renders() -> None:
    create(type="cube", name="Box")
    hidden = operators.update_object({"name": "Box", "visibility": False})["object"]
    assert hidden["visibility"] == {"hide_viewport": True, "hide_render": True, "visible": False}
    shown = operators.update_object({"name": "Box", "visibility": True})["object"]
    assert shown["visibility"] == {"hide_viewport": False, "hide_render": False, "visible": True}


def test_updating_a_missing_object_is_refused() -> None:
    with pytest.raises(ActionError) as excinfo:
        operators.update_object({"name": "Ghost", "location": [0, 0, 0]})
    assert excinfo.value.code == "OBJECT_NOT_FOUND"


# --- reads and deletes -------------------------------------------------------


def test_get_object_reports_materials_and_modifiers() -> None:
    create(type="cube", name="Box")
    material = bpy.data.materials.new("Red")
    bpy.data.objects["Box"].data.materials.append(material)
    bpy.data.objects["Box"].modifiers.new("Bevel", "BEVEL")

    detail = operators.get_object({"name": "Box"})
    assert detail["materials"] == ["Red"]
    assert detail["modifiers"] == [{"name": "Bevel", "type": "BEVEL"}]
    assert detail["parent"] is None


def test_non_mesh_objects_do_not_break_the_listing() -> None:
    """Cameras and lights have no material slots; the listing must still work."""
    camera_data = bpy.data.cameras.new("Cam")
    bpy.context.scene.collection.objects.link(bpy.data.objects.new("Cam", camera_data))
    light_data = bpy.data.lights.new("Key", type="POINT")
    bpy.context.scene.collection.objects.link(bpy.data.objects.new("Key", light_data))
    create(type="cube", name="Box")

    scene = operators.get_scene({})
    assert scene["cameras"] == ["Cam"]
    assert [light["name"] for light in scene["lights"]] == ["Key"]
    assert operators.get_object({"name": "Cam"})["materials"] == []


def test_get_scene_stays_compact() -> None:
    create(type="cube", name="Box")
    scene = operators.get_scene({})
    assert scene["objects_count"] == 1
    assert set(scene["objects"][0]) == {
        "name",
        "type",
        "location",
        "rotation",
        "scale",
        "dimensions",
        "visible",
    }
    # No mesh data, whatever the caller asks for.
    assert "vertices" not in json_text(scene)


def test_get_scene_matches_the_documented_shape() -> None:
    create(type="cube", name="Box")
    scene = operators.get_scene({})
    for field in (
        "scene",
        "objects_count",
        "frame",
        "active_object",
        "active_camera",
        "render_engine",
        "collections",
        "objects",
    ):
        assert field in scene, f"{field} missing from the scene payload"
    assert scene["active_object"] == "Box"
    assert scene["render"]["engines"]


def test_get_scene_can_include_detail_and_drop_objects() -> None:
    create(type="cube", name="Box")
    assert "collection" in operators.get_scene({"include_details": True})["objects"][0]
    assert "objects" not in operators.get_scene({"include_objects": False})


def test_getting_a_missing_object_is_refused() -> None:
    with pytest.raises(ActionError) as excinfo:
        operators.get_object({"name": "Ghost"})
    assert excinfo.value.code == "OBJECT_NOT_FOUND"


def test_delete_removes_the_object() -> None:
    create(type="cube", name="Box")
    assert operators.delete_object({"name": "Box"}) == {
        "success": True,
        "deleted": "Box",
        "type": "MESH",
    }
    assert "Box" not in bpy.data.objects


def test_deleting_twice_is_refused_the_second_time() -> None:
    create(type="cube", name="Box")
    operators.delete_object({"name": "Box"})
    with pytest.raises(ActionError) as excinfo:
        operators.delete_object({"name": "Box"})
    assert excinfo.value.code == "OBJECT_NOT_FOUND"


def test_ping_answers_on_the_main_thread() -> None:
    answer = operators.ping()
    assert answer["pong"] is True
    assert answer["scene"] == bpy.context.scene.name
    assert answer["blender_version"] == bpy.app.version_string


def test_an_unknown_action_is_refused() -> None:
    with pytest.raises(ActionError) as excinfo:
        operators.dispatch("make_coffee", {})
    assert excinfo.value.code == "UNKNOWN_ACTION"


# --- execute_python ----------------------------------------------------------


def test_execute_python_returns_the_result_variable() -> None:
    outcome = executor.execute_user_code(
        "import bpy\nresult = {'objects': len(bpy.data.objects), 'blender': bpy.app.version_string}"
    )
    assert outcome["success"] is True
    assert outcome["result"] == {"objects": 0, "blender": bpy.app.version_string}
    assert outcome["result_json"] == json.dumps(outcome["result"])


def test_execute_python_without_a_result_still_succeeds() -> None:
    outcome = executor.execute_user_code("import bpy\nbpy.ops.wm.read_homefile(use_empty=True)")
    assert outcome["success"] is True
    assert "note" in outcome


def test_execute_python_reports_a_failure_with_a_traceback() -> None:
    with pytest.raises(ActionError) as excinfo:
        executor.execute_user_code("result = undefined_name")
    assert excinfo.value.code == "PYTHON_EXECUTION_ERROR"
    assert "NameError" in excinfo.value.message
    assert "undefined_name" in str(excinfo.value.details["traceback"])


def test_execute_python_can_reach_bpy() -> None:
    outcome = executor.execute_user_code(
        "import bpy\n"
        "bpy.ops.mesh.primitive_ico_sphere_add(location=(0, 0, 2))\n"
        "obj = bpy.context.object\n"
        "obj.name = 'ViaPython'\n"
        "result = {'created': obj.name, 'z': round(obj.location.z, 3)}"
    )
    assert outcome["result"] == {"created": "ViaPython", "z": 2.0}
    assert "ViaPython" in bpy.data.objects


def test_execute_python_coerces_unserialisable_results() -> None:
    outcome = executor.execute_user_code("import bpy\nresult = bpy.data.objects")
    assert isinstance(outcome["result"], list)
    assert all(isinstance(name, str) for name in outcome["result"])


def test_execute_python_does_not_leak_state_between_calls() -> None:
    executor.execute_user_code("leaked = 42")
    outcome = executor.execute_user_code("result = 'leaked' in dir()")
    assert outcome["result"] is False


def test_execute_python_through_the_operator() -> None:
    outcome = operators.execute_python({"code": "result = 1 + 1"})
    assert outcome["result"] == 2


def test_execute_python_rejects_empty_code() -> None:
    with pytest.raises(ActionError) as excinfo:
        operators.execute_python({"code": "   "})
    assert excinfo.value.code == "INVALID_PARAMETER"


# --- transactions and undo ---------------------------------------------------


def test_rollback_undoes_every_step_in_a_transaction() -> None:
    _push_undo("baseline")
    create(type="cube", name="Before")

    state = TransactionState()
    state.begin()
    _push_undo("MCP transaction begin")
    for name in ("InTx1", "InTx2"):
        create(type="cube", name=name)
        state.steps += 1
        _push_undo("MCP create_object")

    assert state.rollback() == 2
    assert sorted(obj.name for obj in bpy.data.objects) == ["Before"]
    assert state.active is False


def test_commit_keeps_the_changes() -> None:
    state = TransactionState()
    state.begin()
    _push_undo("begin")
    create(type="cube", name="Kept")
    state.steps += 1
    assert state.commit() == 1
    assert "Kept" in bpy.data.objects


def test_nesting_a_transaction_is_refused() -> None:
    state = TransactionState()
    state.begin()
    with pytest.raises(ActionError) as excinfo:
        state.begin()
    assert excinfo.value.code == "TRANSACTION_ACTIVE"


def test_rollback_without_a_transaction_is_refused() -> None:
    with pytest.raises(ActionError) as excinfo:
        TransactionState().rollback()
    assert excinfo.value.code == "TRANSACTION_NOT_ACTIVE"


# --- render ------------------------------------------------------------------


@pytest.fixture
def lit_scene() -> Any:
    """A one-cube scene with a sun and a camera, ready for a tiny Cycles render."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    create(type="plane", name="Floor", scale=[10, 10, 1])
    create(type="cube", name="Box", location=[0, 0, 0.5], scale=[0.6, 0.6, 0.5])
    operators.execute_python(
        {
            "code": """
import bpy, math
material = bpy.data.materials.new("Red")
material.diffuse_color = (0.8, 0.05, 0.05, 1.0)
bpy.data.objects["Box"].data.materials.append(material)

light = bpy.data.lights.new("Sun", type='SUN')
light.energy = 4.0
light_object = bpy.data.objects.new("Sun", light)
light_object.rotation_euler = (math.radians(35), 0, math.radians(30))
bpy.context.scene.collection.objects.link(light_object)

camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("CamData"))
camera.location = (3, -3, 2)
camera.rotation_euler = (math.radians(68), 0, math.radians(45))
bpy.context.scene.collection.objects.link(camera)
bpy.context.scene.camera = camera
result = True
"""
        }
    )
    return scene


def test_render_writes_an_image(tmp_path, lit_scene: Any) -> None:
    output = tmp_path / "render.png"
    result = operators.render(
        {
            "engine": "CYCLES",
            "resolution_x": 32,
            "resolution_y": 24,
            "samples": 1,
            "output_path": str(output),
        }
    )

    assert result["success"] is True
    assert result["used"] == {"engine": "CYCLES", "resolution": [32, 24], "samples": 1}
    assert result["render_time"] >= 0.0
    assert os.path.exists(result["output_path"])
    with open(result["output_path"], "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_render_restores_the_scenes_settings(tmp_path, lit_scene: Any) -> None:
    before = (
        lit_scene.render.engine,
        lit_scene.render.resolution_x,
        lit_scene.render.resolution_y,
        lit_scene.cycles.samples,
    )
    operators.render(
        {
            "engine": "CYCLES",
            "resolution_x": 32,
            "resolution_y": 24,
            "samples": 1,
            "output_path": str(tmp_path / "restored.png"),
        }
    )
    after = (
        lit_scene.render.engine,
        lit_scene.render.resolution_x,
        lit_scene.render.resolution_y,
        lit_scene.cycles.samples,
    )
    assert after == before


def test_render_rejects_an_unknown_engine(lit_scene: Any) -> None:
    with pytest.raises(ActionError) as excinfo:
        operators.render({"engine": "NOT_AN_ENGINE"})
    assert excinfo.value.code == "INVALID_PARAMETER"
    assert lit_scene.render.engine == "CYCLES"


def test_render_without_a_camera_reports_a_blender_operation_error(lit_scene: Any) -> None:
    lit_scene.camera = None
    with pytest.raises(ActionError) as excinfo:
        operators.render({"engine": "CYCLES", "resolution_x": 16, "resolution_y": 16})
    assert excinfo.value.code in {"BLENDER_OPERATION_FAILED", "INVALID_PARAMETER"}


# --- helpers -----------------------------------------------------------------


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


# --- get_objects -------------------------------------------------------------


def test_get_objects_lists_everything_by_default() -> None:
    for name in ("Alpha", "Beta", "Gamma"):
        create(type="cube", name=name)
    page = operators.get_objects({})
    assert page["total"] == 3
    assert [obj["name"] for obj in page["objects"]] == ["Alpha", "Beta", "Gamma"]
    assert page["truncated"] is False


def test_get_objects_pages_and_says_so() -> None:
    for index in range(5):
        create(type="cube", name=f"Obj{index}")
    first = operators.get_objects({"limit": 2})
    assert [obj["name"] for obj in first["objects"]] == ["Obj0", "Obj1"]
    assert first["total"] == 5
    assert first["truncated"] is True

    second = operators.get_objects({"limit": 2, "offset": 2})
    assert [obj["name"] for obj in second["objects"]] == ["Obj2", "Obj3"]
    last = operators.get_objects({"limit": 2, "offset": 4})
    assert [obj["name"] for obj in last["objects"]] == ["Obj4"]
    assert last["truncated"] is False


def test_get_objects_filters_by_name_and_type() -> None:
    create(type="cube", name="TableTop")
    create(type="sphere", name="TableBall")
    create(type="cylinder", name="LegFL")
    assert [obj["name"] for obj in operators.get_objects({"name_contains": "table"})["objects"]] == [
        "TableBall",
        "TableTop",
    ]
    assert operators.get_objects({"type": "MESH"})["total"] == 3
    assert operators.get_objects({"type": "CAMERA"})["total"] == 0


def test_get_objects_ignores_a_filter_that_matches_nothing() -> None:
    create(type="cube", name="Box")
    page = operators.get_objects({"name_contains": "nothing-here"})
    assert page == {
        "objects": [],
        "count": 0,
        "total": 0,
        "offset": 0,
        "limit": 50,
        "truncated": False,
    }


def test_get_objects_caps_the_page_size() -> None:
    from blender_mcp.operators import MAX_LIST_LIMIT

    assert operators.get_objects({"limit": 100000})["limit"] == MAX_LIST_LIMIT
    assert operators.get_objects({"limit": 0})["limit"] == 1
    assert operators.get_objects({"offset": -5})["offset"] == 0


def test_get_objects_returns_summaries_not_details() -> None:
    create(type="cube", name="Box")
    summary = operators.get_objects({})["objects"][0]
    assert "materials" not in summary
    assert "modifiers" not in summary


# --- rename and material ------------------------------------------------------


def test_update_renames_an_object() -> None:
    create(type="cube", name="Box")
    result = operators.update_object({"name": "Box", "new_name": "Crate"})
    assert result["renamed_to"] == "Box -> Crate"
    assert result["object"]["name"] == "Crate"
    assert "Crate" in bpy.data.objects and "Box" not in bpy.data.objects


def test_renaming_to_the_same_name_is_a_no_op() -> None:
    create(type="cube", name="Box")
    result = operators.update_object({"name": "Box", "new_name": "Box"})
    assert "renamed_to" not in result
    assert "Box" in bpy.data.objects


def test_renaming_onto_a_taken_name_is_refused() -> None:
    create(type="cube", name="Box")
    create(type="cube", name="Crate")
    with pytest.raises(ActionError) as excinfo:
        operators.update_object({"name": "Box", "new_name": "Crate"})
    assert excinfo.value.code == "OBJECT_ALREADY_EXISTS"
    assert "Box" in bpy.data.objects and "Crate" in bpy.data.objects


def test_update_assigns_an_existing_material() -> None:
    create(type="cube", name="Box")
    bpy.data.materials.new("Wood")
    result = operators.update_object({"name": "Box", "material": "Wood"})
    assert result["material"] == "Wood"
    assert result["material_created"] is False
    assert result["object"]["materials"] == ["Wood"]


def test_update_creates_a_missing_material_with_the_given_colour() -> None:
    create(type="cube", name="Box")
    result = operators.update_object({"name": "Box", "material": "Red", "material_color": [0.8, 0.05, 0.05]})
    assert result["material_created"] is True
    material = bpy.data.materials["Red"]
    assert tuple(round(value, 3) for value in material.diffuse_color[:3]) == (0.8, 0.05, 0.05)
    # The shader must agree with the viewport colour, or a render comes out grey.
    bsdf = material.node_tree.nodes["Principled BSDF"]
    assert tuple(round(value, 3) for value in bsdf.inputs["Base Color"].default_value[:3]) == (
        0.8,
        0.05,
        0.05,
    )


def test_update_material_replaces_the_previous_slots() -> None:
    create(type="cube", name="Box")
    operators.update_object({"name": "Box", "material": "First"})
    result = operators.update_object({"name": "Box", "material": "Second"})
    assert result["object"]["materials"] == ["Second"]


def test_a_material_cannot_be_assigned_to_a_camera() -> None:
    camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("CamData"))
    bpy.context.scene.collection.objects.link(camera)
    with pytest.raises(ActionError) as excinfo:
        operators.update_object({"name": "Cam", "material": "Wood"})
    assert excinfo.value.code == "INVALID_PARAMETER"


# --- rotation and the active object ------------------------------------------


def test_a_new_object_becomes_the_active_one() -> None:
    create(type="cube", name="Box")
    assert operators.get_scene({})["active_object"] == "Box"
    create(type="sphere", name="Ball")
    assert operators.get_scene({})["active_object"] == "Ball"


def test_get_object_reports_mesh_statistics() -> None:
    create(type="cube", name="Box")
    detail = operators.get_object({"name": "Box"})
    assert detail["vertices"] == 8
    assert detail["edges"] == 12
    assert detail["polygons"] == 6
    assert detail["loops"] == 24


def test_mesh_statistics_are_absent_for_non_meshes() -> None:
    camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("CamData"))
    bpy.context.scene.collection.objects.link(camera)
    assert "vertices" not in operators.get_object({"name": "Cam"})


def test_summary_visibility_follows_the_hide_flags() -> None:
    create(type="cube", name="Box")
    assert operators.get_objects({})["objects"][0]["visible"] is True
    operators.update_object({"name": "Box", "visibility": False})
    assert operators.get_objects({})["objects"][0]["visible"] is False


@pytest.mark.parametrize("given", ["CUBE", "Cube", "cube"])
def test_the_addon_also_accepts_any_case(given: str) -> None:
    """Both entry points to the bridge normalise, so a direct protocol client
    gets the same answer as one going through the MCP tool."""
    assert create(type=given, name=f"Case{given}")["name"] == f"Case{given}"
