"""Blender-side user interface: a connection panel and its two operators.

Kept in one small module on purpose. The panel and the operators that drive it
are two halves of the same feature, and splitting them across files would only
make the wiring harder to follow.

Open it with ``N`` in the 3D Viewport, in the Blender MCP tab.
"""

from __future__ import annotations

import logging
from typing import Any

import bpy  # type: ignore[import-not-found]
from bpy.props import IntProperty, StringProperty  # type: ignore[import-not-found]
from bpy.types import Operator, Panel  # type: ignore[import-not-found]

from blender_mcp.connection import BlenderConnection

logger = logging.getLogger(__name__)

#: The live connection, created by :func:`register` in ``__init__``.
_connection: BlenderConnection | None = None


def get_connection() -> BlenderConnection:
    """Return the shared connection, creating it if the panel ran first."""
    global _connection
    if _connection is None:  # pragma: no cover - only before register()
        _connection = BlenderConnection()
    return _connection


def set_connection(connection: BlenderConnection) -> None:
    """Install the connection instance the operators and panel share."""
    global _connection
    _connection = connection


class BLENDERMCP_OT_connect(Operator):  # noqa: N801 - Blender naming convention
    """Connect to the Blender MCP server"""

    bl_idname = "blender_mcp.connect"
    bl_label = "Connect"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        connection = get_connection()
        connection.set_server(context.scene.blender_mcp_host, context.scene.blender_mcp_port)
        if connection.connect():
            self.report({"INFO"}, f"Connecting to {connection.url}")
        else:
            self.report({"WARNING"}, "Already connected or connecting")
        return {"FINISHED"}


class BLENDERMCP_OT_disconnect(Operator):  # noqa: N801 - Blender naming convention
    """Disconnect from the Blender MCP server"""

    bl_idname = "blender_mcp.disconnect"
    bl_label = "Disconnect"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        get_connection().disconnect()
        self.report({"INFO"}, "Disconnected")
        return {"FINISHED"}


class BLENDERMCP_PT_panel(Panel):  # noqa: N801 - Blender naming convention
    """Blender MCP connection status and controls."""

    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender MCP"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        connection = get_connection()
        scene = context.scene

        box = layout.box()
        row = box.row()
        row.label(text="Status:", icon="LINKED" if connection.is_connected else "UNLINKED")
        row.label(text=connection.status)

        column = box.column(align=True)
        column.prop(scene, "blender_mcp_host", text="Server")
        column.prop(scene, "blender_mcp_port", text="Port")

        row = box.row(align=True)
        row.scale_y = 1.4
        if connection.is_connected:
            row.operator(BLENDERMCP_OT_disconnect.bl_idname, icon="UNLINKED")
        else:
            row.operator(BLENDERMCP_OT_connect.bl_idname, icon="LINKED")

        if connection.transaction_open:
            box.label(text="Transaction open", icon="CHECKMARK")
        if connection.last_error:
            box.label(text=connection.last_error, icon="ERROR")
        box.label(text="AI client talks MCP; this add-on talks to it.")


CLASSES: tuple[Any, ...] = (
    BLENDERMCP_OT_connect,
    BLENDERMCP_OT_disconnect,
    BLENDERMCP_PT_panel,
)


def register_properties() -> None:
    """Scene-level connection settings, so they survive a file reload."""
    bpy.types.Scene.blender_mcp_host = StringProperty(  # type: ignore[attr-defined]
        name="Host",
        description="Hostname of the Blender MCP server",
        default=get_connection().host,
    )
    bpy.types.Scene.blender_mcp_port = IntProperty(  # type: ignore[attr-defined]
        name="Port",
        description="Port of the Blender MCP server",
        default=get_connection().port,
        min=1,
        max=65535,
    )


def unregister_properties() -> None:
    del bpy.types.Scene.blender_mcp_host  # type: ignore[attr-defined]
    del bpy.types.Scene.blender_mcp_port  # type: ignore[attr-defined]


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    register_properties()


def unregister() -> None:
    get_connection().disconnect()
    unregister_properties()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
