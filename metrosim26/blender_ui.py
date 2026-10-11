"""Blender sidebar controls, settings, job lifecycle, and progress."""

from __future__ import annotations

import atexit
import json
import math
import subprocess
import textwrap
import time
from pathlib import Path

import numpy as np

from metrosim26.blender.coordinates import (
    parse_coordinates as parse_coordinates,
)
from metrosim26.blender.job import BlenderJob
from metrosim26.blender.operators import make_operator_types
from metrosim26.blender.panel import draw_status
from metrosim26.blender.presentation import (
    PALETTES,
)
from metrosim26.blender.properties import make_setup_type

try:
    import bpy  # pyright: ignore[reportMissingImports] -- provided by Blender
except ModuleNotFoundError:
    bpy = None


def readable_labels(layout, text, width=36):
    """Wrap by rendered text width instead of assuming all letters are equally wide."""
    if bpy is None:
        for paragraph in str(text).splitlines():
            for line in textwrap.wrap(paragraph, max(18, width)) or [""]:
                layout.label(text=line)
        return
    import blf  # Blender's font metrics, in unscaled UI pixels.

    blf.size(0, 11)
    available = max(18, width) * 8
    # Separate rows should share normal line spacing even in a tall parent panel.
    column = layout.column(align=True)
    for paragraph in str(text).splitlines():
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if line and blf.dimensions(0, candidate)[0] > available:
                column.label(text=line)
                line = word
            else:
                line = candidate
        column.label(text=line)


_LOCATION_PROCESS = None
_LOCATION_TARGET = None
_LOCATION_CHANGED = 0.0
_LOCATION_LAST_REQUEST = 0.0


def location_watch():
    """Debounce completed coordinate edits; network work runs outside Blender."""
    global _LOCATION_PROCESS, _LOCATION_TARGET, _LOCATION_CHANGED, _LOCATION_LAST_REQUEST
    if bpy is None or not hasattr(bpy.context.scene, "metrosim26_setup"):
        return None
    settings = bpy.context.scene.metrosim26_setup
    key = (round(settings.latitude, 5), round(settings.longitude, 5), settings.cache_dir)
    now = time.monotonic()
    if _LOCATION_PROCESS is not None:
        if _LOCATION_PROCESS.poll() is None:
            return 0.5
        output, error = _LOCATION_PROCESS.communicate()
        _LOCATION_PROCESS = None
        if key == _LOCATION_TARGET:
            try:
                record = json.loads(output)
                label = record["label"]
                if (
                    not settings.location_name
                    or settings.location_name == settings.auto_location_name
                ):
                    settings.location_name = label
                    settings.auto_location_name = label
                settings.location_label = label
                settings.location_coordinates = record["coordinates"]
            except (ValueError, KeyError):
                settings.location_label = "Place lookup unavailable; enter a name."
            for area in bpy.context.screen.areas if bpy.context.screen else []:
                area.tag_redraw()
    if key != _LOCATION_TARGET:
        _LOCATION_TARGET = key
        _LOCATION_CHANGED = now
        settings.location_label = "Looking up location…"
        return 0.5
    coordinates = f"{key[0]:.5f},{key[1]:.5f}"
    if settings.location_style != "detailed-v1":
        settings.location_coordinates = ""
        settings.location_style = "detailed-v1"
    if coordinates == settings.location_coordinates:
        return 0.5
    if now - _LOCATION_CHANGED < 2 or now - _LOCATION_LAST_REQUEST < 1:
        return 0.5
    settings.location_coordinates = coordinates  # One request per edit, including errors.
    try:
        from metrosim26.simulation import _worker_python

        command = "import json,sys; from metrosim26.data import lookup_location; print(json.dumps(lookup_location(float(sys.argv[1]),float(sys.argv[2]),sys.argv[3])))"
        _LOCATION_PROCESS = subprocess.Popen(
            [_worker_python({}), "-c", command, str(key[0]), str(key[1]), storage_path(key[2])],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _LOCATION_LAST_REQUEST = now
    except (OSError, ValueError, RuntimeError):
        settings.location_label = "Place lookup unavailable; enter a name."
    return 0.5


LAYER_DEFAULTS = {
    "buildings": True,
    "roads": True,
    "parks": True,
    "water": True,
    "ocean": True,
    "terrain": True,
    "ground": True,
}
_PALETTE_UPDATING = False


def palette_changed(settings, context):
    if _PALETTE_UPDATING:
        return
    if settings.palette_preset != "CUSTOM":
        settings.palette_preset = "CUSTOM"
    if context is None or not hasattr(context.scene, "metrosim26_setup"):
        return
    colors = {key: tuple(getattr(settings, "color_" + key)) for key in PALETTES["NATURAL"]}
    for obj in context.scene.objects:
        if not obj.get("metrosim26_generated_visualization") or obj.type != "MESH":
            continue
        for material in obj.data.materials:
            key = material.name.removeprefix("MetroSim26_").split(".")[0]
            if key not in colors:
                continue
            color = (*colors[key], 1)
            material.diffuse_color = color
            if material.use_nodes:
                shader = material.node_tree.nodes.get("Principled BSDF")
                if shader is not None:
                    shader.inputs["Base Color"].default_value = color


def preset_changed(settings, context):
    global _PALETTE_UPDATING
    if _PALETTE_UPDATING:
        return
    _PALETTE_UPDATING = True
    try:
        if settings.palette_preset in PALETTES:
            for key, value in PALETTES[settings.palette_preset].items():
                setattr(settings, "color_" + key, value)
    finally:
        _PALETTE_UPDATING = False
    # Apply without changing the preset label.
    chosen = settings.palette_preset
    palette_changed(settings, context)
    if chosen != "CUSTOM":
        _PALETTE_UPDATING = True
        try:
            settings.palette_preset = chosen
        finally:
            _PALETTE_UPDATING = False


def remove_default_cube():
    if bpy is None:
        return
    cube = bpy.context.scene.objects.get("Cube")
    if (
        cube is None
        or cube.type != "MESH"
        or cube.modifiers
        or cube.constraints
        or cube.animation_data
        or cube.parent is not None
        or cube.keys()
    ):
        return
    mesh = cube.data
    expected = {(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)}
    if (
        tuple(cube.location) == (0, 0, 0)
        and tuple(cube.rotation_euler) == (0, 0, 0)
        and tuple(cube.scale) == (1, 1, 1)
        and len(mesh.materials) <= 1
        and (
            not mesh.materials
            or (
                mesh.materials[0].name == "Material"
                and all(abs(c - 0.8) < 1e-5 for c in mesh.materials[0].diffuse_color[:3])
            )
        )
        and len(mesh.vertices) == 8
        and len(mesh.polygons) == 6
        and {tuple(v.co) for v in mesh.vertices} == expected
    ):
        bpy.data.objects.remove(cube, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def smaller_map(cfg):
    if bpy is None or cfg.get("view_mode") != "MAP":
        return None
    candidates = []
    for scene in bpy.data.scenes:
        try:
            previous = json.loads(scene.get("configuration_json", "{}"))
            if (
                previous.get("view_mode") == "MAP"
                and previous.get("study_center") == cfg.get("study_center")
                and 0 < previous.get("study_radius_km", 0) < cfg.get("study_radius_km", 0)
                and previous.get("osm_data_snapshot") == cfg.get("osm_data_snapshot")
                and previous.get("vertical_exaggeration") == cfg.get("vertical_exaggeration")
            ):
                candidates.append((previous["study_radius_km"], scene))
        except (ValueError, TypeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


_ACTIVE_RUN = None
_REGISTERED = False


def _cancel_on_exit(*_args):
    if _LOCATION_PROCESS is not None and _LOCATION_PROCESS.poll() is None:
        _LOCATION_PROCESS.terminate()
        _LOCATION_PROCESS.wait(timeout=5)
    if _ACTIVE_RUN is not None and _ACTIVE_RUN.active:
        _ACTIVE_RUN.cancel()


def _cancel_on_load(*_args):
    """Timers and scene IDs do not survive loading another .blend."""
    global _ACTIVE_RUN, _LOCATION_PROCESS, _LOCATION_TARGET
    assert bpy is not None
    _cancel_on_exit()
    if _ACTIVE_RUN is not None:
        if bpy.app.timers.is_registered(_ACTIVE_RUN.timer_callback):
            bpy.app.timers.unregister(_ACTIVE_RUN.timer_callback)
        _ACTIVE_RUN.active = False
        if _ACTIVE_RUN.importer is not None:
            _ACTIVE_RUN.importer.steps.close()
        _ACTIVE_RUN.importer = None
        _ACTIVE_RUN.status.update(
            phase="cancelled", message="Cancelled on file load", complete=False
        )
    _ACTIVE_RUN = None
    _LOCATION_PROCESS = None
    _LOCATION_TARGET = None


if bpy is not None:
    _cancel_on_load = bpy.app.handlers.persistent(_cancel_on_load)

    class METROSIM26_PT_status(bpy.types.Panel):
        bl_label = "MetroSim 26"
        bl_idname = "METROSIM26_PT_status"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "MetroSim 26"

        def draw(self, context):
            draw_status(
                self,
                context,
                _ACTIVE_RUN,
                readable_labels=readable_labels,
                LAYER_DEFAULTS=LAYER_DEFAULTS,
                setup_config=setup_config,
                estimate_text=estimate_text,
                smaller_map=smaller_map,
            )


def start_blender_job(cfg):
    """Launch and return immediately; timers do only polling and bounded imports."""
    global _ACTIVE_RUN, _REGISTERED
    assert bpy is not None, "Run metrosim.py inside Blender."
    from metrosim26.config import validate_config
    from metrosim26.simulation import _worker_python

    if _ACTIVE_RUN is not None and _ACTIVE_RUN.active:
        raise RuntimeError(
            "A MetroSim26 job is already running. Use the MetroSim26 panel to cancel it first."
        )
    validate_config(cfg)
    open_setup(cfg, reset=False)
    _ACTIVE_RUN = BlenderJob(
        dict(cfg, data_mode="OFFLINE"),
        _worker_python(cfg),
        read_settings=setup_config,
        start_build=start_blender_job,
        apply_palette=palette_changed,
    )
    # Cancel before loading another .blend, while the old data still exists.
    if _cancel_on_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_cancel_on_load)
    bpy.app.timers.register(_ACTIVE_RUN.timer_callback, first_interval=0.1)
    print("[MetroSim26] Calculation launched. View 3D Viewport > Sidebar (N) > MetroSim26.")
    print(f"[MetroSim26] Job status and calculation.log: {_ACTIVE_RUN.job.directory}")
    return _ACTIVE_RUN


_SETUP_CONFIG = None


def storage_path(value):
    if not value.strip():
        raise ValueError("Choose both Cache and Results folders before preparing an area.")
    expanded = bpy.path.abspath(value) if bpy is not None else value
    path = Path(expanded).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"Storage path is a file, not a folder: {path}")
    return str(path)


def setup_config(settings):
    if getattr(settings, "coordinate_error", ""):
        raise ValueError(settings.coordinate_error)
    from metrosim26.config import circle_config, make_config

    base = dict(_SETUP_CONFIG or make_config(), view_mode=settings.view_mode)
    if settings.view_mode == "MAP":
        base.update(
            end_year=base["base_year"],
            historical_mode="DISABLED",
            timeline_start_year=None,
            enable_projects=False,
            enable_infrastructure=False,
        )
    cfg = circle_config(
        base,
        round(settings.latitude, 5),
        round(settings.longitude, 5),
        settings.radius,
        settings.units,
    )
    cfg["geometry_layout_version"] = "feature-terrain-2"
    cfg["terrain_spacing_km"] = max(
        0.03, max(cfg["bounds"][2] - cfg["bounds"][0], 0) * 110.574 / 200
    )
    cfg.pop("expand_inner_radius_km", None)
    cfg.pop("expand_scene", None)
    cfg.update(
        {
            "include_" + key: getattr(settings, "include_" + key, default)
            for key, default in LAYER_DEFAULTS.items()
        }
    )
    if settings.view_mode != "MAP":
        cfg["include_terrain"] = cfg["include_ocean"] = False
    if not cfg["include_water"]:
        cfg["include_ocean"] = False
    cfg["palette"] = {
        key: tuple(getattr(settings, "color_" + key, value))
        for key, value in PALETTES["NATURAL"].items()
    }
    cfg.update(
        end_year=cfg["base_year"] if settings.view_mode == "MAP" else settings.end_year,
        run_mode=settings.run_mode,
        progressive_build=settings.progressive_build,
        display_units=settings.units,
        location_name=getattr(settings, "location_name", ""),
        location_label=getattr(settings, "location_label", ""),
        auto_location_name=getattr(settings, "auto_location_name", ""),
        auto_build=getattr(settings, "auto_build", True),
        cache_dir=storage_path(settings.cache_dir),
        reuse_cache_dir=storage_path(settings.reuse_cache_dir)
        if getattr(settings, "reuse_cache_dir", "")
        else "",
        output_dir=storage_path(settings.output_dir),
    )
    if getattr(settings, "build_choice", "EXPAND") == "EXPAND":
        previous = smaller_map(cfg)
        if previous is not None:
            cfg["expand_scene"] = previous.name
            old = json.loads(previous.get("configuration_json", "{}"))
            compatible = old.get("geometry_layout_version") == "feature-terrain-2" and all(
                old.get("include_" + key, default) == cfg.get("include_" + key, default)
                for key, default in LAYER_DEFAULTS.items()
            )
            compatible = compatible and old.get("osm_query_version") == cfg.get("osm_query_version")
            if compatible and not any(
                obj.type == "MESH"
                and obj.get("metrosim26_generated_visualization")
                and not np.allclose(np.asarray(obj.matrix_world), np.eye(4), atol=1e-8)
                for obj in previous.objects
            ):
                cfg["expand_inner_radius_km"] = old["study_radius_km"]
                cfg["terrain_spacing_km"] = old["terrain_spacing_km"]
    from metrosim26.config import validate_config

    validate_config(cfg)
    return cfg


def apply_setup(settings, cfg):
    center = cfg.get("study_center", [-95.99, 41.25])
    settings.view_mode = cfg.get("view_mode", "MAP")
    for key, default in LAYER_DEFAULTS.items():
        setattr(settings, "include_" + key, cfg.get("include_" + key, default))
    for key, value in cfg.get("palette", PALETTES["NATURAL"]).items():
        if key in PALETTES["NATURAL"]:
            setattr(settings, "color_" + key, value)
    for preset, values in PALETTES.items():
        if all(
            np.allclose(getattr(settings, "color_" + key), value) for key, value in values.items()
        ):
            settings.palette_preset = preset
            break
    settings.longitude, settings.latitude = center
    if hasattr(settings, "coordinate_latitude"):
        settings.coordinate_latitude = f"{settings.latitude:.5f}"
        settings.coordinate_longitude = f"{settings.longitude:.5f}"
    settings.units = cfg.get("display_units", "MILES")
    settings.radius = cfg.get("study_radius_km", 2 * 1.609344) / (
        1.609344 if settings.units == "MILES" else 1
    )
    settings.location_name = cfg.get("location_name", "")
    settings.auto_location_name = cfg.get("auto_location_name", "")
    settings.location_label = cfg.get("location_label", "")
    if settings.location_label:
        settings.location_coordinates = f"{settings.latitude:.5f},{settings.longitude:.5f}"
    settings.auto_build = cfg.get("auto_build", True)
    settings.end_year = cfg["end_year"]
    settings.run_mode = cfg["run_mode"]
    settings.progressive_build = cfg.get("progressive_build", True)
    settings.pause_reveal = False
    settings.cache_dir = cfg["cache_dir"]
    settings.output_dir = cfg["output_dir"]


def estimate_text(cfg):
    """Broad measured ranges; missing inputs are a lightweight cache hint, not validation."""
    from metrosim26.data import expected_inputs

    specs = expected_inputs(cfg)
    missing = sum(not spec.path.is_file() for spec in specs)
    lines = [f"Cached files: {len(specs) - missing}/{len(specs)} (checked when preparing)"]
    path = Path(cfg["output_dir"]) / "area_timings.json"
    try:
        samples = json.loads(path.read_text())
        if not isinstance(samples, list):
            samples = []
    except (OSError, ValueError):
        samples = []
    action = "MAP" if cfg.get("view_mode") == "MAP" else cfg.get("run_mode", "SIMULATE")
    for kind, label in (
        ("PREFETCH", "Prepare data"),
        (action, "Calculate / export"),
        ("IMPORT", "Visible build"),
    ):
        if kind == "PREFETCH" and missing == 0:
            lines.append("Prepare data: inputs cached; validation still needed")
            continue
        records = []
        for sample in samples[-30:]:
            try:
                same_cache_work = kind != "PREFETCH" or bool(sample["missing_inputs"]) == bool(
                    missing
                )
                if (
                    sample["action"] == kind
                    and sample.get("view_mode", "GROWTH") == cfg.get("view_mode", "GROWTH")
                    and same_cache_work
                    and 0.5 <= cfg["study_radius_km"] / sample["radius_km"] <= 2
                    and math.isfinite(sample["seconds"])
                    and sample["seconds"] > 0
                ):
                    records.append(sample)
            except (KeyError, TypeError, ZeroDivisionError):
                continue
        if records:
            record = records[-1]
            factor = (cfg["study_radius_km"] / record["radius_km"]) ** 2
            if kind == "SIMULATE":
                factor *= (cfg["end_year"] - cfg["base_year"] + 1) / max(1, record["years"])
            minutes = max(0.5, record["seconds"] * factor / 60)
            lines.append(
                f"{label}: roughly {max(1, round(minutes * 0.5))}–{max(2, math.ceil(minutes * 2))} min (measured)"
            )
        else:
            lines.append(label + ": not enough local measurements yet")
    lines.append("Density/network change times; visible build overlaps calculation.")
    return lines


def show_setup_sidebar():
    """Switch after Text Editor execution, then select the registered sidebar tab."""
    if bpy.app.background or bpy.context.window is None:
        return
    window = bpy.context.window
    attempts = 0
    properties_tabs = [
        area.spaces.active.context for area in window.screen.areas if area.type == "PROPERTIES"
    ]

    def reveal():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            layout = bpy.data.workspaces.get("Layout")
            if layout is not None:
                window.workspace = layout
            for index, area in enumerate(a for a in window.screen.areas if a.type == "PROPERTIES"):
                if properties_tabs:
                    area.spaces.active.context = properties_tabs[
                        min(index, len(properties_tabs) - 1)
                    ]
            return 0.15
        found = False
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            area.spaces.active.show_region_ui = True
            area.tag_redraw()
            for region in area.regions:
                if region.type == "UI":
                    try:
                        with bpy.context.temp_override(window=window, area=area, region=region):
                            region.active_panel_category = "MetroSim 26"
                        found = True
                    except (AttributeError, TypeError, RuntimeError):
                        pass  # Panels may need one redraw after switching workspaces.
        return None if found or attempts >= 10 else 0.15

    bpy.app.timers.register(reveal, first_interval=0.05)


def open_setup(cfg, reset=True):
    global _REGISTERED, _SETUP_CONFIG
    assert bpy is not None
    if _cancel_on_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_cancel_on_load)
    if not _REGISTERED:
        for cls in REGISTERED_CLASSES:
            if not getattr(cls, "is_registered", False):
                bpy.utils.register_class(cls)
        bpy.types.Scene.metrosim26_setup = bpy.props.PointerProperty(type=METROSIM26_PG_setup)
        atexit.register(_cancel_on_exit)
        _REGISTERED = True
    if reset:
        _SETUP_CONFIG = dict(cfg)
        apply_setup(bpy.context.scene.metrosim26_setup, cfg)
        settings = bpy.context.scene.metrosim26_setup
        if settings.include_defaults_version < 1:
            for key in LAYER_DEFAULTS:
                setattr(settings, "include_" + key, True)
            settings.include_defaults_version = 1
        bpy.context.scene.metrosim26_setup.show_include = False
        remove_default_cube()
        show_setup_sidebar()
        if not bpy.app.background and not bpy.app.timers.is_registered(location_watch):
            bpy.app.timers.register(location_watch, first_interval=0.5)
    print("[MetroSim26] Open Sidebar (N) > MetroSim 26 to choose an area.")


if bpy is not None:
    METROSIM26_PG_setup = make_setup_type(
        LAYER_DEFAULTS=LAYER_DEFAULTS,
        palette_changed=palette_changed,
        preset_changed=preset_changed,
    )


if bpy is not None:
    OPERATOR_CLASSES = make_operator_types(
        get_run=lambda: _ACTIVE_RUN,
        setup_config=setup_config,
        start_blender_job=start_blender_job,
        storage_path=storage_path,
    )
    REGISTERED_CLASSES = (METROSIM26_PG_setup, *OPERATOR_CLASSES, METROSIM26_PT_status)
else:
    REGISTERED_CLASSES = ()


def _register_extension_operator():
    # Operators need not be exposed by their Python class name on bpy.types.
    # The class itself records registration even when that attribute is absent.
    if not getattr(METROSIM26_OT_extend, "is_registered", False):
        bpy.utils.register_class(METROSIM26_OT_extend)


def show_extension_dialog():
    """Register the reusable extension control without launching a calculation."""
    assert bpy is not None
    _register_extension_operator()
    bpy.ops.metrosim26.extend_saved_run("INVOKE_DEFAULT")


if bpy is not None:
    METROSIM26_OT_extend = OPERATOR_CLASSES[0]
