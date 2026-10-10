"""Sun, fill, rim, world, and exposure. Does not create or move cameras."""

from __future__ import annotations

import math

try:
    import bpy
    from mathutils import Vector
except ModuleNotFoundError:
    bpy = None
    Vector = None


def configure_lighting(grid, scene, collection, own=lambda database, item: item):
    center = Vector(
        (
            (grid.min_x + grid.max_x) * 0.5,
            (grid.min_y + grid.max_y) * 0.5,
            (getattr(grid, "min_z", 0) + getattr(grid, "max_z", 0.05)) / 2,
        )
    )
    span = max(grid.width, grid.height, 0.1)
    light_data = own(bpy.data.lights, bpy.data.lights.new("MetroSim26_Sun", "SUN"))
    light_data["metrosim26_generated_visualization"] = True
    light_data.energy = 2.0
    light_data.color = (1.0, 0.96, 0.89)
    light_data.angle = math.radians(1.25)
    sun = own(bpy.data.objects, bpy.data.objects.new("MetroSim26_Sun", light_data))
    sun["metrosim26_generated_visualization"] = True
    collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), math.radians(-20), math.radians(-35))
    # Broad fill and rear light reveal facades without flattening the sun shadows.
    # Area power scales with distance squared so the balance survives radius changes.
    for name, offset, strength, color, size in (
        ("Sky_Fill", (-0.9, -0.5, 0.8), 3.0, (0.78, 0.87, 1.0), 1.6),
        ("Edge_Light", (0.3, 0.9, 1.0), 2.5, (1.0, 0.95, 0.86), 1.2),
    ):
        area_data = own(bpy.data.lights, bpy.data.lights.new("MetroSim26_" + name, "AREA"))
        area_data["metrosim26_generated_visualization"] = True
        area_data.shape = "DISK"
        area_data.size = span * size
        area_data.energy = span * span * strength
        area_data.color = color
        lamp = own(bpy.data.objects, bpy.data.objects.new("MetroSim26_" + name, area_data))
        lamp["metrosim26_generated_visualization"] = True
        collection.objects.link(lamp)
        lamp.location = center + Vector(offset) * span
        lamp.rotation_euler = (center - lamp.location).to_track_quat("-Z", "Y").to_euler()
    world = own(bpy.data.worlds, bpy.data.worlds.new("MetroSim26_Atmosphere"))
    world["metrosim26_generated_visualization"] = True
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.78, 0.83, 0.91, 1.0)
    background.inputs["Strength"].default_value = 0.14
    scene.world = world
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.exposure = -0.35
    scene.view_settings.gamma = 1
    for look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
        try:
            scene.view_settings.look = look
            break
        except TypeError:
            continue
