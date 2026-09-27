"""Shared fixtures.

Two things need setting up before any test runs: the repository root (so
``import server`` works) and the add-on directory (so ``import blender_mcp``
works, for the two add-on modules that do not need a real Blender).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = ROOT / "addon"

for path in (str(ROOT), str(ADDON_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from server.config import Settings  # noqa: E402
from tests.support.stubs import FakeBridge, FakeContext  # noqa: E402

#: The Blender Python module segfaults while tearing itself down at interpreter
#: exit, after pytest has already reported the results. Exiting hard, with
#: pytest's own status, keeps the exit code honest instead of turning a green run
#: into 139.
_BPY_IMPORTABLE = (
    importlib.util.find_spec("bpy") is not None and os.environ.get("BLENDER_MCP_SKIP_BPY") != "1"
)
_exit_status = 0


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _exit_status
    _exit_status = int(exitstatus)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _BPY_IMPORTABLE:
        # stdout is block-buffered when piped, and os._exit does not flush.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_exit_status)


#: Every test module that has async tests declares ``pytestmark =
#: pytest.mark.anyio``. anyio ships with the MCP SDK, so the suite needs exactly
#: one async plugin rather than anyio and pytest-asyncio competing over the same
#: tests.


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(blender_host="127.0.0.1", blender_port=8765, blender_request_timeout=5.0)


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture
def ctx(bridge: FakeBridge, settings: Settings) -> FakeContext:
    """A tool context whose Python execution is off, the safe default."""
    return FakeContext(bridge, settings)


@pytest.fixture
def python_settings(settings: Settings) -> Settings:
    """The same settings with ``ALLOW_PYTHON_EXECUTION`` turned on."""
    return settings.model_copy(update={"allow_python_execution": True})


@pytest.fixture
def python_ctx(bridge: FakeBridge, python_settings: Settings) -> FakeContext:
    """A tool context where ``blender.execute_python`` is permitted."""
    return FakeContext(bridge, python_settings)
