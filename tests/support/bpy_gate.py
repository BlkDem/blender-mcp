"""Gate for the tests that need a real Blender.

Kept in one place so ``BLENDER_MCP_SKIP_BPY=1`` behaves the same in every module
that needs it, and so a module-level skip never turns into a collection error.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

SKIP_ENV_VAR = "BLENDER_MCP_SKIP_BPY"


def require_bpy() -> Any:
    """Return the real ``bpy`` module, or skip the whole module.

    Install it with ``pip install bpy==<version>``; the version must match your
    Python. Blender 4.2 is the 3.11 build.
    """
    if os.environ.get(SKIP_ENV_VAR) == "1":
        pytest.skip(f"{SKIP_ENV_VAR}=1: skipping the real-Blender tests", allow_module_level=True)
    return pytest.importorskip("bpy", reason="the real bpy module is not installed")
