"""``blender.wait_for_change``: block until the scene changes, or time out.

A model that has just moved something usually wants to know whether it worked
before doing it again. Polling ``get_scene`` to find out wastes a round trip and
fills the context with a payload it does not need; this waits instead.

Why a long poll and not an MCP notification: this SDK can only publish
``notifications/resources/updated`` from inside a request, through
``Context.notify_resource_updated``. An unsolicited push from the bridge, while
no request is in flight, has no supported path today. The wait semantics a client
wants are the same either way, and the change log this reads is what a
notification would carry — so when the SDK grows a server-side notify, only this
tool changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import ContentBlock, TextContent

from server.blender.protocol import Action
from server.errors import BlenderMCPError
from server.mcp.support import bridge_of, register, tool_error

logger = logging.getLogger(__name__)

#: Longest a client may hold a request open here. A minute covers a human
#: clicking around, and keeps a forgotten call from sitting in the client's list.
MAX_WAIT_SECONDS = 60.0

#: How often to ask Blender whether anything changed. Short enough to feel
#: immediate, long enough not to flood the bridge.
POLL_SECONDS = 0.1

WAIT_DESCRIPTION = """\
Wait until the scene changes, or until `timeout` seconds pass.

Use it after a change you are unsure about, instead of reading the whole scene
again: it returns as soon as Blender reports a change, and tells you what changed.

timeout: seconds to wait, 0-60, default 10.
objects: optional object names to narrow the answer to. Omit to watch everything.
include_image: set true to also return the last render, if there is one, so a
              wait-and-look step is a single call.

Returns {"changed": bool, "changes": int, "waited": seconds, "changed_objects": [...]}.
`changed` false means nothing happened before the timeout, which is an answer
rather than a failure.\
"""


async def wait_for_change(
    ctx: Context,
    timeout: float = 10.0,
    objects: list[str] | None = None,
    include_image: bool = False,
) -> list[ContentBlock]:
    """Block until the scene changes, then report what changed."""
    from server.mcp.tools.preview import read_image

    bridge = bridge_of(ctx)
    try:
        before = await bridge.request(Action.CHANGE_COUNT)
    except BlenderMCPError as exc:
        raise tool_error(exc) from exc
    baseline = int(before.get("count", 0))

    wait_for = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
    loop = asyncio.get_running_loop()
    started = loop.time()
    wanted = set(objects) if objects else None
    names: list[str] = []
    matching: list[str] = []
    report: dict[str, Any] = {}

    while True:
        if loop.time() - started >= wait_for:
            break  # a timeout of zero should not cost a poll interval
        await asyncio.sleep(POLL_SECONDS)
        try:
            report = await bridge.request(Action.CHANGES, {"since": baseline})
        except BlenderMCPError as exc:
            raise tool_error(exc) from exc
        entries = report.get("changes", [])
        names = sorted({name for entry in entries for name in entry.get("names", [])})
        matching = [name for name in names if wanted is None or name in wanted]
        if matching or (wanted is None and entries):
            break
        if loop.time() - started >= wait_for:
            break

    payload: dict[str, Any] = {
        "changed": bool(names if wanted is None else matching),
        "changes": int(report.get("count", 0)) - baseline,
        "waited": round(loop.time() - started, 2),
        "changed_objects": matching if wanted is not None else names,
        "watched": sorted(wanted) if wanted is not None else "scene",
    }
    if int(report.get("dropped", 0)):
        payload["note"] = f"{report['dropped']} earlier change(s) fell outside the window Blender keeps"

    blocks: list[ContentBlock] = [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if include_image and payload["changed"]:
        try:
            last = await bridge.request(Action.LAST_RENDER)
        except BlenderMCPError:
            last = {}
        image = read_image(str(last.get("output_path", ""))) if last.get("exists") else None
        if image is not None:
            blocks.append(image)
    return blocks


def register_tools(server: MCPServer, enabled: Callable[[str], bool] | None = None) -> list[str]:
    """Register the wait tool, which returns content blocks rather than JSON."""
    name = "blender.wait_for_change"
    if not (enabled or (lambda _n: True))(name):
        return [name]
    register(server, wait_for_change, name, WAIT_DESCRIPTION, structured_output=False)
    return []
