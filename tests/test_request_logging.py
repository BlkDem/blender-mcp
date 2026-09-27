"""Tests for the per-request log that :func:`server.mcp.support.register` adds.

One line per MCP call, in a fixed shape. This is what an operator reads when a
model reports that a tool misbehaved, so the guarantees worth testing are that
every call produces exactly one line, carrying the id, the duration, the outcome
and the error code, and that registering a tool wires the log in at all.
"""

from __future__ import annotations

import inspect
import logging
import re

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server.blender.protocol import Action
from server.errors import BlenderMCPError, ErrorCode
from server.mcp.support import _instrument, tool_error
from server.mcp.tools import scene
from tests.support.stubs import FakeBridge, FakeContext

pytestmark = pytest.mark.anyio

LINE = re.compile(
    r"tool=(?P<tool>\S+) request=(?P<request>\S+) duration_ms=(?P<duration>[\d.]+) "
    r"success=(?P<success>true|false)(?: error=(?P<error>\S+))?"
)


@pytest.fixture
def records(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """caplog.records is a property, so hand back the fixture and read it per test."""
    caplog.set_level(logging.INFO, logger="server.mcp.support")
    return caplog


def logged_lines(records: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in records if record.getMessage().startswith("mcp ")]


def instrument(fn, name: str):
    """The same wrapping :func:`register` applies, for a direct call."""
    return _instrument(name, fn)


# --- the wrapper itself -------------------------------------------------------


async def test_a_successful_call_logs_one_line(
    ctx: FakeContext, bridge: FakeBridge, records: pytest.LogCaptureFixture
) -> None:
    bridge.results[Action.GET_SCENE] = {"scene": "Scene"}
    await instrument(scene.get_scene, "blender.get_scene")(ctx)

    lines = logged_lines(records.records)
    assert len(lines) == 1
    match = LINE.search(lines[0].getMessage())
    assert match is not None
    assert match["tool"] == "blender.get_scene"
    assert match["request"] == "1"
    assert match["success"] == "true"
    assert match["error"] is None
    assert float(match["duration"]) >= 0.0
    assert lines[0].levelno == logging.INFO


async def test_a_failed_call_logs_the_error_code(
    ctx: FakeContext, bridge: FakeBridge, records: pytest.LogCaptureFixture
) -> None:
    bridge.raise_for[Action.GET_OBJECT] = BlenderMCPError(
        "Object 'Chair' does not exist", code=ErrorCode.OBJECT_NOT_FOUND
    )
    with pytest.raises(ToolError):
        await instrument(scene.get_object, "blender.get_object")(ctx, name="Chair")

    lines = logged_lines(records.records)
    assert len(lines) == 1
    match = LINE.search(lines[0].getMessage())
    assert match is not None
    assert match["success"] == "false"
    assert match["error"] == "OBJECT_NOT_FOUND"
    # An expected failure is a normal outcome: no traceback, no ERROR level.
    assert lines[0].levelno == logging.INFO
    assert lines[0].exc_info is None


async def test_an_unexpected_failure_keeps_its_traceback(
    ctx: FakeContext, bridge: FakeBridge, records: pytest.LogCaptureFixture
) -> None:
    bridge.raise_for[Action.GET_SCENE] = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await instrument(scene.get_scene, "blender.get_scene")(ctx)

    match = LINE.search(records.records[-1].getMessage())
    assert match is not None
    assert match["error"] == "RuntimeError"
    assert records.records[-1].exc_info is not None


def test_a_synchronous_tool_is_instrumented_too(records: pytest.LogCaptureFixture) -> None:
    def sample() -> str:
        return "ok"

    assert instrument(sample, "blender.sync")() == "ok"
    assert LINE.search(records.records[-1].getMessage())["tool"] == "blender.sync"


def test_a_synchronous_failure_is_logged(records: pytest.LogCaptureFixture) -> None:
    def sample() -> str:
        raise tool_error(BlenderMCPError("nope", code=ErrorCode.INVALID_PARAMETER))

    with pytest.raises(ToolError):
        instrument(sample, "blender.sync")()
    match = LINE.search(records.records[-1].getMessage())
    assert match["success"] == "false"
    assert match["error"] == "INVALID_PARAMETER"


def test_the_wrapper_keeps_the_signature_so_schemas_stay_correct() -> None:
    """The SDK reads the signature through functools.wraps; if that broke, every
    registered tool would silently lose its input schema."""

    def sample(ctx: FakeContext, name: str, count: int = 1) -> dict:
        """A docstring."""
        return {}

    wrapped = instrument(sample, "blender.sample")
    assert wrapped.__name__ == "sample"
    assert wrapped.__doc__ == "A docstring."
    assert list(inspect.signature(wrapped).parameters) == ["ctx", "name", "count"]


def test_a_tool_without_a_context_still_logs(records: pytest.LogCaptureFixture) -> None:
    def sample() -> None:
        return None

    instrument(sample, "blender.no_context")()
    match = LINE.search(records.records[-1].getMessage())
    assert match is not None
    assert match["request"] == "-", "a missing request id must show up, not break the log"
