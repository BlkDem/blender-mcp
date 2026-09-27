"""Live smoke test: spawn the real server over stdio and talk to it.

Everything else runs in-process. This one starts ``python -m server.main`` as a
subprocess exactly the way an AI client would, connects a Blender double to the
bridge it opens, and walks the acceptance list from the README. If the entry
point, the stdio transport, the bridge lifecycle or the logging setup are broken,
this is the test that says so.
"""

from __future__ import annotations

import json
import os
import random
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.conftest import ROOT
from tests.support.fake_blender import FakeBlender

pytestmark = pytest.mark.anyio

_PORT_BASE = 26000 + random.randint(0, 2000)
_next_port = iter(range(_PORT_BASE, _PORT_BASE + 500))


def free_port() -> int:
    return next(_next_port)


async def wait_for_blender(blender: FakeBlender, port: int, session: ClientSession) -> None:
    blender.connect("127.0.0.1", port)
    for _ in range(250):
        result = await session.call_tool("blender.get_scene", {})
        if not result.is_error:
            return
        await _sleep(0.02)
    raise AssertionError("the Blender double never attached to the subprocess bridge")


async def _sleep(seconds: float) -> None:
    import anyio

    await anyio.sleep(seconds)


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("BLENDER_MCP_SKIP_SUBPROCESS") == "1",
    reason="subprocess smoke test disabled by BLENDER_MCP_SKIP_SUBPROCESS",
)
async def test_the_installed_entry_point_serves_mcp_over_stdio(
    anyio_backend: str,
) -> None:  # noqa: ARG001 - anyio fixture
    port = free_port()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "server.main"],
        cwd=str(ROOT),
        env={
            **os.environ,
            "BLENDER_HOST": "127.0.0.1",
            "BLENDER_PORT": str(port),
            "ALLOW_PYTHON_EXECUTION": "true",
            "LOG_LEVEL": "WARNING",
        },
    )
    blender = FakeBlender()
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await wait_for_blender(blender, port, session)

                created = await session.call_tool(
                    "blender.create_object",
                    {"type": "cube", "name": "SmokeBox", "location": [0, 0, 1]},
                )
                assert not created.is_error
                assert created.structured_content["object"]["name"] == "SmokeBox"

                scene = await session.call_tool("blender.get_scene", {})
                assert scene.structured_content["objects_total"] == 1

                read_result = await session.read_resource("blender://objects")
                assert json.loads(read_result.contents[0].text)["objects"][0]["name"] == "SmokeBox"
    finally:
        blender.close()
