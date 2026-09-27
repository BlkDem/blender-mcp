"""Glue between MCP tool signatures and the application context.

The server's lifespan publishes an :class:`AppContext` holding the running
:class:`~server.blender.connection.BlenderBridge` and the
:class:`~server.config.Settings` the process was started with. Tools read both
from the SDK's ``Context``, which means:

* no module-level mutable state, and
* a test can hand a tool a context with a stub bridge and explicit settings.

:func:`register` also wraps every tool with the request log, so no individual
tool has to remember to log itself. That is what keeps a tool's failure path
down to one line of ``raise tool_error(exc)``.

(The SDK does not inject a ``Context`` into *static* resources, so the resource
handlers close over the bridge instead — see :mod:`server.mcp.resources`.)
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from server.blender.connection import BlenderBridge
from server.config import Settings
from server.errors import BlenderMCPError, ErrorCode

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class AppContext:
    """What the lifespan hands to every tool and resource."""

    settings: Settings
    bridge: BlenderBridge


def _app_context(ctx: Context) -> AppContext:
    context = ctx.request_context.lifespan_context
    if not isinstance(context, AppContext):  # pragma: no cover - wiring guard
        raise BlenderMCPError("MCP server lifespan did not provide an application context")
    return context


def bridge_of(ctx: Context) -> BlenderBridge:
    """The bridge to the connected Blender."""
    return _app_context(ctx).bridge


def settings_of(ctx: Context) -> Settings:
    """The settings this server was started with."""
    return _app_context(ctx).settings


def tool_error(exc: BlenderMCPError) -> ToolError:
    """Wrap a structured error so the model sees ``is_error`` *and* the code.

    The SDK prefixes the text with ``Error executing tool <name>:`` and marks the
    call failed; the JSON body after it keeps ``error.code`` machine-readable.

    The code is also attached to the exception, because ``ToolError`` itself
    carries only a message: without this the request log would report
    ``error=ToolError`` for the most common failure there is.
    """
    error = ToolError(exc.to_tool_message())
    error.code = exc.code  # type: ignore[attr-defined]
    return error


def _context_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Context | None:
    """The tool's ``Context`` argument, whatever it is called.

    Duck-typed on ``request_context`` rather than checked with ``isinstance``:
    the real class is generic and version-specific, and a test's stub context
    would otherwise be treated as "no context" and lose its request id.
    """
    for candidate in (*args[:1], kwargs.get("ctx")):
        if hasattr(candidate, "request_context"):
            return cast("Context", candidate)
    return None


def _request_id(ctx: Context | None) -> str:
    request_id = getattr(getattr(ctx, "request_context", None), "request_id", None)
    return str(request_id) if request_id is not None else "-"


def _instrument(name: str, fn: F) -> F:
    """Wrap a tool so every call is logged with its id, duration and outcome.

    One line per call, in a fixed shape, so a log can be grepped or shipped
    anywhere without a parser::

        tool=blender.create_object request=7 duration_ms=42 success=true

    Failures keep the code, which is what makes the log useful for triage: a
    TIMEOUT and a BLENDER_OPERATION_FAILED need very different responses. The
    traceback stays where it already is, in the underlying log record.
    """

    def report(started: float, ctx: Context | None, result_success: bool, exc: BaseException | None) -> None:
        _log_outcome(name, started, ctx, success=result_success, exc=exc)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            ctx = _context_of(args, kwargs)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                report(started, ctx, False, exc)
                raise
            report(started, ctx, True, None)
            return result

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        ctx = _context_of(args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            report(started, ctx, False, exc)
            raise
        report(started, ctx, True, None)
        return result

    return sync_wrapper  # type: ignore[return-value]


def _log_outcome(
    name: str,
    started: float,
    ctx: Context | None,
    *,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    duration = (time.perf_counter() - started) * 1000
    fields: dict[str, Any] = {
        "tool": name,
        "request": _request_id(ctx),
        "duration_ms": round(duration, 1),
        "success": success,
    }
    if exc is None:
        logger.info("mcp %s", _format(fields))
        return

    # tool_error() attaches the code to the ToolError it raises, so the common
    # "expected failure" path is reportable by code rather than by class name.
    code = getattr(exc, "code", None)
    expected = isinstance(code, ErrorCode)
    fields["error"] = code.value if isinstance(code, ErrorCode) else type(exc).__name__
    # An expected failure is a normal outcome, not a crash: INFO with no
    # traceback. ERROR and the traceback stay for what nobody anticipated.
    logger.info("mcp %s", _format(fields), exc_info=None if expected else exc)


def _format(fields: dict[str, Any]) -> str:
    """``key=value`` pairs, one line, greppable.

    Booleans are spelled ``true``/``false`` rather than Python's ``True`` so the
    line matches the JSON-shaped payloads the model sees and can be matched with
    a plain ``success=true`` search.
    """
    return " ".join(
        f"{key}={str(value).lower() if isinstance(value, bool) else value}" for key, value in fields.items()
    )


def register(server: MCPServer, fn: F, name: str, description: str) -> None:
    """Register a tool under an explicit, dotted name, with request logging.

    ``functools.wraps`` keeps the wrapper's signature identical to the original,
    which is what the SDK reads to build the tool's JSON schema.
    """
    server.add_tool(_instrument(name, fn), name=name, description=description, structured_output=True)
