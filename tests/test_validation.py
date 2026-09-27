"""Tests for the Python execution policy screen."""

from __future__ import annotations

import pytest

from server.errors import BlenderMCPError, ErrorCode
from server.validation.python import BLOCKED_MODULES, validate_python

ALLOWED = [
    "import bpy\nbpy.ops.mesh.primitive_cube_add()",
    "import math\nresult = math.pi",
    "import mathutils\nresult = mathutils.Vector((1, 2, 3))",
    "obj = bpy.data.objects['Cube']\nresult = {'name': obj.name}",
    "result = [i ** 2 for i in range(10)]",
    "import json\nresult = json.dumps({'a': 1})",
    "from math import sin, cos\nresult = sin(1.0) + cos(1.0)",
    "import random\nresult = random.random()",
    "for i in range(3):\n    bpy.ops.mesh.primitive_cube_add()\nresult = 'done'",
    "def helper(x):\n    return x * 2\nresult = helper(21)",
    "# just a comment\nresult = 1",
    "obj = bpy.data.objects['Cube']\nresult = obj.type == 'MESH'",
]

BLOCKED = [
    "import os\nos.system('rm -rf /')",
    "import os\nos.system('ls')",
    "import subprocess\nsubprocess.run(['ls'])",
    "import shutil\nshutil.rmtree('/tmp/x')",
    "import socket\ns = socket.socket()",
    "import requests\nrequests.get('http://example.com')",
    "import urllib.request\nurllib.request.urlopen('http://example.com')",
    "import http\nimport urllib",
    "open('/etc/passwd').read()",
    "eval('1 + 1')",
    "exec('x = 1')",
    "result = __import__('os')",
    "compile('1', '<s>', 'eval')",
    "import multiprocessing",
    "import ctypes",
    "import pickle\nresult = pickle.loads(b'')",
    "import importlib\nimportlib.import_module('os')",
    "result = (1).__class__.__mro__[1].__subclasses__()",
    "f = open\nf('/etc/passwd')",
    "obj = bpy.context\nresult = obj.__getattribute__('__class__')",
    "import bpy\nbpy.ops.wm.quit_blender()",
    "import bpy\nbpy.ops.wm.read_homefile(use_empty=True)",
    "import builtins\nbuiltins.eval('1')",
    "import builtins\nresult = builtins.open('/etc/passwd')",
    "globals()",
    "locals()",
    "f = open\nf('/etc/passwd')",
    "import pathlib\npathlib.Path('/tmp/x').write_text('a')",
]


@pytest.mark.parametrize("code", ALLOWED)
def test_allowed_python(code: str) -> None:
    report = validate_python(code)
    assert report.allowed, report.violations
    assert not report.violations


@pytest.mark.parametrize("code", BLOCKED)
def test_blocked_python(code: str) -> None:
    report = validate_python(code)
    assert not report.allowed, f"expected a violation for: {code}"
    assert report.violations


def test_violation_reports_a_line_number() -> None:
    report = validate_python("import bpy\nresult = 1\nimport os\n")
    assert report.violations == ["line 3: import of blocked module 'os'"]


def test_blocked_attribute_access_is_reported() -> None:
    report = validate_python("result = f.__globals__")
    assert report.violations == ["line 1: use of blocked attribute '__globals__'"]


def test_report_lists_imports() -> None:
    report = validate_python("import bpy\nfrom mathutils import Vector\n")
    assert set(report.imports) == {"bpy", "mathutils"}


def test_line_count_is_reported() -> None:
    assert validate_python("result = 1").line_count == 1
    assert validate_python("result = 1\nresult = 2").line_count == 2


@pytest.mark.parametrize("code", ["", "   ", "\n\n"])
def test_empty_code_is_rejected(code: str) -> None:
    report = validate_python(code)
    assert not report.allowed
    assert report.violations == ["code is empty"]


def test_syntax_error_is_reported_not_raised() -> None:
    report = validate_python("def broken(:\n")
    assert not report.allowed
    assert "syntax error" in report.violations[0]


def test_oversized_code_is_rejected() -> None:
    report = validate_python("result = 1\n" * 6000)
    assert not report.allowed
    assert "exceeds" in report.violations[0]


def test_raise_if_blocked_raises_a_validation_error() -> None:
    report = validate_python("import os")
    with pytest.raises(BlenderMCPError) as excinfo:
        report.raise_if_blocked()
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["violations"]


def test_raise_if_blocked_is_a_no_op_when_allowed() -> None:
    validate_python("import bpy").raise_if_blocked()


def test_every_documented_blocked_module_is_enforced() -> None:
    for module in sorted(BLOCKED_MODULES):
        report = validate_python(f"import {module}")
        assert not report.allowed, f"{module} should be blocked"


def test_bpy_is_not_blocked() -> None:
    assert "bpy" not in BLOCKED_MODULES
    assert validate_python("import bpy").allowed
