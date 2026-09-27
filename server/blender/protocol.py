"""Transport between the MCP server and the Blender add-on.

Nothing in this module knows about MCP or about ``bpy``: it is a plain JSON
request/response protocol over a WebSocket, so it can be tested and reasoned
about on its own.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from server.errors import BlenderMCPError, ErrorCode, error_from_dict


class Action(StrEnum):
    """Every operation the add-on knows how to perform.

    Read actions are pure. Mutating actions push a Blender undo step, which is
    what makes :mod:`server.transactions` able to roll them back.
    """

    # reads
    GET_SCENE = "get_scene"
    GET_OBJECTS = "get_objects"
    GET_OBJECT = "get_object"
    PING = "ping"
    # mutations
    CREATE_OBJECT = "create_object"
    UPDATE_OBJECT = "update_object"
    DELETE_OBJECT = "delete_object"
    RENDER = "render"
    EXECUTE_PYTHON = "execute_python"
    # undo grouping
    BEGIN_TRANSACTION = "begin_transaction"
    COMMIT_TRANSACTION = "commit_transaction"
    ROLLBACK_TRANSACTION = "rollback_transaction"

    @property
    def is_mutation(self) -> bool:
        return self in _MUTATING_ACTIONS


_MUTATING_ACTIONS = frozenset(
    {
        Action.CREATE_OBJECT,
        Action.UPDATE_OBJECT,
        Action.DELETE_OBJECT,
        Action.RENDER,
        Action.EXECUTE_PYTHON,
        Action.BEGIN_TRANSACTION,
        Action.COMMIT_TRANSACTION,
        Action.ROLLBACK_TRANSACTION,
    }
)


class Request(BaseModel):
    """Server -> add-on. Exactly one response is sent per request id."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Unique request id, echoed in the response.")
    action: str = Field(description="One of the Action values.")
    params: dict[str, Any] = Field(default_factory=dict)

    def parsed_action(self) -> Action:
        try:
            return Action(self.action)
        except ValueError as exc:
            raise BlenderMCPError(
                f"Unknown action '{self.action}'",
                code=ErrorCode.UNKNOWN_ACTION,
                details={"action": self.action, "supported": [a.value for a in Action]},
            ) from exc


class ErrorInfo(BaseModel):
    """Structured error payload, mirrored 1:1 by the add-on."""

    code: str = Field(description="One of the documented error codes.")
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """Add-on -> server."""

    model_config = ConfigDict(extra="forbid")

    id: str
    success: bool = True
    result: dict[str, Any] | None = None
    error: ErrorInfo | None = None

    def raise_for_status(self) -> dict[str, Any]:
        """Return the result, or raise :class:`BlenderMCPError`."""
        if self.error is not None:
            raise error_from_dict(self.error.model_dump())
        if not self.success:
            raise BlenderMCPError(code=ErrorCode.INTERNAL_ERROR)
        return self.result or {}

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def ok(cls, request_id: str, result: dict[str, Any] | None = None) -> Response:
        return cls(id=request_id, success=True, result=result or {})

    @classmethod
    def fail(cls, request_id: str, error: BlenderMCPError) -> Response:
        return cls(
            id=request_id,
            success=False,
            error=ErrorInfo(code=error.code.value, message=error.message, details=error.details),
        )


def new_request(action: Action, **params: Any) -> Request:
    """Build a request with a fresh id."""
    return Request(id=uuid.uuid4().hex, action=action.value, params=params)


def parse_request(raw: str) -> Request:
    """Parse an inbound frame, raising :class:`BlenderMCPError` on garbage."""
    try:
        return Request.model_validate_json(raw)
    except Exception as exc:  # pydantic ValidationError, json.JSONDecodeError, ...
        raise BlenderMCPError(f"Malformed request: {exc}", code=ErrorCode.MALFORMED_MESSAGE) from exc


def parse_response(raw: str) -> Response:
    """Parse an inbound response frame, raising on garbage."""
    try:
        return Response.model_validate_json(raw)
    except Exception as exc:
        raise BlenderMCPError(f"Malformed response: {exc}", code=ErrorCode.MALFORMED_MESSAGE) from exc


def encode(payload: BaseModel) -> str:
    return payload.model_dump_json()
