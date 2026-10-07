"""Blender visualization, timeline, materials, and render configuration."""

from __future__ import annotations

import atexit
import json
import math
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from omaha_runtime import CalculationJob, write_json
from omaha_scene import (
    _building_geometry as _building_geometry,
)
from omaha_scene import (
    _visual_lifetime_records as _visual_lifetime_records,
)

try:
    import bpy  # pyright: ignore[reportMissingImports] -- provided by Blender
    from mathutils import Vector  # pyright: ignore[reportMissingImports] -- provided by Blender
except ModuleNotFoundError:
    bpy = None
    Vector = None


class SceneImport:
    """One bounded mesh per step; all methods must run on Blender's main thread."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.scene: Any = None
        self.owned = []
        self.finished = False
        self.done = 0
        self.total = 0
        self.message = "Reading mesh manifest"
        self.steps = self._steps()
        self.cfg = {}

    def own(self, database, item):
        self.owned.append((database, item))
        return item

    def _steps(self):
        assert bpy is not None
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf8"))
        if manifest["schema_version"] != 1:
            raise ValueError("Unsupported mesh manifest schema")
        cfg = dict(manifest["cfg"], run_directory=manifest["result_directory"])
        self.cfg = cfg
        grid = SimpleNamespace(**manifest["grid"])
        self.total = len(manifest["packets"])
        scene = self.own(bpy.data.scenes, bpy.data.scenes.new("OMAHA_SIMULATION"))
        self.scene = scene
        scene["omaha_simulation"] = True
        scene["model_note"] = (
            "Experimental demand scenario constrained by real OSM and LODES; not a forecast."
        )
        scene["year_at_frame_1"] = int(cfg["base_year"])
        scene["seed"] = int(cfg["seed"])
        scene["configuration_json"] = json.dumps(cfg, sort_keys=True)
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1000.0
        scene.unit_settings.length_unit = "KILOMETERS"
        yield
        collections = {}
        for key, name in (
            ("baseline", "Observed Omaha"),
            ("land", "Water and Parks"),
            ("future", "Scenario Development"),
            ("stage", "Presentation"),
        ):
            collection = self.own(bpy.data.collections, bpy.data.collections.new(name))
            collection["omaha_generated_visualization"] = True
            scene.collection.children.link(collection)
            collections[key] = collection
            yield
        materials = {}
        for name, values in manifest["materials"].items():
            materials[name] = self.own(bpy.data.materials, _new_material(name, **values))
            yield
        for packet in manifest["packets"]:
            self.message = f"Building {packet['collection']}: mesh {self.done + 1}/{self.total}"
            with np.load(self.directory / packet["file"], allow_pickle=False) as arrays:
                vertices = arrays["vertices"].tolist()
                indices, offsets = arrays["indices"], arrays["offsets"]
                faces = [indices[a:b].tolist() for a, b in zip(offsets[:-1], offsets[1:])]
            mesh = self.own(bpy.data.meshes, bpy.data.meshes.new(packet["name"]))
            mesh["omaha_generated_visualization"] = True
            mesh.from_pydata(vertices, [], faces)
            mesh.update()
            mesh.materials.append(materials[packet["material"]])
            obj = self.own(bpy.data.objects, bpy.data.objects.new(packet["name"], mesh))
            obj["omaha_generated_visualization"] = True
            collections[packet["collection"]].objects.link(obj)
            if packet["lifetime"] is not None:
                _set_object_lifetime(obj, *packet["lifetime"])
            self.done += 1
            yield
        self.message = "Configuring timeline and render device"
        base_year = int(cfg["base_year"])
        scene.frame_start = 1
        scene.frame_end = int(cfg["end_year"]) - base_year + 1
        scene.render.fps = int(cfg.get("render_fps", 2))
        for year in range(base_year, int(cfg["end_year"]) + 1):
            scene.timeline_markers.new(str(year), frame=year - base_year + 1)
        resolution = cfg.get("render_resolution", [1600, 1000])
        scene.render.resolution_x, scene.render.resolution_y = map(int, resolution)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.film_transparent = False
        _configure_cycles(cfg, scene)
        yield
        self.message = "Configuring camera and lighting"
        # Register presentation IDs as they are created, including on failure.
        _configure_camera_and_lighting(cfg, grid, scene, collections["stage"], own=self.own)
        yield
        scene.frame_set(1)
        if bpy.context.window is not None:
            bpy.context.window.scene = scene
        if bpy.context.screen is not None:
            for area in bpy.context.screen.areas:
                if area.type == "VIEW_3D":
                    space = area.spaces.active
                    space.clip_start = 0.001
                    space.clip_end = max(grid.width, grid.height) * 20.0
                    space.shading.color_type = "MATERIAL"
                    space.region_3d.view_perspective = "CAMERA"
        self.finished = True

    def cleanup_step(self):
        """Remove one partial-build ID per tick; never touch prior scenes."""
        self.steps.close()
        if self.finished or not self.owned:
            return True
        database, item = self.owned.pop()
        database.remove(item, do_unlink=True)
        return not self.owned


class BlenderJob:
    def __init__(self, cfg, executable):
        self.job = CalculationJob(cfg, executable)
        self.status = dict(self.job.status)
        self.started = time.monotonic()
        self.scene_started = None
        self.importer = None
        self.active = True
        self.cancel_requested = False
        self.failure = None
        self.last_phase = None
        self.last_written = 0.0
        self.timer_callback = self.tick

    def cancel(self):
        if self.active and not self.cancel_requested:
            self.cancel_requested = True
            self.job.cancel()
            self.status.update(
                phase="cancelling", message="Stopping calculation / removing partial scene"
            )

    def publish(self, force=False):
        now = time.monotonic()
        self.status["elapsed_seconds"] = now - self.started
        if force or now - self.last_written >= 0.5:
            self.status["heartbeat_utc"] = time.time()
            write_json(self.job.directory / "progress.json", self.status)
            self.last_written = now

    def tick(self):
        assert bpy is not None
        try:
            delay = self._tick()
        except Exception:
            self.failure = traceback.format_exc()
            print(self.failure)
            self.cancel()
            delay = 0.1
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        return delay

    def _tick(self):
        if not self.active:
            return None
        if self.cancel_requested:
            if not self.job.cancellation_finished():
                return 0.1
            if self.importer is not None and not self.importer.cleanup_step():
                return 0.01
            self.status.update(
                phase="error" if self.failure else "cancelled",
                complete=False,
                error=self.failure,
                worker_processes=0,
                broker_pid=None,
                message="Failed; see log"
                if self.failure
                else "Cancelled; saved checkpoints retained",
            )
            self.publish(force=True)
            self.active = False
            print("[Omaha] " + self.status["message"])
            return None
        if self.importer is None:
            code = self.job.poll()
            log = self.job.read_log()
            if log:
                print(log)
            self.status = dict(self.job.status)
            phase = self.status.get("phase")
            if phase != self.last_phase:
                print("[Omaha] " + self.status.get("message", str(phase)))
                self.last_phase = phase
            if code is None:
                return 0.25
            if code != 0 or phase != "scene_ready":
                raise RuntimeError(
                    self.status.get("error")
                    or f"Calculation exited with code {code}; see {self.job.directory / 'calculation.log'}"
                )
            if self.job.log_offset < (self.job.directory / "calculation.log").stat().st_size:
                return 0.01  # Drain the bounded log stream before starting scene import.
            self.importer = SceneImport(self.job.directory / "scene")
            self.scene_started = time.monotonic()
            self.status.update(phase="building_scene", complete=False, worker_processes=0)
            print("[Omaha] Blender scene construction: importing bounded mesh chunks")
        try:
            next(self.importer.steps)
        except StopIteration:
            cfg, scene = self.importer.cfg, self.importer.scene
            assert self.scene_started is not None
            print(f"Blender scene construction: {time.monotonic() - self.scene_started:.3f} s")
            self.status.update(phase="finalizing", message="Finalizing optional output")
            self.publish(force=True)
            # Optional native save/render operations remain explicit, synchronous
            # exports; the default interactive workflow does not enable them.
            finalize_blender_output(cfg, scene)
            self.status.update(
                phase="complete",
                message="Ready",
                complete=True,
                scene_progress=1.0,
                year=cfg["end_year"],
            )
            self.publish(force=True)
            self.active = False
            print(
                f"Ready: frame 1 = {cfg['base_year']}; frame {scene.frame_end} = {cfg['end_year']}."
            )
            print(f"Yearly states and summary: {Path(cfg['run_directory']) / 'simulation'}")
            # Blender owns the finished scene; do not retain large ID lists here.
            self.importer.owned.clear()
            return None
        self.status.update(
            message=self.importer.message,
            scene_progress=self.importer.done / max(1, self.importer.total),
        )
        self.publish()
        return 0.01


_ACTIVE_RUN = None
_REGISTERED = False


def _cancel_on_exit(*_args):
    if _ACTIVE_RUN is not None and _ACTIVE_RUN.active:
        _ACTIVE_RUN.cancel()


def _cancel_on_load(*_args):
    """Timers and scene IDs do not survive loading another .blend."""
    assert bpy is not None
    _cancel_on_exit()
    if _ACTIVE_RUN is not None:
        if bpy.app.timers.is_registered(_ACTIVE_RUN.timer_callback):
            bpy.app.timers.unregister(_ACTIVE_RUN.timer_callback)
        _ACTIVE_RUN.active = False
        _ACTIVE_RUN.importer = None
        _ACTIVE_RUN.status.update(
            phase="cancelled", message="Cancelled on file load", complete=False
        )


if bpy is not None:

    class OMAHA_OT_cancel(bpy.types.Operator):
        bl_idname = "omaha.cancel_simulation"
        bl_label = "Cancel Simulation"

        def execute(self, context):
            if _ACTIVE_RUN is not None:
                _ACTIVE_RUN.cancel()
            return {"FINISHED"}

    class OMAHA_PT_status(bpy.types.Panel):
        bl_label = "Omaha Simulation"
        bl_idname = "OMAHA_PT_status"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Omaha"

        def draw(self, context):
            layout = self.layout
            run = _ACTIVE_RUN
            if run is None:
                layout.label(text="Run blender.py to start")
                return
            status = run.status
            layout.label(text="Phase: " + status["phase"].replace("_", " ").title())
            layout.label(text=status.get("message", ""))
            year = status.get("year")
            if year is not None:
                layout.label(text=f"Year: {year} / {status['end_year']}")
            layout.label(text=f"Workers: {status.get('worker_processes', 0)}")
            elapsed = status.get("elapsed_seconds", 0)
            layout.label(text=f"Elapsed: {int(elapsed // 60)}m {int(elapsed % 60)}s")
            if status["phase"] == "building_scene":
                layout.progress(factor=status.get("scene_progress", 0), text="Scene meshes")
            elif status["phase"] == "simulation" and year is not None:
                span = max(1, status["end_year"] - status["start_year"])
                layout.progress(
                    factor=max(0, min(1, (year - status["start_year"]) / span)),
                    text="Simulation year",
                )
            heartbeat = status.get("heartbeat_utc")
            if run.active and heartbeat is not None and time.time() - heartbeat > 10:
                layout.label(text="No recent heartbeat; check calculation.log", icon="ERROR")
            if status.get("error"):
                layout.label(text="See live log or job calculation.log", icon="ERROR")
            if run.active:
                row = layout.row()
                row.enabled = not run.cancel_requested
                row.operator("omaha.cancel_simulation")


def start_blender_job(cfg):
    """Launch and return immediately; timers do only polling and bounded imports."""
    global _ACTIVE_RUN, _REGISTERED
    assert bpy is not None, "Run blender.py inside Blender."
    from omaha_config import validate_config
    from omaha_simulation import _worker_python

    if _ACTIVE_RUN is not None and _ACTIVE_RUN.active:
        raise RuntimeError(
            "An Omaha job is already running. Use the Omaha panel to cancel it first."
        )
    validate_config(cfg)
    if not _REGISTERED:
        bpy.utils.register_class(OMAHA_OT_cancel)
        bpy.utils.register_class(OMAHA_PT_status)
        atexit.register(_cancel_on_exit)
        _REGISTERED = True
    _ACTIVE_RUN = BlenderJob(dict(cfg, data_mode="OFFLINE"), _worker_python(cfg))
    # Cancel before loading another .blend, while the old data still exists.
    if _cancel_on_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_cancel_on_load)
    bpy.app.timers.register(_ACTIVE_RUN.timer_callback, first_interval=0.1)
    print("[Omaha] Calculation launched. View 3D Viewport > Sidebar (N) > Omaha.")
    print(f"[Omaha] Job status and calculation.log: {_ACTIVE_RUN.job.directory}")
    return _ACTIVE_RUN


def _new_material(name, color, roughness=0.75, metallic=0.0):
    assert bpy is not None, "This operation requires Blender."
    material = bpy.data.materials.new(name)
    material["omaha_generated_visualization"] = True
    material.use_nodes = True
    material.diffuse_color = (*color, 1.0)
    shader = material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1.0)
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metallic
    return material


def _set_object_lifetime(obj, start_year, end_year, base_year):
    """Saved, stateless visibility for viewport and rendering in both scrub directions.

    Only Blender's built-in frame variable and simple expressions are used.
    No handlers, timers, custom driver namespace, or legacy Action API is needed.
    end_year is exclusive, so replacement versions never coexist in a frame.
    """
    first = int(start_year) - int(base_year) + 1
    expression = f"frame < {first}"
    if end_year is not None:
        last = int(end_year) - int(base_year) + 1
        expression += f" or frame >= {last}"
    obj["construction_start_year"] = int(start_year)
    obj["demolition_year_exclusive"] = int(end_year) if end_year is not None else -1
    for property_name in ("hide_viewport", "hide_render"):
        driver = obj.driver_add(property_name).driver
        driver.type = "SCRIPTED"
        driver.expression = expression


def _configure_cycles(cfg, scene):
    """Use the RTX 3080 Ti via OptiX; clearly announce any CPU fallback."""
    assert bpy is not None, "This operation requires Blender."
    scene.render.engine = "CYCLES"
    scene.cycles.samples = int(cfg.get("render_samples", 64 if cfg["preview_mode"] else 128))
    scene.cycles.preview_samples = int(cfg.get("preview_samples", 24))
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.use_denoising = True
    scene.cycles.seed = int(cfg["seed"]) % 2147483647
    scene.cycles.use_animated_seed = False
    scene.render.use_persistent_data = True
    failure = None
    try:
        addon = bpy.context.preferences.addons.get("cycles")
        if addon is None:
            raise RuntimeError("Cycles preferences are unavailable")
        preferences = addon.preferences
        preferences.compute_device_type = "OPTIX"
        preferences.refresh_devices()
        optix = [device for device in preferences.devices if device.type == "OPTIX"]
        if not optix:
            raise RuntimeError("no OptiX device was detected")
        preferred_name = cfg.get("render_device_name", "3080 Ti").lower()
        selected = [device for device in optix if preferred_name in device.name.lower()]
        if not selected:
            selected = optix
        selected_ids = {device.id for device in selected}
        for device in preferences.devices:
            device.use = device.type == "OPTIX" and device.id in selected_ids
        scene.cycles.device = "GPU"
        scene["render_backend"] = "OPTIX: " + ", ".join(device.name for device in selected)
    except (RuntimeError, TypeError, AttributeError) as exc:
        failure = str(exc)
    if failure is not None:
        message = f"OptiX unavailable ({failure}). Cycles will render on the CPU."
        if cfg.get("require_optix", False):
            raise RuntimeError(message + " Set require_optix=False to permit CPU rendering.")
        print("[Omaha] " + message)
        scene.cycles.device = "CPU"
        scene["render_backend"] = "CPU fallback: " + failure
    else:
        print("[Omaha] " + scene["render_backend"])


def _configure_camera_and_lighting(cfg, grid, scene, collection, own=lambda database, item: item):
    assert Vector is not None, "This operation requires Blender."
    assert bpy is not None, "This operation requires Blender."
    center = Vector(((grid.min_x + grid.max_x) * 0.5, (grid.min_y + grid.max_y) * 0.5, 0.025))
    span = max(grid.width, grid.height, 0.1)
    data = own(bpy.data.cameras, bpy.data.cameras.new("Omaha_Overview_Camera"))
    data["omaha_generated_visualization"] = True
    camera = own(bpy.data.objects, bpy.data.objects.new("Omaha_Overview_Camera", data))
    camera["omaha_generated_visualization"] = True
    collection.objects.link(camera)
    camera.location = center + Vector((0.65 * span, -0.85 * span, 0.9 * span))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    data.type = "ORTHO"
    data.clip_start = 0.001
    data.clip_end = span * 20.0
    data.ortho_scale = 1.0
    scene.camera = camera
    # Fit study corners using Blender's actual camera frame and aspect ratio.
    inverse_rotation = camera.rotation_euler.to_quaternion().inverted()
    corners = [
        inverse_rotation @ (Vector((x, y, z)) - center)
        for x in (grid.min_x, grid.max_x)
        for y in (grid.min_y, grid.max_y)
        for z in (0.0, 0.3)
    ]
    frame = data.view_frame(scene=scene)
    unit_half_width = max(abs(vertex.x) for vertex in frame)
    unit_half_height = max(abs(vertex.y) for vertex in frame)
    data.ortho_scale = 1.12 * max(
        max(abs(vertex.x) for vertex in corners) / unit_half_width,
        max(abs(vertex.y) for vertex in corners) / unit_half_height,
    )
    light_data = own(bpy.data.lights, bpy.data.lights.new("Omaha_Sun", "SUN"))
    light_data["omaha_generated_visualization"] = True
    light_data.energy = 2.5
    light_data.angle = math.radians(12.0)
    sun = own(bpy.data.objects, bpy.data.objects.new("Omaha_Sun", light_data))
    sun["omaha_generated_visualization"] = True
    collection.objects.link(sun)
    sun.rotation_euler = (math.radians(27), math.radians(-19), math.radians(-28))
    world = own(bpy.data.worlds, bpy.data.worlds.new("Omaha_Atmosphere"))
    world["omaha_generated_visualization"] = True
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.68, 0.75, 0.84, 1.0)
    background.inputs["Strength"].default_value = 0.45
    scene.world = world
    scene.view_settings.view_transform = "AgX"
    return camera


def finalize_blender_output(cfg, scene):
    """Optionally save a .blend, final still, and/or numbered PNG animation."""
    assert bpy is not None, "This operation requires Blender."
    output_dir = Path(cfg.get("run_directory", cfg["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    animation_dir = output_dir / "frames"
    scene.render.filepath = str(animation_dir / "omaha_")
    if cfg.get("save_blend", False):
        path = output_dir / "omaha_simulation.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        print("[Omaha] Saved visualization: " + str(path))
    original_frame = scene.frame_current
    try:
        if cfg.get("render_final", False):
            scene.frame_set(scene.frame_end)
            scene.render.filepath = str(output_dir / f"omaha_{cfg['end_year']}.png")
            bpy.ops.render.render(write_still=True, scene=scene.name)
        if cfg.get("render_animation", False):
            animation_dir.mkdir(parents=True, exist_ok=True)
            scene.render.filepath = str(animation_dir / "omaha_")
            bpy.ops.render.render(animation=True, scene=scene.name)
    finally:
        scene.frame_set(original_frame)
        scene.render.filepath = str(animation_dir / "omaha_")
