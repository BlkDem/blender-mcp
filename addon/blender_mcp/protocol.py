"""JSON protocol spoken with the MCP server, standard library only.

This mirrors :mod:`server.blender.protocol` field for field. It is duplicated
rather than imported because the add-on runs inside Blender's own Python, which
has neither pydantic nor this package on its path. ``tests/test_addon_protocol.py``
asserts the two definitions stay in step.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

PROTOCOL_VERSION = 1

# --- actions ----------------------------------------------------------------
GET_SCENE = "get_scene"
GET_OBJECTS = "get_objects"
GET_OBJECT = "get_object"
PING = "ping"
CREATE_OBJECT = "create_object"
UPDATE_OBJECT = "update_object"
DELETE_OBJECT = "delete_object"
RENDER = "render"
EXECUTE_PYTHON = "execute_python"
BEGIN_TRANSACTION = "begin_transaction"
COMMIT_TRANSACTION = "commit_transaction"
ROLLBACK_TRANSACTION = "rollback_transaction"

#: Actions that change the scene and therefore push an undo step.
MUTATING_ACTIONS = frozenset(
    {
        CREATE_OBJECT,
        UPDATE_OBJECT,
        DELETE_OBJECT,
        RENDER,
        EXECUTE_PYTHON,
        BEGIN_TRANSACTION,
        COMMIT_TRANSACTION,
        ROLLBACK_TRANSACTION,
    }
)

# --- error codes ------------------------------------------------------------
OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
OBJECT_ALREADY_EXISTS = "OBJECT_ALREADY_EXISTS"
INVALID_OBJECT_TYPE = "INVALID_OBJECT_TYPE"
INVALID_PARAMETER = "INVALID_PARAMETER"
BLENDER_OPERATION_FAILED = "BLENDER_OPERATION_FAILED"
PYTHON_EXECUTION_ERROR = "PYTHON_EXECUTION_ERROR"
VALIDATION_ERROR = "VALIDATION_ERROR"
TIMEOUT = "TIMEOUT"
BLENDER_NOT_CONNECTED = "BLENDER_NOT_CONNECTED"
PYTHON_EXECUTION_DISABLED = "PYTHON_EXECUTION_DISABLED"
CONNECTION_LOST = "CONNECTION_LOST"
MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
UNKNOWN_ACTION = "UNKNOWN_ACTION"
TRANSACTION_ACTIVE = "TRANSACTION_ACTIVE"
TRANSACTION_NOT_ACTIVE = "TRANSACTION_NOT_ACTIVE"
INTERNAL_ERROR = "INTERNAL_ERROR"

ERROR_CODES = frozenset(
    {
        OBJECT_NOT_FOUND,
        OBJECT_ALREADY_EXISTS,
        INVALID_OBJECT_TYPE,
        INVALID_PARAMETER,
        BLENDER_OPERATION_FAILED,
        PYTHON_EXECUTION_ERROR,
        VALIDATION_ERROR,
        TIMEOUT,
        BLENDER_NOT_CONNECTED,
        PYTHON_EXECUTION_DISABLED,
        CONNECTION_LOST,
        MALFORMED_MESSAGE,
        UNKNOWN_ACTION,
        TRANSACTION_ACTIVE,
        TRANSACTION_NOT_ACTIVE,
        INTERNAL_ERROR,
    }
)


class ProtocolError(Exception):
    """A frame arrived that this add-on cannot interpret."""

    code = MALFORMED_MESSAGE

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ActionError(Exception):
    """An action was rejected, with a code the MCP server understands."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


def new_request_id() -> str:
    return uuid.uuid4().hex


def encode_response(request_id: str, result: dict[str, Any] | None = None, *, success: bool = True) -> str:
    """Serialise a success envelope."""
    return json.dumps({"id": request_id, "success": success, "result": result or {}})


def encode_error(request_id: str, code: str, message: str, details: dict[str, Any] | None = None) -> str:
    """Serialise an error envelope."""
    return json.dumps(
        {
            "id": request_id,
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        }
    )


def encode_exception(request_id: str, exc: BaseException) -> str:
    """Serialise any exception as a BLENDER_OPERATION_FAILED envelope.

    The traceback stays in Blender's log; the message the AI sees is the
    exception text, which is what actually helps it recover.
    """
    if isinstance(exc, ActionError):
        return encode_error(request_id, exc.code, exc.message, exc.details)
    return encode_error(request_id, BLENDER_OPERATION_FAILED, f"{type(exc).__name__}: {exc}")


def parse_request(raw: str) -> tuple[str, str, dict[str, Any]]:
    """Validate an inbound frame, returning ``(id, action, params)``.

    ``method`` is accepted as a synonym for ``action`` so a client written
    against the other common JSON-RPC-ish spelling interoperates; ``action``
    wins when both are present, because that is what this project emits.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("frame is not a JSON object")

    request_id = payload.get("id")
    action = payload.get("action") or payload.get("method")
    params = payload.get("params", {})
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("missing or invalid 'id'")
    if not isinstance(action, str) or not action:
        raise ProtocolError("missing or invalid 'action'")
    if not isinstance(params, dict):
        raise ProtocolError("'params' must be an object")
    return request_id, action, params
