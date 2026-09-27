"""Attach a headless Blender to the bridge, for scripted end-to-end runs.

``blender --background --python examples/blender_attach.py -- --port 8765``

Blender has no event loop in background mode, so ``bpy.app.timers`` never fires
and a connected add-on would sit there with its queue full. This script calls
:meth:`BlenderConnection.drain` on a loop instead — the same function the GUI's
timer calls, on the same main thread.

In the GUI you do not need this: the add-on's own panel drives the pump. This
exists so the full stack can be exercised from a script or from CI::

    python examples/mcp_acceptance.py --blender-arg=blender \\
        --blender-arg=--background \\
        --blender-arg=--python \\
        --blender-arg=examples/blender_attach.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Blender runs this script, so `bpy` is importable here by definition; importing
# it is also what proves the script is running inside Blender and not a plain
# Python process.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_connection():
    """Import the add-on, preferring the copy Blender has installed.

    A launch script that silently puts the working tree first would happily run
    against code that differs from the one the user's Preferences installed, and
    the resulting bug report would be about the wrong version. So: try the
    installed package, and only fall back to the sibling ``addon/`` directory.
    """
    try:
        from blender_mcp.connection import BlenderConnection

        print(f"[attach] using the installed add-on: {BlenderConnection.__module__}", flush=True)
        return BlenderConnection
    except ImportError:
        path = os.path.join(_REPO, "addon")
        sys.path.insert(0, path)
        from blender_mcp.connection import BlenderConnection

        print(f"[attach] add-on not installed; using {path}", flush=True)
        return BlenderConnection


#: How long to keep serving before quitting, so a forgotten run cannot linger.
DEFAULT_LIFETIME = 600.0


def arguments() -> argparse.Namespace:
    """Options from the environment, overridden by anything after Blender's ``--``.

    Environment first, for the same reason as ``gui_blender_demo``: a launcher
    that sets ``BLENDER_ATTACH_PORT`` needs no ``--`` separator threaded through
    a second argument parser.
    """
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="blender_attach")
    parser.add_argument("--host", default=os.environ.get("BLENDER_ATTACH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_ATTACH_PORT", "8765")))
    parser.add_argument(
        "--seconds", type=float, default=float(os.environ.get("BLENDER_ATTACH_SECONDS", DEFAULT_LIFETIME))
    )
    return parser.parse_args(argv)


def main() -> int:
    import bpy  # noqa: PLC0415 - only meaningful inside Blender

    options = arguments()

    BlenderConnection = _import_connection()
    connection = BlenderConnection(options.host, options.port)
    connection.connect()
    print(
        f"[attach] Blender {bpy.app.version_string} (background={bpy.app.background}) "
        f"-> ws://{options.host}:{options.port}",
        flush=True,
    )

    deadline = time.monotonic() + options.seconds
    while time.monotonic() < deadline:
        # The timer would do this in a GUI; background mode has no event loop.
        if connection.drain() is None and not connection.is_connected:
            if connection.info()["last_error"] is None:
                break
        time.sleep(0.01)

    print(f"[attach] final state: {connection.info()}", flush=True)
    connection.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
