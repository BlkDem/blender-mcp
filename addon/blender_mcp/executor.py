"""Execution of model-written Python inside Blender.

Two responsibilities, deliberately kept apart:

* :func:`execute_user_code` runs the code and turns the outcome into a
  JSON-safe payload. It is a convenience, not a boundary — the real policy
  screening happens on the MCP server before the request is ever sent.
* :func:`build_namespace` decides what the code can see.

Blender's own Python already has full access to the process, so nothing here
pretends to be a sandbox. What the namespace does provide is a predictable
starting point: ``result`` is defined and empty, and a fresh dict is used per
call so state does not leak between tool calls the way it would with a bare
``exec`` in the interpreter globals.
"""

from __future__ import annotations

import json
import logging
import math
import traceback
from typing import Any

from blender_mcp import protocol
from blender_mcp.protocol import ActionError

logger = logging.getLogger(__name__)

#: Names always present, so a snippet can rely on them without importing.
BASE_NAMESPACE: dict[str, Any] = {
    "bpy": None,  # filled in per call: the live bpy module
    "math": math,
    "mathutils": None,  # filled in per call
}

#: How much of a failed snippet's traceback the AI gets.
_MAX_TRACEBACK_LINES = 12


def build_namespace() -> dict[str, Any]:
    """A fresh globals dict with the standard Blender modules available."""
    import bpy  # local import keeps this module importable outside Blender
    import mathutils  # noqa: F401 - presence is what matters

    namespace: dict[str, Any] = {"__builtins__": __builtins__}
    namespace.update({key: value for key, value in BASE_NAMESPACE.items() if value is not None})
    namespace["bpy"] = bpy
    namespace["mathutils"] = mathutils
    namespace["result"] = None
    return namespace


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion of a returned value into something JSON can carry."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:200]
        return [_jsonable(item, depth + 1) for item in items]
    if hasattr(value, "to_list") and callable(value.to_list):  # mathutils vectors/matrices
        return _jsonable(value.to_list(), depth + 1)
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        try:
            return [_jsonable(item, depth + 1) for item in list(value)[:200]]
        except TypeError:
            pass
    return f"<{type(value).__name__}>"


def _short_traceback(exc: BaseException) -> list[str]:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return [line.rstrip() for line in lines[-_MAX_TRACEBACK_LINES:]]


def execute_user_code(code: str) -> dict[str, Any]:
    """Run ``code`` and report ``result``, output or the failure.

    A snippet that raises is an :class:`ActionError` with EXECUTION_ERROR, so the
    model sees the exception type, the message and the tail of the traceback —
    enough to fix the mistake, without shipping Blender's whole stack.
    """
    namespace = build_namespace()
    try:
        exec(compile(code, "<mcp-execute-python>", "exec"), namespace)  # noqa: S102
    except ActionError:
        raise
    except BaseException as exc:  # noqa: BLE001 - model code can raise anything
        logger.warning("execute_python failed: %s", exc)
        raise ActionError(
            protocol.EXECUTION_ERROR,
            f"{type(exc).__name__}: {exc}",
            details={"traceback": _short_traceback(exc)},
        ) from exc

    value = namespace.get("result")
    payload: dict[str, Any] = {
        "success": True,
        "executed": True,
        "result": _jsonable(value),
    }
    if value is None:
        payload["note"] = "no variable named 'result' was assigned; nothing to return"
    try:
        payload["result_json"] = json.dumps(payload["result"])
    except (TypeError, ValueError):  # pragma: no cover - _jsonable already coerced
        payload["result_json"] = None
    return payload
