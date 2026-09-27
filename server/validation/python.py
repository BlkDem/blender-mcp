"""AST-based screening of code sent to :func:`blender.execute_python`.

This is deliberately *not* a sandbox. Blender's own Python API can already reach
the filesystem and the network, and no amount of source scanning changes that.
What this module does is stop the obvious footguns in code an LLM emitted by
accident, and it does so in one place so tools never grow their own ad-hoc
checks.

The check is a syntactic screen over the parsed AST, so it is a filter, not a
security boundary. See the "Security" section of the README.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from server.errors import BlenderMCPError, ErrorCode

#: Top-level modules a snippet may never import. ``bpy``/``mathutils`` and the
#: rest of the standard library stay available on purpose: they are what makes
#: the tool useful.
BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "requests",
        "urllib",
        "urllib2",
        "urllib3",
        "http",
        "httplib",
        "ftplib",
        "telnetlib",
        "smtplib",
        "asyncio",
        "multiprocessing",
        "ctypes",
        "builtins",
        "pickle",
        "pty",
        "importlib",
    }
)

#: Modules allowed under a blocked name when a narrower purpose applies.
_ALLOWED_SUBMODULES: dict[str, frozenset[str]] = {
    "pathlib": frozenset({"pathlib"}),  # importing is fine, only specific calls are not
}

#: Bare names that are never referenced or called. ``open`` is here rather than
#: only in :data:`BLOCKED_CALLS` so that ``f = open`` cannot launder it.
BLOCKED_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
        "exit",
        "quit",
        "memoryview",
    }
)

#: ``module.attribute`` and ``method()`` targets that are never allowed.
BLOCKED_CALLS: frozenset[str] = frozenset(
    {
        "open",
        "os.system",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.removedirs",
        "os.popen",
        "os.execv",
        "os.execve",
        "os.spawnv",
        "os.fork",
        "os.kill",
        "os.setuid",
        "os.putenv",
        "shutil.rmtree",
        "shutil.move",
        "shutil.copytree",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "socket.socket",
        "socket.create_connection",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
        "pathlib.Path.unlink",
        "pathlib.Path.rmdir",
        "pathlib.Path.touch",
        "bpy.ops.wm.quit_blender",
        "bpy.ops.wm.read_homefile",
        "bpy.ops.wm.read_factory_settings",
        "bpy.ops.wm.open_mainfile",
        "bpy.app.timers.unregister",
    }
)

#: Final attribute names that are never called, whatever they hang off. This is
#: the backstop for a call whose receiver is dynamic and therefore has no dotted
#: name to check against :data:`BLOCKED_CALLS`.
BLOCKED_METHODS: frozenset[str] = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rmdir",
        "rmtree",
        "system",
        "popen",
        "execv",
        "execve",
        "spawnv",
        "fork",
        "setuid",
        "putenv",
        "create_connection",
    }
)

#: Attribute names that are unsafe on any object, e.g. ``obj.__class__.__bases__``.
BLOCKED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__code__",
        "__closure__",
        "__getattribute__",
        "__reduce__",
        "__reduce_ex__",
    }
)

MAX_CODE_LENGTH = 100_000
MAX_CODE_LINES = 5_000


@dataclass(slots=True)
class ValidationReport:
    """Outcome of a screening pass."""

    allowed: bool
    violations: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    line_count: int = 0

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise BlenderMCPError(
                "Code blocked by the Python execution policy",
                code=ErrorCode.VALIDATION_ERROR,
                details={"violations": self.violations},
            )

    def summary(self) -> str:
        imports = ", ".join(sorted(set(self.imports))) or "none"
        return f"{self.line_count} line(s); imports: {imports}"


class _Visitor(ast.NodeVisitor):
    """Collects policy violations without executing anything."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.imports: list[str] = []

    def _violation(self, node: ast.AST, reason: str) -> None:
        self.violations.append(f"line {getattr(node, 'lineno', '?')}: {reason}")

    # --- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.imports.append(alias.name)
            if root in BLOCKED_MODULES and alias.name not in _ALLOWED_SUBMODULES.get(root, frozenset()):
                self._violation(node, f"import of blocked module '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module:
            self.imports.append(module)
            root = module.split(".")[0]
            if root in BLOCKED_MODULES and module not in _ALLOWED_SUBMODULES.get(root, frozenset()):
                self._violation(node, f"import from blocked module '{module}'")
        for alias in node.names:
            if alias.name in BLOCKED_NAMES:
                self._violation(node, f"import of blocked name '{alias.name}'")
        self.generic_visit(node)

    # --- calls and names -------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        target = _dotted_name(node.func)
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else None
        # `builtins.eval(...)`, `pathlib.Path(p).write_text(...)`: judge the
        # final attribute too, so wrapping a blocked name in a module or in a
        # constructor call does not help.
        if target in BLOCKED_NAMES or attribute in BLOCKED_NAMES:
            self._violation(node, f"call to blocked builtin '{target}'")
        elif target in BLOCKED_CALLS:
            self._violation(node, f"call to blocked target '{target}'")
        elif attribute in BLOCKED_METHODS:
            self._violation(node, f"call to blocked method '{attribute}'")
        elif target.endswith(".read") or target.endswith(".write"):
            # A file object handed in as a local would otherwise slip past `open`.
            self._violation(node, f"call to '{target}' (file access)")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BLOCKED_NAMES:
            self._violation(node, f"use of blocked name '{node.id}'")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in BLOCKED_ATTRIBUTES:
            self._violation(node, f"use of blocked attribute '{node.attr}'")
        self.generic_visit(node)


def _dotted_name(node: ast.AST) -> str:
    """Render ``a.b.c`` for a name/attribute chain, walking through calls.

    ``pathlib.Path(x).write_text`` resolves to ``pathlib.Path.write_text``; a
    chain that cannot be named statically comes back as ``<dynamic>``.
    """
    parts: list[str] = []
    current: ast.AST = node
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        else:
            return "<dynamic>"


def validate_python(code: str) -> ValidationReport:
    """Screen ``code`` and return a report; never raises for policy violations."""
    if not isinstance(code, str) or not code.strip():
        return ValidationReport(allowed=False, violations=["code is empty"])
    if len(code) > MAX_CODE_LENGTH:
        return ValidationReport(allowed=False, violations=[f"code exceeds {MAX_CODE_LENGTH} characters"])
    if code.count("\n") + 1 > MAX_CODE_LINES:
        return ValidationReport(allowed=False, violations=[f"code exceeds {MAX_CODE_LINES} lines"])

    try:
        tree = ast.parse(code, filename="<mcp-execute-python>", mode="exec")
    except SyntaxError as exc:
        return ValidationReport(allowed=False, violations=[f"syntax error: {exc.msg} (line {exc.lineno})"])

    visitor = _Visitor()
    visitor.visit(tree)
    return ValidationReport(
        allowed=not visitor.violations,
        violations=visitor.violations,
        imports=visitor.imports,
        line_count=code.count("\n") + 1,
    )
