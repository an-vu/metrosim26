"""Materials, viewport state, timeline visibility, and presentation coordination."""

from __future__ import annotations

from pathlib import Path

try:
    import bpy
except ModuleNotFoundError:
    bpy = None

from .cameras import camera_view_signature as camera_view_signature
from .cameras import camera_was_adjusted as camera_was_adjusted
from .cameras import configure_cameras
from .cameras import fit_overview_to_geometry as fit_overview_to_geometry
from .lighting import configure_lighting


def _configure_camera_and_lighting(
    cfg, grid, scene, collection, own=lambda database, item: item, source_camera=None
):
    camera = configure_cameras(cfg, grid, scene, collection, own, source_camera)
    configure_lighting(grid, scene, collection, own)
    return camera


PALETTES = {
    "APPLE": {
        "existing": (0.72, 0.70, 0.66),
        "road": (0.91, 0.90, 0.86),
        "park": (0.47, 0.66, 0.35),
        "water": (0.30, 0.61, 0.82),
        "ground": (0.85, 0.84, 0.78),
    },
    "NATURAL": {
        "existing": (0.65, 0.63, 0.58),
        "road": (0.18, 0.20, 0.22),
        "park": (0.28, 0.48, 0.30),
        "water": (0.12, 0.38, 0.55),
        "ground": (0.55, 0.58, 0.45),
    },
    "SLATE_EARTH": {
        "existing": (0.56, 0.54, 0.50),
        "road": (0.12, 0.14, 0.16),
        "park": (0.30, 0.39, 0.21),
        "water": (0.13, 0.29, 0.38),
        "ground": (0.62, 0.55, 0.43),
    },
    "BRIGHT": {
        "existing": (0.9, 0.84, 0.70),
        "road": (0.20, 0.24, 0.32),
        "park": (0.27, 0.66, 0.40),
        "water": (0.08, 0.56, 0.8),
        "ground": (0.72, 0.74, 0.61),
    },
    "MONO": {
        "existing": (0.72, 0.72, 0.72),
        "road": (0.20, 0.20, 0.20),
        "park": (0.44, 0.44, 0.44),
        "water": (0.35, 0.35, 0.35),
        "ground": (0.58, 0.58, 0.58),
    },
}


def activate_scene_preserving_view(scene):
    """Switch scenes without changing any existing 3D viewport framing."""
    if bpy.context.window is None:
        return
    properties = (
        "view_distance",
        "view_location",
        "view_rotation",
        "view_perspective",
        "view_camera_zoom",
        "view_camera_offset",
    )
    views = []
    tabs = (
        [
            (area.spaces.active, area.spaces.active.context)
            for area in bpy.context.screen.areas
            if area.type == "PROPERTIES"
        ]
        if bpy.context.screen
        else []
    )
    for area in bpy.context.screen.areas if bpy.context.screen else []:
        if area.type == "VIEW_3D":
            region = area.spaces.active.region_3d
            values = {}
            for key in properties:
                value = getattr(region, key)
                values[key] = value.copy() if hasattr(value, "copy") else value
            views.append((region, values))
    bpy.context.window.scene = scene
    for space, tab in tabs:
        space.context = tab
    for region, values in views:
        for key, value in values.items():
            setattr(region, key, value)


def collapse_generated_outliner():
    if bpy.context.screen is None:
        return
    for area in bpy.context.screen.areas:
        if area.type != "OUTLINER":
            continue
        region = next((region for region in area.regions if region.type == "WINDOW"), None)
        if region is None:
            continue
        with bpy.context.temp_override(area=area, region=region):
            # Close descendants, then expose the root's collection list.
            for _ in range(16):
                bpy.ops.outliner.show_one_level(open=False)
            bpy.ops.outliner.show_one_level(open=True)
        area.tag_redraw()


def _new_material(name, color, roughness=0.75, metallic=0.0):
    assert bpy is not None, "This operation requires Blender."
    material = bpy.data.materials.new(name)
    material["metrosim26_generated_visualization"] = True
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
        print("[MetroSim26] " + message)
        scene.cycles.device = "CPU"
        scene["render_backend"] = "CPU fallback: " + failure
    else:
        print("[MetroSim26] " + scene["render_backend"])


def finalize_blender_output(cfg, scene):
    """Optionally save a .blend, final still, and/or numbered PNG animation."""
    assert bpy is not None, "This operation requires Blender."
    output_dir = Path(cfg.get("run_directory", cfg["output_dir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    animation_dir = output_dir / "frames"
    scene.render.filepath = str(animation_dir / "metrosim26_")
    if cfg.get("save_blend", False):
        path = output_dir / "simulation.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        print("[MetroSim26] Saved visualization: " + str(path))
    original_frame = scene.frame_current
    try:
        if cfg.get("render_final", False):
            scene.frame_set(scene.frame_end)
            scene.render.filepath = str(output_dir / f"metrosim26_{cfg['end_year']}.png")
            bpy.ops.render.render(write_still=True, scene=scene.name)
        if cfg.get("render_animation", False):
            animation_dir.mkdir(parents=True, exist_ok=True)
            scene.render.filepath = str(animation_dir / "metrosim26_")
            bpy.ops.render.render(animation=True, scene=scene.name)
    finally:
        scene.frame_set(original_frame)
        scene.render.filepath = str(animation_dir / "metrosim26_")
