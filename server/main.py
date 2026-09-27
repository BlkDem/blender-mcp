"""Entry point: ``python -m server.main``.

Runs the MCP server over stdio (what AI clients spawn) with the WebSocket bridge
to Blender started in the same process. stdout is reserved for the MCP protocol;
every log line goes to stderr.
"""

from __future__ import annotations

import logging
import sys

from server.config import get_settings, setup_logging
from server.mcp.server import create_server

logger = logging.getLogger(__name__)


def main() -> int:
    """Start the MCP server. Returns a process exit code."""
    settings = get_settings()
    setup_logging(settings)
    logger.info(
        "Starting blender-mcp (transport=%s, bridge=ws://%s:%s)",
        settings.mcp_transport,
        settings.blender_host,
        settings.blender_port,
    )
    if not settings.allow_python_execution:
        logger.info("blender.execute_python is disabled (ALLOW_PYTHON_EXECUTION=false)")

    server = create_server(settings)
    try:
        if settings.mcp_transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http", host=settings.mcp_host, port=settings.mcp_port)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.info("Interrupted, shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
