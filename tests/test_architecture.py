"""Architecture tests.

The brief's central rule is that the MCP server never touches ``bpy`` and the
add-on never talks MCP. Those are structural claims, so they are checked
structurally rather than left to review: a stray ``import bpy`` in the server, or
a stray ``import mcp`` in the add-on, fails the build here.
"""

from __future__ import annotations

import ast
import py_compile
from pathlib import Path

import pytest

from tests.conftest import ADDON_DIR, ROOT

SERVER_DIR = ROOT / "server"
ADDON_PACKAGE = ADDON_DIR / "blender_mcp"

#: Add-on modules that must stay importable without Blender on the path.
BLENDER_FREE_ADDON_MODULES = ("protocol.py", "websocket.py")


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


# --- the server half must never import bpy -----------------------------------


@pytest.mark.parametrize("path", python_files(SERVER_DIR), ids=lambda p: p.name)
def test_the_server_never_imports_bpy(path: Path) -> None:
    assert "bpy" not in imported_modules(path), f"{path} imports bpy"
    assert "mathutils" not in imported_modules(path), f"{path} imports mathutils"


@pytest.mark.parametrize("path", python_files(SERVER_DIR), ids=lambda p: p.name)
def test_the_server_never_imports_the_addon_package(path: Path) -> None:
    """The transport speaks JSON; the server has no in-process Blender."""
    assert "blender_mcp" not in imported_modules(path), f"{path} imports the add-on"


# --- the add-on half must never speak MCP ------------------------------------


@pytest.mark.parametrize("path", python_files(ADDON_PACKAGE), ids=lambda p: p.name)
def test_the_addon_never_imports_the_mcp_sdk(path: Path) -> None:
    for name in imported_modules(path):
        assert not name.startswith("mcp"), f"{path} imports {name}"
        assert not name.startswith("pydantic"), f"{path} imports {name}"
        assert not name.startswith("server."), f"{path} imports {name}"


# --- every file has to at least parse ----------------------------------------


@pytest.mark.parametrize(
    "path",
    python_files(SERVER_DIR) + python_files(ADDON_PACKAGE) + python_files(ROOT / "examples"),
    ids=lambda p: p.name,
)
def test_every_module_compiles(path: Path, tmp_path: Path) -> None:
    """Byte-compiles the ``bpy``-importing modules that pytest cannot import."""
    target = tmp_path / f"{path.stem}.pyc"
    py_compile.compile(str(path), cfile=str(target), doraise=True)


# --- the add-on's portable modules must stay portable ------------------------


@pytest.mark.parametrize("name", BLENDER_FREE_ADDON_MODULES)
def test_blender_free_modules_import_without_bpy(name: str) -> None:
    import importlib

    module = importlib.import_module(f"blender_mcp.{name.removesuffix('.py')}")
    assert "bpy" not in imported_modules(Path(module.__file__ or ""))


def test_only_the_expected_addon_modules_import_bpy() -> None:
    """``__init__.py`` is bpy-free on purpose: it defers its imports to register()."""
    importing = {path.name for path in python_files(ADDON_PACKAGE) if "bpy" in imported_modules(path)}
    assert importing == {"connection.py", "executor.py", "operators.py", "ui.py"}


def test_the_addon_declares_bl_info() -> None:
    import blender_mcp

    assert set(blender_mcp.bl_info) >= {"name", "version", "blender", "category"}
    assert blender_mcp.bl_info["name"] == "Blender MCP"
