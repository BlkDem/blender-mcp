"""Runs inside the Blender GUI: prepare the window, then wait to be photographed.

The other half is ``examples/mcp_client_demo.py``, which owns the MCP server and
plays the part of the AI client. This side only makes Blender look ready:

1. enable the add-on and connect it to the bridge,
2. clear the scene,
3. put the 3D viewport on the Blender MCP tab,
4. wait for the client to finish building the scene, then take a screenshot.

Two Blender-specific rules are baked in here, both learned the hard way:

* **Everything runs in a timer.** Scene changes in a ``--python`` startup script
  crash Blender 5.x, because the UI is not realised yet.
* **The sidebar tab can only be selected after a redraw.** ``Region
  .active_panel_category`` is read-only until the region has been drawn with the
  panel in it, so the order is: open the region, redraw, then assign.

Usage::

    blender --python examples/gui_blender_demo.py -- --out /tmp/mcp-demo
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

import bpy

_ADDON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon")
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

#: What the camera should look at. The floor is 16 m across, so framing
#: everything would shrink the subject to a speck.
SUBJECT = ("TableTop", "LegFL", "LegFR", "LegBL", "LegBR", "Ball", "Ring")

#: Redraws to allow EEVEE to compile the scene before the shot.
_COMPILE_FRAMES = 150


def arguments() -> argparse.Namespace:
    """Options from the environment, overridden by anything after Blender's ``--``.

    The environment is the primary channel on purpose. The alternative —
    passing ``--out`` through a launcher — means threading a bare ``--``
    separator through a second argument parser, and argparse turns
    ``--blender-arg=--`` into a nested list rather than the string. A launcher
    that sets ``MCP_DEMO_OUT`` and passes nothing else cannot get the quoting
    wrong on a path with spaces, on either platform.
    """
    parser = argparse.ArgumentParser(prog="gui_blender_demo")
    parser.add_argument("--out", default=os.environ.get("MCP_DEMO_OUT", os.getcwd()))
    parser.add_argument("--host", default=os.environ.get("MCP_DEMO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_DEMO_PORT", "8765")))
    parser.add_argument("--shot", default=os.environ.get("MCP_DEMO_SHOT", "screenshot.png"))
    parser.add_argument(
        "--flag",
        default=os.environ.get("MCP_DEMO_FLAG", "shoot.flag"),
        help="written by the client to trigger the shot",
    )
    parser.add_argument(
        "--done",
        default=os.environ.get("MCP_DEMO_DONE", "done.flag"),
        help="written here once the shot is safe to read",
    )
    parser.add_argument("--blend", default=os.environ.get("MCP_DEMO_BLEND", "mcp_demo.blend"))
    parser.add_argument("--log", default=os.environ.get("MCP_DEMO_LOG", "gui.log"))
    parser.add_argument(
        "--keep-open",
        action="store_true",
        default=os.environ.get("MCP_DEMO_KEEP_OPEN") == "1",
        help="stay open after the screenshot instead of quitting, so a human can look at it",
    )
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(argv)


class Demo:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.log_path = os.path.join(options.out, options.log)
        self.shot = os.path.join(options.out, options.shot)
        self.flag = os.path.join(options.out, options.flag)
        self.done = os.path.join(options.out, options.done)
        self.blend = os.path.join(options.out, options.blend)
        self.stage = 0

    # --- plumbing ---------------------------------------------------------

    def log(self, message: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")
        print(f"[gui] {message}", flush=True)

    def ui_region(self):
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "UI":
                        return window, area, region
        return None, None, None

    @staticmethod
    def redraw(count: int = 1) -> None:
        for _ in range(count):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

    # --- steps ------------------------------------------------------------

    def enable_addon(self) -> None:
        bpy.ops.preferences.addon_enable(module="blender_mcp")
        self.log("add-on enabled")

    def connect(self) -> None:
        from blender_mcp import ui

        connection = ui.get_connection()
        connection.set_server(self.options.host, self.options.port)
        bpy.context.scene.blender_mcp_host = self.options.host
        bpy.context.scene.blender_mcp_port = self.options.port
        connection.connect()
        self.log(f"connecting: {connection.info()}")

    def clear_scene(self) -> None:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.context.scene.name = "MCPDemo"
        bpy.context.scene.render.engine = _preferred_engine()
        self.log(f"scene cleared, engine -> {bpy.context.scene.render.engine}")

    def show_panel(self) -> None:
        _window, area, region = self.ui_region()
        if area is None:
            self.log("no 3D viewport to host the panel")
            return
        space = area.spaces.active
        space.show_region_ui = True
        # RENDERED shows the materials and lights the model set up. The GUI has a
        # GL context, so EEVEE works here; it does not when headless.
        space.shading.type = "RENDERED"
        self.redraw(2)
        try:
            region.active_panel_category = "Blender MCP"
            self.log("sidebar on the Blender MCP tab")
        except Exception as exc:  # pragma: no cover - Blender version dependent
            self.log(f"could not select the tab: {exc!r}")
        self.redraw(2)

    def frame_scene(self) -> None:
        window, area, _ = self.ui_region()
        if area is None:
            return
        region = next(r for r in area.regions if r.type == "WINDOW")
        for obj in bpy.data.objects:
            obj.select_set(False)
        for name in SUBJECT:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                obj.select_set(True)
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.view3d.view_persportho()
            bpy.ops.view3d.view_axis(type="FRONT")
            bpy.ops.view3d.view_orbit(angle=0.85, type="ORBITLEFT")
            bpy.ops.view3d.view_orbit(angle=0.45, type="ORBITUP")
            bpy.ops.view3d.view_selected()
        # Keep the framing, drop the orange outlines.
        for obj in bpy.data.objects:
            obj.select_set(False)

    def take_shot(self) -> None:
        from blender_mcp import ui

        self.log("client says the scene is ready")
        self.frame_scene()
        self.show_panel()
        self.redraw(_COMPILE_FRAMES)
        bpy.ops.screen.screenshot(filepath=self.shot)
        self.log(f"screenshot: {self.shot} ({os.path.getsize(self.shot)} bytes)")
        self.log(f"connection: {ui.get_connection().info()}")
        bpy.ops.wm.save_as_mainfile(filepath=self.blend)
        self.log(f"saved {self.blend}")
        # Written last: the client must not read the screenshot until this exists,
        # or it kills Blender mid-flush and the PNG ends up truncated.
        with open(self.done, "w", encoding="utf-8") as handle:
            handle.write("done")
        self.log("wrote the done flag")
        if self.options.keep_open:
            # Stay open for a human. The timer unregisters itself; the add-on
            # stays connected for as long as the client keeps the bridge up.
            self.log("keeping Blender open (--keep-open)")
            self.stage = 5

    # --- the timer --------------------------------------------------------

    def tick(self):
        try:
            if self.stage == 0:
                self.enable_addon()
                self.stage = 1
            elif self.stage == 1:
                self.connect()
                self.stage = 2
            elif self.stage == 2:
                self.clear_scene()
                self.show_panel()
                self.frame_scene()
                self.log("ready; waiting for the client")
                self.stage = 3
            elif self.stage == 3:
                if not os.path.exists(self.flag):
                    return 0.25
                os.remove(self.flag)
                # take_shot() owns the next stage: 4 to quit, 5 to stay open.
                self.take_shot()
            elif self.stage == 4:
                bpy.ops.wm.quit_blender()
                return None
            else:
                return None  # stage 5 with --keep-open: idle
        except Exception:
            self.log(f"EXCEPTION at stage {self.stage}:\n{traceback.format_exc()}")
            try:
                bpy.ops.wm.quit_blender()
            except Exception:
                pass
            return None
        return 0.25


def _preferred_engine() -> str:
    """EEVEE was renamed between 4.x and 5.x; ask instead of guessing."""
    scene = bpy.context.scene
    original = scene.render.engine
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "CYCLES"):
        try:
            scene.render.engine = engine
        except TypeError:
            continue
        return engine
    return original


def main() -> None:
    options = arguments()
    os.makedirs(options.out, exist_ok=True)
    for stale in (options.log, options.done, options.flag):
        path = os.path.join(options.out, stale)
        if os.path.exists(path):
            os.remove(path)
    # Read on the first redraw, so it has to happen before the timer.
    bpy.context.preferences.view.show_splash = False

    demo = Demo(options)
    demo.log("registering the startup timer")
    bpy.app.timers.register(demo.tick, first_interval=1.0)


if __name__ == "__main__":
    # Deliberately not `raise SystemExit(main())`: SystemExit ends the Blender
    # process, and a GUI session has to stay open to be photographed.
    main()
