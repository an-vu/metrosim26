"""Blender visualization, timeline, materials, and render configuration."""
from __future__ import annotations

from pathlib import Path
import json
import math
from omaha_workers import (
    mesh_triangles,
)
try:
    import bpy
    from mathutils import Vector
except ModuleNotFoundError:
    bpy = None
    Vector = None

def _oriented_ring(ring, counterclockwise=True):
    points = [(float(p[0]), float(p[1])) for p in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    signed = sum(a[0] * b[1] - b[0] * a[1]
                 for a, b in zip(points, points[1:] + points[:1]))
    if (signed > 0.0) != counterclockwise:
        points.reverse()
    return points


def _surface_geometry(polygon, elevation_km=0.0):
    """Pure geometry helper; triangulation preserves multipolygon courtyard holes."""
    vertices, faces = [], []
    for triangle in mesh_triangles(polygon):
        triangle = _oriented_ring(triangle)
        if len(triangle) != 3:
            continue
        start = len(vertices)
        vertices.extend((x, y, elevation_km) for x, y in triangle)
        faces.append((start, start + 1, start + 2))
    return vertices, faces


def _building_geometry(polygon, height_m, z0_m=0.0):
    """Extrude actual footprints, including courtyard walls and a holed roof."""
    # Centimeter-scale offsets separate flat presentation surfaces without
    # visibly lifting buildings off the ground.
    floor = 0.00008 + float(z0_m) / 1000.0
    roof = floor + max(0.1, float(height_m)) / 1000.0
    vertices, faces = _surface_geometry(polygon, roof)
    lower_vertices, lower_faces = _surface_geometry(polygon, floor)
    offset = len(vertices)
    vertices.extend(lower_vertices)
    faces.extend(tuple(offset + i for i in reversed(face)) for face in lower_faces)
    rings = [(polygon['outer'], True)]
    rings.extend((hole, False) for hole in polygon.get('holes', []))
    for ring, counterclockwise in rings:
        ring = _oriented_ring(ring, counterclockwise)
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if a == b:
                continue
            offset = len(vertices)
            vertices.extend(((a[0], a[1], floor), (b[0], b[1], floor),
                             (b[0], b[1], roof), (a[0], a[1], roof)))
            faces.append((offset, offset + 1, offset + 2, offset + 3))
    return vertices, faces


def _road_geometry(points, width_km, elevation_km=0.00002):
    """Flat road strips with bounded miter joins; no cylindrical road tubes."""
    clean = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not clean or math.dist(clean[-1], xy) > 1e-9:
            clean.append(xy)
    if len(clean) < 2:
        return [], []
    normals = []
    for a, b in zip(clean, clean[1:]):
        length = math.dist(a, b)
        normals.append((-(b[1] - a[1]) / length, (b[0] - a[0]) / length))
    half = max(float(width_km), 0.001) * 0.5
    vertices, faces = [], []
    for i, (x, y) in enumerate(clean):
        before = normals[max(0, i - 1)]
        after = normals[min(i, len(normals) - 1)]
        mx, my = before[0] + after[0], before[1] + after[1]
        length = math.hypot(mx, my)
        if length < 1e-8:
            mx, my = after
            extent = half
        else:
            mx, my = mx / length, my / length
            dot = max(0.25, mx * after[0] + my * after[1])
            extent = min(half / dot, half * 2.0)
        vertices.extend(((x + mx * extent, y + my * extent, elevation_km),
                         (x - mx * extent, y - my * extent, elevation_km)))
        if i:
            start = 2 * i
            faces.append((start - 2, start - 1, start + 1, start))
    return vertices, faces


def _new_material(name, color, roughness=0.75, metallic=0.0):
    material = bpy.data.materials.new(name)
    material['omaha_generated_visualization'] = True
    material.use_nodes = True
    material.diffuse_color = (*color, 1.0)
    shader = material.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (*color, 1.0)
    shader.inputs['Roughness'].default_value = roughness
    shader.inputs['Metallic'].default_value = metallic
    return material


def _set_object_lifetime(obj, start_year, end_year, base_year):
    """Saved, stateless visibility for viewport and rendering in both scrub directions.

    Only Blender's built-in frame variable and simple expressions are used.
    No handlers, timers, custom driver namespace, or legacy Action API is needed.
    end_year is exclusive, so replacement versions never coexist in a frame.
    """
    first = int(start_year) - int(base_year) + 1
    expression = f'frame < {first}'
    if end_year is not None:
        last = int(end_year) - int(base_year) + 1
        expression += f' or frame >= {last}'
    obj['construction_start_year'] = int(start_year)
    obj['demolition_year_exclusive'] = int(end_year) if end_year is not None else -1
    for property_name in ('hide_viewport', 'hide_render'):
        driver = obj.driver_add(property_name).driver
        driver.type = 'SCRIPTED'
        driver.expression = expression


class _MeshBatch:
    """Bounded mesh chunks, grouped by shared lifetime and material."""
    def __init__(self, collection, name, material, max_vertices=120000, lifetime=None):
        self.collection = collection
        self.name = name
        self.material = material
        self.max_vertices = max(1000, int(max_vertices))
        self.lifetime = lifetime
        self.vertices = []
        self.faces = []
        self.chunk = 0
        self.objects = []

    def add(self, vertices, faces):
        if not faces:
            return
        if self.vertices and len(self.vertices) + len(vertices) > self.max_vertices:
            self.flush()
        # One unusually complex OSM polygon can exceed the target chunk size;
        # retaining its footprint is preferable to dropping or simplifying it.
        offset = len(self.vertices)
        self.vertices.extend(vertices)
        self.faces.extend(tuple(offset + index for index in face) for face in faces)

    def flush(self):
        if not self.faces:
            return
        name = f'{self.name}_{self.chunk:03d}'
        mesh = bpy.data.meshes.new(name)
        mesh['omaha_generated_visualization'] = True
        mesh.from_pydata(self.vertices, [], self.faces)
        mesh.update()
        mesh.materials.append(self.material)
        obj = bpy.data.objects.new(name, mesh)
        self.collection.objects.link(obj)
        obj['omaha_generated_visualization'] = True
        if self.lifetime is not None:
            _set_object_lifetime(obj, *self.lifetime)
        self.objects.append(obj)
        self.chunk += 1
        self.vertices = []
        self.faces = []


def _configure_cycles(cfg, scene):
    """Use the RTX 3080 Ti via OptiX; clearly announce any CPU fallback."""
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = int(cfg.get('render_samples', 64 if cfg['preview_mode'] else 128))
    scene.cycles.preview_samples = int(cfg.get('preview_samples', 24))
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.use_denoising = True
    scene.cycles.seed = int(cfg['seed']) % 2147483647
    scene.cycles.use_animated_seed = False
    scene.render.use_persistent_data = True
    failure = None
    try:
        addon = bpy.context.preferences.addons.get('cycles')
        if addon is None:
            raise RuntimeError('Cycles preferences are unavailable')
        preferences = addon.preferences
        preferences.compute_device_type = 'OPTIX'
        preferences.refresh_devices()
        optix = [device for device in preferences.devices if device.type == 'OPTIX']
        if not optix:
            raise RuntimeError('no OptiX device was detected')
        preferred_name = cfg.get('render_device_name', '3080 Ti').lower()
        selected = [device for device in optix if preferred_name in device.name.lower()]
        if not selected:
            selected = optix
        selected_ids = {device.id for device in selected}
        for device in preferences.devices:
            device.use = device.type == 'OPTIX' and device.id in selected_ids
        scene.cycles.device = 'GPU'
        scene['render_backend'] = 'OPTIX: ' + ', '.join(device.name for device in selected)
    except (RuntimeError, TypeError, AttributeError) as exc:
        failure = str(exc)
    if failure is not None:
        message = f'OptiX unavailable ({failure}). Cycles will render on the CPU.'
        if cfg.get('require_optix', False):
            raise RuntimeError(message + ' Set require_optix=False to permit CPU rendering.')
        print('[Omaha] ' + message)
        scene.cycles.device = 'CPU'
        scene['render_backend'] = 'CPU fallback: ' + failure
    else:
        print('[Omaha] ' + scene['render_backend'])


def _configure_camera_and_lighting(cfg, grid, scene, collection):
    center = Vector(((grid.min_x + grid.max_x) * 0.5,
                     (grid.min_y + grid.max_y) * 0.5, 0.025))
    span = max(grid.width, grid.height, 0.1)
    data = bpy.data.cameras.new('Omaha_Overview_Camera')
    data['omaha_generated_visualization'] = True
    camera = bpy.data.objects.new('Omaha_Overview_Camera', data)
    camera['omaha_generated_visualization'] = True
    collection.objects.link(camera)
    camera.location = center + Vector((0.65 * span, -0.85 * span, 0.9 * span))
    camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
    data.type = 'ORTHO'
    data.clip_start = 0.001
    data.clip_end = span * 20.0
    data.ortho_scale = 1.0
    scene.camera = camera
    # Fit study corners using Blender's actual camera frame and aspect ratio.
    inverse_rotation = camera.rotation_euler.to_quaternion().inverted()
    corners = [inverse_rotation @ (Vector((x, y, z)) - center)
               for x in (grid.min_x, grid.max_x)
               for y in (grid.min_y, grid.max_y) for z in (0.0, 0.3)]
    frame = data.view_frame(scene=scene)
    unit_half_width = max(abs(vertex.x) for vertex in frame)
    unit_half_height = max(abs(vertex.y) for vertex in frame)
    data.ortho_scale = 1.12 * max(
        max(abs(vertex.x) for vertex in corners) / unit_half_width,
        max(abs(vertex.y) for vertex in corners) / unit_half_height)
    light_data = bpy.data.lights.new('Omaha_Sun', 'SUN')
    light_data['omaha_generated_visualization'] = True
    light_data.energy = 2.5
    light_data.angle = math.radians(12.0)
    sun = bpy.data.objects.new('Omaha_Sun', light_data)
    sun['omaha_generated_visualization'] = True
    collection.objects.link(sun)
    sun.rotation_euler = (math.radians(27), math.radians(-19), math.radians(-28))
    world = bpy.data.worlds.new('Omaha_Atmosphere')
    world['omaha_generated_visualization'] = True
    world.use_nodes = True
    background = world.node_tree.nodes.get('Background')
    background.inputs['Color'].default_value = (0.68, 0.75, 0.84, 1.0)
    background.inputs['Strength'].default_value = 0.45
    scene.world = world
    scene.view_settings.view_transform = 'AgX'
    return camera


def _cleanup_previous_scenes(keep_scene):
    """Release previous wholly owned scenes; preserve additions and shared objects."""
    def descendants(collection):
        result = []
        for child in collection.children:
            result.append(child)
            result.extend(descendants(child))
        return result

    for old in list(bpy.data.scenes):
        if old == keep_scene or not old.get('omaha_simulation', False):
            continue
        collections = descendants(old.collection)
        collection_set = set(collections)
        objects = list(old.objects)
        safe_collections = all(collection.get('omaha_generated_visualization', False)
                               and collection.users == 1 for collection in collections)
        safe_objects = all(obj.get('omaha_generated_visualization', False)
                           and len(obj.users_scene) == 1
                           and all(collection in collection_set for collection in obj.users_collection)
                           for obj in objects)
        if not (safe_collections and safe_objects):
            print(f'[Omaha] Preserved modified or shared prior scene: {old.name}')
            continue
        for window in bpy.context.window_manager.windows:
            if window.scene == old:
                window.scene = keep_scene
        bpy.data.scenes.remove(old)
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for collection in reversed(collections):
            bpy.data.collections.remove(collection)
    # Restrict orphan cleanup to script-owned IDs. Never purge the user's data.
    for database in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights,
                     bpy.data.worlds, bpy.data.materials):
        for data in list(database):
            if data.users == 0 and data.get('omaha_generated_visualization', False):
                database.remove(data)


def _visual_lifetime_records(versions):
    """Coalesce persistent buildings across infill versions before meshing.

    An unchanged house belongs to one construction/demolition cohort, even if
    its site received infill many times. This avoids duplicating its mesh once
    per snapshot while retaining exclusive replacement lifetimes.
    """
    records = {}
    for site in sorted(versions, key=lambda s: (s['start_year'], s['id'], s['version'])):
        for category in ('buildings', 'surfaces'):
            for item in site.get(category, []):
                key = (category, item['id'])
                if key in records:
                    existing = records[key]
                    if existing['end_year'] != site['start_year']:
                        raise ValueError(f"Noncontiguous or overlapping lifetime for {item['id']}")
                    if existing[category][0] != item:
                        raise ValueError(f"Persistent geometry changed without a new ID: {item['id']}")
                    existing['end_year'] = site.get('end_year')
                else:
                    records[key] = {
                        'id': item['id'], 'version': 0,
                        'start_year': int(site['start_year']),
                        'end_year': site.get('end_year'),
                        'buildings': [item] if category == 'buildings' else [],
                        'surfaces': [item] if category == 'surfaces' else [],
                    }
    return sorted(records.values(), key=lambda record: (
        record['start_year'], record['end_year'] if record['end_year'] is not None else 99999,
        record['id']))


def build_blender_scene(cfg, grid, baseline, result):
    """Visualize stored results in a new isolated scene; return bpy.types.Scene.

    Previous unmodified script-owned scenes are released after a successful build.
    Other scenes and shared or user-added objects are preserved. bpy access stays
    on Blender's main thread, after the simulation finishes.
    """
    if bpy is None or Vector is None:
        raise RuntimeError('Scene construction must run inside Blender 5.2.2.')
    if bpy.app.version < (5, 2, 2):
        raise RuntimeError('This script targets Blender 5.2.2; older versions are unsupported.')
    scene = bpy.data.scenes.new('OMAHA_SIMULATION')
    scene['omaha_simulation'] = True
    scene['model_note'] = 'Experimental demand scenario constrained by real OSM and LODES; not a forecast.'
    scene['year_at_frame_1'] = int(cfg['base_year'])
    scene['seed'] = int(cfg['seed'])
    scene['configuration_json'] = json.dumps(cfg, sort_keys=True)
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1000.0
    scene.unit_settings.length_unit = 'KILOMETERS'
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    collections = {}
    for key, name in (('baseline', 'Observed Omaha'), ('land', 'Water and Parks'),
                      ('future', 'Scenario Development'), ('stage', 'Presentation')):
        collection = bpy.data.collections.new(name)
        collection['omaha_generated_visualization'] = True
        scene.collection.children.link(collection)
        collections[key] = collection
    colors = {
        'existing': (0.56, 0.55, 0.52), 'road': (0.19, 0.21, 0.22),
        'future_road': (0.23, 0.26, 0.27), 'parking': (0.36, 0.37, 0.36),
        'water': (0.11, 0.27, 0.31), 'park': (0.28, 0.40, 0.29),
        'ground': (0.43, 0.46, 0.40),
    }
    materials = {key: _new_material('Omaha_' + key, color,
                                   0.28 if key == 'water' else 0.8)
                 for key, color in colors.items()}
    # Muted warm construction cohorts; visual choices, not observed colors.
    decade_colors = ((0.62, 0.48, 0.34), (0.65, 0.52, 0.38), (0.68, 0.56, 0.43),
                     (0.71, 0.60, 0.48), (0.74, 0.65, 0.54), (0.77, 0.69, 0.60))
    chunk_size = int(cfg.get('mesh_chunk_vertices', 120000))
    observed = _MeshBatch(collections['baseline'], 'OSM_Buildings', materials['existing'], chunk_size)
    vertical_scale = float(cfg.get('vertical_exaggeration', 1.25))
    for building in sorted(baseline['buildings'], key=lambda record: record['id']):
        for polygon in building['polygons']:
            observed.add(*_building_geometry(polygon, building['height_m'] * vertical_scale))
    observed.flush()
    roads = _MeshBatch(collections['baseline'], 'OSM_Roads', materials['road'], chunk_size)
    for road in sorted(baseline['roads'], key=lambda record: record['id']):
        if road.get('tunnel', False):
            continue  # The flat map cannot represent terrain above a tunnel.
        elevation = 0.007 if road.get('bridge', False) else 0.00002
        roads.add(*_road_geometry(road['points'], road['width_km'], elevation))
    roads.flush()
    land_batches = {kind: _MeshBatch(collections['land'], 'OSM_' + kind,
                                   materials[kind], chunk_size) for kind in ('water', 'park')}
    for feature in sorted(baseline['land'], key=lambda record: record['id']):
        kind = feature['kind']
        if kind in land_batches:
            elevation = 0.0 if kind == 'water' else -0.000005
            for polygon in feature['polygons']:
                land_batches[kind].add(*_surface_geometry(polygon, elevation))
    for batch in land_batches.values():
        batch.flush()
    ground = {'outer': [(grid.min_x, grid.min_y), (grid.max_x, grid.min_y),
                        (grid.max_x, grid.max_y), (grid.min_x, grid.max_y)], 'holes': []}
    ground_batch = _MeshBatch(collections['stage'], 'Flat_Study_Ground', materials['ground'], chunk_size)
    ground_batch.add(*_surface_geometry(ground, -0.00002))
    ground_batch.flush()
    batches = {}
    base_year = int(cfg['base_year'])
    base_decade = (base_year // 10) * 10
    versions = _visual_lifetime_records(result['versions'])
    previous_lifetime = None
    for site in versions:
        start, end = int(site['start_year']), site.get('end_year')
        if end is not None:
            end = int(end)
        if end is not None and end <= start:
            raise ValueError(f"Invalid lifetime for site {site['id']}, version {site['version']}")
        lifetime = (start, end)
        if lifetime != previous_lifetime:
            for batch in batches.values():
                batch.flush()
            batches = {}
            previous_lifetime = lifetime
        for building in sorted(site['buildings'], key=lambda record: record['id']):
            year = int(building.get('construction_year', start))
            decade = (year // 10) * 10
            material_key = 'future_' + str(decade)
            if material_key not in materials:
                index = min(len(decade_colors) - 1, max(0, (decade - base_decade) // 10))
                materials[material_key] = _new_material('Future_' + str(decade) + 's', decade_colors[index])
            key = (start, end, material_key)
            if key not in batches:
                name = f'Scenario_{start}_{end or "onward"}_{material_key}'
                batches[key] = _MeshBatch(collections['future'], name, materials[material_key],
                                          chunk_size, (start, end, base_year))
            for volume in building.get('volumes') or [building]:
                polygon = {'outer': volume['poly'], 'holes': volume.get('holes', [])}
                batches[key].add(*_building_geometry(polygon, volume['height_m'] * vertical_scale,
                                                     volume.get('z0_m', 0.0) * vertical_scale))
        for surface in site.get('surfaces', []):
            material_key = 'future_road' if surface['kind'] == 'road' else 'parking'
            key = (start, end, material_key)
            if key not in batches:
                name = f'Scenario_{start}_{end or "onward"}_{material_key}'
                batches[key] = _MeshBatch(collections['future'], name, materials[material_key],
                                          chunk_size, (start, end, base_year))
            polygon = {'outer': surface['poly'], 'holes': surface.get('holes', [])}
            batches[key].add(*_surface_geometry(polygon, 0.00007))
    for batch in batches.values():
        batch.flush()
    # One frame per saved year. Drivers survive reopening and background rendering.
    scene.frame_start = 1
    scene.frame_end = int(cfg['end_year']) - base_year + 1
    scene.render.fps = int(cfg.get('render_fps', 2))
    for year in range(base_year, int(cfg['end_year']) + 1):
        scene.timeline_markers.new(str(year), frame=year - base_year + 1)
    resolution = cfg.get('render_resolution', [1600, 1000])
    scene.render.resolution_x, scene.render.resolution_y = map(int, resolution)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.film_transparent = False
    _configure_cycles(cfg, scene)
    _configure_camera_and_lighting(cfg, grid, scene, collections['stage'])
    scene.frame_set(1)
    if bpy.context.screen is not None:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                space = area.spaces.active
                space.clip_start = 0.001
                space.clip_end = max(grid.width, grid.height) * 20.0
                space.shading.color_type = 'MATERIAL'
                space.region_3d.view_perspective = 'CAMERA'
    _cleanup_previous_scenes(scene)
    print(f'[Omaha] Scene {scene.name}: frame 1 = {base_year}; '
          f'frame {scene.frame_end} = {cfg["end_year"]}. '
          f'Jump to year Y with scene.frame_set(Y - {base_year} + 1).')
    return scene


def finalize_blender_output(cfg, scene):
    """Optionally save a .blend, final still, and/or numbered PNG animation."""
    output_dir = Path(cfg.get('run_directory', cfg['output_dir'])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    animation_dir = output_dir / 'frames'
    scene.render.filepath = str(animation_dir / 'omaha_')
    if cfg.get('save_blend', False):
        path = output_dir / 'omaha_simulation.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        print('[Omaha] Saved visualization: ' + str(path))
    original_frame = scene.frame_current
    try:
        if cfg.get('render_final', False):
            scene.frame_set(scene.frame_end)
            scene.render.filepath = str(output_dir / f'omaha_{cfg["end_year"]}.png')
            bpy.ops.render.render(write_still=True, scene=scene.name)
        if cfg.get('render_animation', False):
            animation_dir.mkdir(parents=True, exist_ok=True)
            scene.render.filepath = str(animation_dir / 'omaha_')
            bpy.ops.render.render(animation=True, scene=scene.name)
    finally:
        scene.frame_set(original_frame)
        scene.render.filepath = str(animation_dir / 'omaha_')


