"""Disposable circular map reveal; final scene geometry is never edited."""

import math
import time

from .presentation import PALETTES, activate_scene_preserving_view

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


def reveal_map(importer, scene, duration=8.0):
    """Preview on disposable object/material copies; leave final geometry intact."""
    layer = scene.view_layers[0]
    start = float(importer.cfg.get("expand_inner_radius_km", 0))
    end = float(importer.cfg["study_radius_km"])
    span = max(0.001, end - start)
    originals, copies, materials, shaders, views = [], [], [], {}, []
    buildings = []
    importer.revealing = True
    try:
        for obj in list(scene.objects):
            if obj.type != "MESH" or not obj.get("metrosim26_generated_visualization"):
                continue
            originals.append((obj, obj.hide_get(view_layer=layer)))
            obj.hide_set(True, view_layer=layer)
            preview = obj.copy()
            scene.collection.objects.link(preview)
            preview.hide_set(False, view_layer=layer)
            copies.append(preview)
            kind = obj.get("feature_kind", "")
            if kind == "building":
                from mathutils import Vector

                box = [obj.matrix_world @ Vector(v) for v in obj.bound_box]
                distance = math.hypot(sum(v.x for v in box) / 8, sum(v.y for v in box) / 8)
                bottom = min(v[2] for v in obj.bound_box)
                buildings.append((preview, distance, bottom, obj.scale.z, obj.location.z))
            lag = 0.035 if kind == "road" else 0.07 if kind == "building" else 0.0
            for slot in preview.material_slots:
                original = slot.material
                if original is None:
                    continue
                key = (original.as_pointer(), lag)
                if key not in shaders:
                    material = original.copy()
                    materials.append(material)
                    material.use_nodes = True
                    nodes, links = material.node_tree.nodes, material.node_tree.links
                    output = next(
                        n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output
                    )
                    surface = output.inputs["Surface"].links[0].from_socket
                    position = nodes.new("ShaderNodeNewGeometry")
                    separate = nodes.new("ShaderNodeSeparateXYZ")
                    xy = nodes.new("ShaderNodeCombineXYZ")
                    links.new(position.outputs["Position"], separate.inputs[0])
                    links.new(separate.outputs["X"], xy.inputs["X"])
                    links.new(separate.outputs["Y"], xy.inputs["Y"])
                    distance = nodes.new("ShaderNodeVectorMath")
                    distance.operation = "LENGTH"
                    links.new(xy.outputs[0], distance.inputs[0])
                    radius = nodes.new("ShaderNodeValue")
                    radius.outputs[0].default_value = start
                    inside = nodes.new("ShaderNodeMath")
                    inside.operation = "LESS_THAN"
                    links.new(distance.outputs["Value"], inside.inputs[0])
                    links.new(radius.outputs[0], inside.inputs[1])
                    transparent = nodes.new("ShaderNodeBsdfTransparent")
                    # A soft, narrow highlight follows the terrain surface.
                    delta = nodes.new("ShaderNodeMath")
                    delta.operation = "SUBTRACT"
                    links.new(radius.outputs[0], delta.inputs[0])
                    links.new(distance.outputs["Value"], delta.inputs[1])
                    fade = nodes.new("ShaderNodeMapRange")
                    fade.clamp = True
                    fade.inputs["From Min"].default_value = 0
                    fade.inputs["From Max"].default_value = span * 0.012
                    fade.inputs["To Min"].default_value = 0.25 if not kind else 0
                    fade.inputs["To Max"].default_value = 0
                    links.new(delta.outputs[0], fade.inputs["Value"])
                    glow = nodes.new("ShaderNodeEmission")
                    glow.inputs["Color"].default_value = (
                        *importer.cfg.get("palette", PALETTES["NATURAL"])["water"],
                        1,
                    )
                    links.new(fade.outputs["Result"], glow.inputs["Strength"])
                    add = nodes.new("ShaderNodeAddShader")
                    links.new(surface, add.inputs[0])
                    links.new(glow.outputs[0], add.inputs[1])
                    mix = nodes.new("ShaderNodeMixShader")
                    links.new(inside.outputs[0], mix.inputs[0])
                    links.new(transparent.outputs[0], mix.inputs[1])
                    links.new(add.outputs[0], mix.inputs[2])
                    links.new(mix.outputs[0], output.inputs["Surface"])
                    shaders[key] = (material, radius, lag)
                slot.link = "OBJECT"
                slot.material = shaders[key][0]
            if len(copies) % 128 == 0:
                importer.message = f"Preparing circular reveal: {len(copies):,} objects"
                yield
        activate_scene_preserving_view(scene)
        for area in bpy.context.screen.areas if bpy.context.screen else []:
            if area.type == "VIEW_3D":
                shading = area.spaces.active.shading
                views.append((shading, shading.type))
                if shading.type in {"SOLID", "WIREFRAME"}:
                    shading.type = "MATERIAL"
        elapsed, last = 0.0, time.monotonic()
        while elapsed < duration:
            now = time.monotonic()
            elapsed += min(0.1, now - last)  # Pause/resume cannot jump the sweep.
            last = now
            progress = min(1.0, elapsed / duration)
            sweep = progress * 1.12
            for material, radius, lag in shaders.values():
                radius.outputs[0].default_value = start + span * max(0.0, sweep - lag)
            for preview, distance, bottom, scale, location in buildings:
                if distance <= start:
                    continue
                fraction = max(0.0, min(1.0, (sweep - (distance - start) / span - 0.07) / 0.05))
                fraction = fraction * fraction * (3 - 2 * fraction)
                preview.scale.z = scale * max(0.001, fraction)
                preview.location.z = location + bottom * (scale - preview.scale.z)
            importer.message = f"Revealing map: {round(progress * 100)}%"
            yield
    finally:
        for obj in copies:
            bpy.data.objects.remove(obj, do_unlink=True)
        for material in materials:
            bpy.data.materials.remove(material)
        for obj, hidden in originals:
            obj.hide_set(hidden, view_layer=layer)
        for shading, original_type in views:
            if shading.type == "MATERIAL" and original_type in {"SOLID", "WIREFRAME"}:
                shading.type = original_type
        importer.revealing = False
