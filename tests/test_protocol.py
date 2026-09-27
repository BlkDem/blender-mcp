"""Tests for the JSON protocol shared by the MCP server and the add-on."""

from __future__ import annotations

import json

import pytest

from server.blender.protocol import (
    Action,
    ErrorInfo,
    Request,
    Response,
    encode,
    new_request,
    parse_request,
    parse_response,
)
from server.errors import BlenderMCPError, ErrorCode


def test_request_serialization() -> None:
    request = Request(id="abc123", action=Action.GET_SCENE.value, params={"include_objects": True})
    payload = json.loads(encode(request))

    assert payload == {"id": "abc123", "action": "get_scene", "params": {"include_objects": True}}
    assert Request.model_validate(payload) == request


def test_request_generates_a_unique_id() -> None:
    ids = {new_request(Action.PING).id for _ in range(50)}
    assert len(ids) == 50


def test_request_defaults_to_empty_params() -> None:
    assert new_request(Action.PING).params == {}


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):  # pydantic ValidationError
        Request(id="x", action="ping", params={}, surprise=1)  # type: ignore[call-arg]


def test_response_serialization() -> None:
    response = Response.ok("abc123", {"scene": "Scene", "objects_count": 3})
    payload = json.loads(response.to_json())

    assert payload["id"] == "abc123"
    assert payload["success"] is True
    assert payload["result"] == {"scene": "Scene", "objects_count": 3}
    assert response.raise_for_status() == {"scene": "Scene", "objects_count": 3}


def test_error_response() -> None:
    error = BlenderMCPError("Object 'Chair' does not exist", code=ErrorCode.OBJECT_NOT_FOUND)
    response = Response.fail("req-1", error)
    payload = json.loads(response.to_json())

    assert payload == {
        "id": "req-1",
        "success": False,
        "result": None,
        "error": {
            "code": "OBJECT_NOT_FOUND",
            "message": "Object 'Chair' does not exist",
            "details": {},
        },
    }


def test_raise_for_status_turns_an_error_into_an_exception() -> None:
    response = Response.fail(
        "req-2", BlenderMCPError("nope", code=ErrorCode.INVALID_PARAMETER, details={"a": 1})
    )
    with pytest.raises(BlenderMCPError) as excinfo:
        response.raise_for_status()

    assert excinfo.value.code is ErrorCode.INVALID_PARAMETER
    assert excinfo.value.message == "nope"
    assert excinfo.value.details == {"a": 1}


def test_raise_for_status_without_a_result_returns_an_empty_dict() -> None:
    assert Response.ok("req-3").raise_for_status() == {}


def test_parse_request_round_trip() -> None:
    original = Request(id="1", action=Action.DELETE_OBJECT.value, params={"name": "Cube"})
    assert parse_request(original.model_dump_json()) == original


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '{"action": "ping"}', '{"id": "", "action": "ping"}', '{"id": 1, "action": "ping"}'],
)
def test_parse_request_rejects_malformed_frames(raw: str) -> None:
    with pytest.raises(BlenderMCPError) as excinfo:
        parse_request(raw)
    assert excinfo.value.code is ErrorCode.MALFORMED_MESSAGE


def test_parse_response_rejects_malformed_frames() -> None:
    with pytest.raises(BlenderMCPError) as excinfo:
        parse_response("{oops")
    assert excinfo.value.code is ErrorCode.MALFORMED_MESSAGE


def test_parsed_action_rejects_an_unknown_action() -> None:
    request = Request(id="1", action="make_coffee", params={})
    with pytest.raises(BlenderMCPError) as excinfo:
        request.parsed_action()
    assert excinfo.value.code is ErrorCode.UNKNOWN_ACTION
    assert "get_scene" in excinfo.value.details["supported"]


def test_mutation_classification_covers_every_write_action() -> None:
    mutating = {action for action in Action if action.is_mutation}
    assert mutating == {
        Action.CREATE_OBJECT,
        Action.UPDATE_OBJECT,
        Action.DELETE_OBJECT,
        Action.RENDER,
        Action.EXECUTE_PYTHON,
        Action.BEGIN_TRANSACTION,
        Action.COMMIT_TRANSACTION,
        Action.ROLLBACK_TRANSACTION,
    }
    assert not Action.GET_SCENE.is_mutation


def test_error_info_round_trips() -> None:
    info = ErrorInfo(code="BLENDER_OPERATION_FAILED", message="boom")
    assert ErrorInfo.model_validate(info.model_dump()) == info
