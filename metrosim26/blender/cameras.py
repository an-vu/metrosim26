"""Overview and detail cameras, framing, and preservation of user adjustments."""

from __future__ import annotations

import json
import math

import numpy as np

try:
    import bpy
    from mathutils import Vector
except ModuleNotFoundError:
    bpy = None
    Vector = None


def camera_view_signature(camera):
    return {
        "matrix": [float(value) for row in camera.matrix_world for value in row],
        "type": camera.data.type,
        "lens": float(camera.data.lens),
        "scale": float(camera.data.ortho_scale),
        "shift": [float(camera.data.shift_x), float(camera.data.shift_y)],
    }


def fit_overview_to_geometry(scene):
    """Fit actual generated vertices, rather than a circle extruded to the tallest height."""
    camera = scene.camera
    if (
        camera is None
        or camera.data.type != "ORTHO"
        or camera.get("metrosim26_camera_role") != "overview"
        or camera_was_adjusted(camera)
    ):
        return
    scene.view_layers[0].update()
    inverse = np.asarray(camera.matrix_world.inverted())
    low, high = np.full(2, np.inf), np.full(2, -np.inf)
    for obj in scene.objects:
        if (
            obj.type != "MESH"
            or not obj.get("metrosim26_generated_visualization")
            or not len(obj.data.vertices)
        ):
            continue
        vertices = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
        obj.data.vertices.foreach_get("co", vertices)
        transform = inverse @ np.asarray(obj.matrix_world)
        projected = vertices.reshape(-1, 3) @ transform[:2, :3].T + transform[:2, 3]
        low = np.minimum(low, projected.min(axis=0))
        high = np.maximum(high, projected.max(axis=0))
    if not np.isfinite(low).all():
        return
    frame = camera.data.view_frame(scene=scene)
    width = max(v.x for v in frame) - min(v.x for v in frame)
    height = max(v.y for v in frame) - min(v.y for v in frame)
    camera.data.ortho_scale *= 1.015 * max((high[0] - low[0]) / width, (high[1] - low[1]) / height)
    midpoint = (low + high) / 2
    camera.location += camera.matrix_world.to_quaternion() @ Vector((midpoint[0], midpoint[1], 0))
    scene.view_layers[0].update()
    camera["automatic_view"] = json.dumps(camera_view_signature(camera))


def camera_was_adjusted(camera):
    if camera.get("preserve_custom_view") or not camera.get("metrosim26_generated_visualization"):
        return True
    try:
        previous = json.loads(camera["automatic_view"])
        current = camera_view_signature(camera)
        return any(
            not np.allclose(previous[key], current[key], atol=1e-5)
            if key != "type"
            else previous[key] != current[key]
            for key in current
        )
    except (KeyError, ValueError, TypeError):
        return False


def configure_cameras(
    cfg, grid, scene, collection, own=lambda database, item: item, source_camera=None
):
    assert Vector is not None, "This operation requires Blender."
    assert bpy is not None, "This operation requires Blender."
    center = Vector(
        (
            (grid.min_x + grid.max_x) * 0.5,
            (grid.min_y + grid.max_y) * 0.5,
            (getattr(grid, "min_z", 0) + getattr(grid, "max_z", 0.05)) / 2,
        )
    )
    span = max(grid.width, grid.height, 0.1)
    data = own(bpy.data.cameras, bpy.data.cameras.new("MetroSim26_Overview_Camera"))
    data["metrosim26_generated_visualization"] = True
    camera = own(bpy.data.objects, bpy.data.objects.new("MetroSim26_Overview_Camera", data))
    camera["metrosim26_generated_visualization"] = True
    collection.objects.link(camera)
    camera.location = center + Vector((0.65 * span, -0.85 * span, 0.9 * span))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    data.type = "ORTHO"
    data.clip_start = 0.001
    data.clip_end = span * 20.0
    data.ortho_scale = 1.0
    camera["metrosim26_camera_role"] = "overview"
    scene.camera = camera
    scene.render.resolution_x, scene.render.resolution_y = cfg.get(
        "render_resolution", [1920, 1080]
    )
    # Fit study corners using Blender's actual camera frame and aspect ratio.
    inverse_rotation = camera.rotation_euler.to_quaternion().inverted()
    corners = [
        inverse_rotation @ (Vector((x, y, z)) - center)
        for x in (grid.min_x, grid.max_x)
        for y in (grid.min_y, grid.max_y)
        for z in (getattr(grid, "min_z", 0.0), getattr(grid, "max_z", 0.3))
    ]
    if cfg.get("study_radius_km"):
        radius = cfg["study_radius_km"]
        corners = [
            inverse_rotation @ (Vector((radius * math.cos(a), radius * math.sin(a), z)) - center)
            for a in np.linspace(0, 2 * math.pi, 96, endpoint=False)
            for z in (getattr(grid, "min_z", 0), getattr(grid, "max_z", 0.3))
        ]
    frame = data.view_frame(scene=scene)
    unit_half_width = max(abs(vertex.x) for vertex in frame)
    unit_half_height = max(abs(vertex.y) for vertex in frame)
    data.ortho_scale = 1.015 * max(
        max(abs(vertex.x) for vertex in corners) / unit_half_width,
        max(abs(vertex.y) for vertex in corners) / unit_half_height,
    )
    projected_center = Vector(
        (
            (max(v.x for v in corners) + min(v.x for v in corners)) / 2,
            (max(v.y for v in corners) + min(v.y for v in corners)) / 2,
            0,
        )
    )
    camera.location += camera.rotation_euler.to_quaternion() @ projected_center
    if source_camera is not None:
        # Own an independent copy; the previous scene and its camera stay untouched.
        inherited = own(bpy.data.cameras, source_camera.data.copy())
        inherited["metrosim26_generated_visualization"] = True
        camera.data = inherited
        camera.matrix_world = source_camera.matrix_world.copy()
        camera["preserve_custom_view"] = True
    else:
        # Record the fitted pose so future builds distinguish it from user edits.
        scene.view_layers[0].update()
        camera["automatic_view"] = json.dumps(camera_view_signature(camera))
    detail_data = own(bpy.data.cameras, bpy.data.cameras.new("MetroSim26_Map_Detail"))
    detail_data["metrosim26_generated_visualization"] = True
    detail_data.type = "ORTHO"
    detail_data.clip_start = 0.001
    detail_data.clip_end = span * 20
    # A neighborhood-sized view, centered at the requested coordinates.
    detail_data.ortho_scale = max(0.2, min(span * 0.6, 1.5))
    detail = own(bpy.data.objects, bpy.data.objects.new("MetroSim26_Map_Detail", detail_data))
    detail["metrosim26_generated_visualization"] = True
    detail["metrosim26_camera_role"] = "detail"
    collection.objects.link(detail)
    target = (
        Vector((0, 0, getattr(grid, "min_z", 0) + 0.08)) if cfg.get("study_radius_km") else center
    )
    detail.location = target + Vector((0.45, -0.65, 1.05)) * span
    detail.rotation_euler = (target - detail.location).to_track_quat("-Z", "Y").to_euler()
    return camera
