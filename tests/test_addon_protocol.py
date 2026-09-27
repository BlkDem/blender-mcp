"""The add-on's protocol copy must not drift from the server's.

The add-on runs inside Blender, which has neither pydantic nor this package
available, so :mod:`blender_mcp.protocol` is a deliberate stdlib duplicate of
:mod:`server.blender.protocol`. A duplicate needs a test that fails the moment
the two disagree, and that is what this file is.
"""

from __future__ import annotations

import json

import pytest
from blender_mcp import protocol as addon

from server.blender.protocol import Action
from server.errors import ErrorCode


def test_action_values_match() -> None:
    server_actions = {action.value for action in Action}
    addon_actions = {getattr(addon, action.name) for action in Action if hasattr(addon, action.name)}
    assert addon_actions == server_actions


def test_mutating_actions_match() -> None:
    server_mutating = {action.value for action in Action if action.is_mutation}
    assert set(addon.MUTATING_ACTIONS) == server_mutating


def test_error_codes_match() -> None:
    assert {code.value for code in ErrorCode} == set(addon.ERROR_CODES)


def test_success_envelope_shape_matches_the_server_model() -> None:
    payload = json.loads(addon.encode_response("abc", {"a": 1}))
    assert payload == {"id": "abc", "success": True, "result": {"a": 1}}


def test_error_envelope_shape_matches_the_server_model() -> None:
    payload = json.loads(addon.encode_error("abc", addon.OBJECT_NOT_FOUND, "nope"))
    assert payload == {
        "id": "abc",
        "success": False,
        "error": {"code": "OBJECT_NOT_FOUND", "message": "nope", "details": {}},
    }


def test_addon_response_parses_as_a_server_response() -> None:
    from server.blender.protocol import Response

    raw = addon.encode_response("abc", {"objects_total": 1})
    response = Response.model_validate_json(raw)
    assert response.raise_for_status() == {"objects_total": 1}


def test_addon_error_parses_as_a_server_response() -> None:
    from server.blender.protocol import Response
    from server.errors import BlenderMCPError

    raw = addon.encode_error("abc", addon.OBJECT_ALREADY_EXISTS, "Object 'T' already exists")
    response = Response.model_validate_json(raw)
    with pytest.raises(BlenderMCPError) as excinfo:
        response.raise_for_status()
    assert excinfo.value.code is ErrorCode.OBJECT_ALREADY_EXISTS


def test_addon_error_carries_details() -> None:
    raw = addon.encode_error("abc", addon.EXECUTION_ERROR, "boom", {"traceback": ["line 1"]})
    assert json.loads(raw)["error"]["details"] == {"traceback": ["line 1"]}


def test_request_ids_are_unique() -> None:
    assert len({addon.new_request_id() for _ in range(100)}) == 100


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '{"action": "ping"}',
        '{"id": "x"}',
        '{"id": 1, "action": "ping"}',
        '{"id": "x", "action": "ping", "params": []}',
    ],
)
def test_malformed_frames_are_rejected(raw: str) -> None:
    with pytest.raises(addon.ProtocolError):
        addon.parse_request(raw)


def test_valid_request_is_parsed_into_its_parts() -> None:
    request_id, action, params = addon.parse_request(
        '{"id": "abc", "action": "create_object", "params": {"name": "Cube"}}'
    )
    assert (request_id, action, params) == ("abc", "create_object", {"name": "Cube"})


def test_params_default_to_empty() -> None:
    _, _, params = addon.parse_request('{"id": "abc", "action": "get_scene"}')
    assert params == {}


def test_encode_exception_maps_an_action_error_to_its_code() -> None:
    raw = addon.encode_exception("abc", addon.ActionError(addon.OBJECT_NOT_FOUND, "gone"))
    assert json.loads(raw)["error"]["code"] == "OBJECT_NOT_FOUND"


def test_encode_exception_wraps_anything_else_as_a_blender_error() -> None:
    raw = addon.encode_exception("abc", ValueError("bad value"))
    payload = json.loads(raw)
    assert payload["error"]["code"] == addon.BLENDER_ERROR
    assert payload["error"]["message"] == "ValueError: bad value"


def test_protocol_version_is_declared() -> None:
    assert addon.PROTOCOL_VERSION == 1
