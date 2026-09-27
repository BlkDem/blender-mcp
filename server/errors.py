"""Structured errors shared by the MCP tools, the WebSocket bridge and the add-on.

Every failure the AI can see is one of these. The wire shape is always::

    {"success": false, "error": {"code": "OBJECT_NOT_FOUND", "message": "...",
                                 "details": {...}}}

so the model can branch on ``error.code`` instead of parsing prose.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    These are the public contract: a model branches on ``error.code``, so the
    names are specific about *what* failed rather than generic. ``BLENDER_*`` is
    anything Blender itself refused, ``PYTHON_*`` is the ``execute_python``
    path, and the transport codes cover the WebSocket bridge.
    """

    # Object operations
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
    OBJECT_ALREADY_EXISTS = "OBJECT_ALREADY_EXISTS"
    INVALID_OBJECT_TYPE = "INVALID_OBJECT_TYPE"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    # Blender itself
    BLENDER_NOT_CONNECTED = "BLENDER_NOT_CONNECTED"
    BLENDER_OPERATION_FAILED = "BLENDER_OPERATION_FAILED"
    TIMEOUT = "TIMEOUT"
    # execute_python
    PYTHON_EXECUTION_DISABLED = "PYTHON_EXECUTION_DISABLED"
    PYTHON_EXECUTION_ERROR = "PYTHON_EXECUTION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # Transport and the add-on
    CONNECTION_LOST = "CONNECTION_LOST"
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    TRANSACTION_ACTIVE = "TRANSACTION_ACTIVE"
    TRANSACTION_NOT_ACTIVE = "TRANSACTION_NOT_ACTIVE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BlenderMCPError(Exception):
    """Base class for every error the AI is allowed to see.

    Subclasses only set :attr:`code` and :attr:`default_message`; there is no
    behaviour to override, so ``BlenderMCPError(message, code=...)`` is
    equivalent and reconstruction from the wire never needs a lookup table.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_message: str = "Unexpected error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error

    def to_payload(self) -> dict[str, Any]:
        return {"success": False, "error": self.to_dict()}

    def to_tool_message(self) -> str:
        """Compact single-line form used as the MCP tool error text."""
        return json.dumps(self.to_payload(), separators=(",", ":"))


def error_from_dict(payload: dict[str, Any]) -> BlenderMCPError:
    """Rebuild an exception from an ``error`` object received over the wire."""
    raw_code = payload.get("code")
    try:
        code = ErrorCode(raw_code) if isinstance(raw_code, str) else ErrorCode.INTERNAL_ERROR
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    message = payload.get("message")
    details = payload.get("details")
    return BlenderMCPError(
        message if isinstance(message, str) and message else None,
        code=code,
        details=details if isinstance(details, dict) else None,
    )
