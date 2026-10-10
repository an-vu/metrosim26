"""Run with Blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py.

Small, offline native checks for registration, expansion and temporary reveal cleanup.
No user scene, download, preferences file, or render output is saved.
"""

import math
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_simulation import synthetic_baseline

from metrosim26 import blender_ui as ui
from metrosim26 import config, geography, scene
from metrosim26.blender import cameras, lighting, reveal, scene_import
from metrosim26.workers import Grid

ROOT = Path(__file__).resolve().parents[1]


def check_launcher():
    for _ in range(2):
        runpy.run_path(str(ROOT / "metrosim.py"), run_name="__main__")
        assert bpy.context.scene.metrosim26_setup.palette_preset == "NATURAL"
        assert bpy.context.scene.metrosim26_setup.cache_dir
        assert bpy.context.scene.metrosim26_setup.output_dir
        assert len(ui.REGISTERED_CLASSES) == 11
        assert bpy.ops.metrosim26.preview_area() == {"FINISHED"}
        assert any(o.get("metrosim26_area_preview") for o in bpy.context.scene.objects)
        bpy.context.scene.metrosim26_setup.radius = 3.5
        runpy.run_path(str(ROOT / "metrosim.py"), run_name="__main__")
        assert bpy.context.scene.metrosim26_setup.radius == 3.5
        bpy.ops.wm.read_homefile(use_empty=False)
    runpy.run_path(str(ROOT / "metrosim.py"), run_name="__main__")
    handlers = [h for h in bpy.app.handlers.load_pre if h.__module__ == ui.__name__]
    assert len(handlers) == 1


def fake_geography(geo, cfg, grid):
    geo.grid = grid
    geo.spacing = cfg["terrain_spacing_km"]
    geo.inner_radius = cfg.get("expand_inner_radius_km", 0)
    geo.tiles = {"synthetic": True}
    geo.segments = [((-2, 0), (2, 0))]
    geo.height = lambda x, y: 0.01 + 0.04 * x * x + 0.02 * y * y


def check_expansion(directory):
    cfg = config.circle_config(config.make_config(preview=True), 41.25, -95.99, 0.2, "KM")
    cfg.update(view_mode="MAP", cache_dir=directory, output_dir=directory)
    ui.open_setup(cfg)
    previous = None
    previous_ocean = previous_mesh = previous_count = None
    for radius in (0.4, 0.8):
        settings = bpy.context.scene.metrosim26_setup
        settings.radius, settings.units = radius, "KM"
        settings.include_ocean = True
        settings.include_terrain = True
        current = ui.setup_config(settings)
        grid = Grid(current["bounds"], current["cell_km"])
        baseline = synthetic_baseline(grid)
        baseline["roads"] = [
            {"id": "way/road", "points": [(-0.35, 0), (0.35, 0)], "width_km": 0.008}
        ]
        baseline["land"] = []
        folder = Path(directory) / str(radius)
        with patch.object(geography.Geography, "__init__", fake_geography):
            scene.export_scene(
                current, grid, baseline, {"versions": [], "run_directory": directory}, folder
            )
        importer = scene_import.SceneImport(folder)
        for _ in importer.steps:
            pass
        result = importer.scene
        assert importer.finished
        assert len([o for o in result.objects if o.name.startswith("Study_Ground")]) == 1
        assert len([o for o in result.objects if o.get("feature_id") == "way/road"]) == 1
        oceans = [o for o in result.objects if o.name.startswith("Ocean")]
        assert len(oceans) == 1, "Ocean must not retain circular batch objects"
        ocean = oceans[0]
        assert all(abs(v.co.z) < 1e-8 for v in ocean.data.vertices)
        expected_area = 0.5 * 48 * radius**2 * math.sin(2 * math.pi / 96)
        actual_area = sum(p.area for p in ocean.data.polygons)
        assert math.isclose(actual_area, expected_area, rel_tol=1e-4), (actual_area, expected_area)
        if previous:
            assert previous_ocean.name in previous.objects
            assert previous_ocean.data.as_pointer() == previous_mesh
            assert len(previous_ocean.data.vertices) == previous_count
            assert current["expand_inner_radius_km"] == 0.4
            assert any(o.name.startswith("Study_Ground") for o in previous.objects)
        previous = result
        previous_ocean = ocean
        previous_mesh = ocean.data.as_pointer()
        previous_count = len(ocean.data.vertices)
    return importer


def check_reveal(importer):
    result = importer.scene
    original_objects = set(bpy.data.objects.keys())
    original_materials = set(bpy.data.materials.keys())
    geometry = {
        o.name: (o.data.as_pointer(), len(o.data.vertices), o.matrix_world.copy())
        for o in result.objects
        if o.type == "MESH"
    }
    for cancel in (True, False):
        clock = [0.0]

        def advance():
            clock[0] += 0.05
            return clock[0]

        with patch.object(reveal.time, "monotonic", side_effect=advance):
            steps = reveal.reveal_map(importer, result, duration=0.1)
            next(steps)
            assert importer.revealing
            if cancel:
                steps.close()
            else:
                for _ in steps:
                    pass
        assert not importer.revealing
        assert set(bpy.data.objects.keys()) == original_objects
        assert set(bpy.data.materials.keys()) == original_materials
        for name, (mesh, count, matrix) in geometry.items():
            obj = result.objects[name]
            assert (obj.data.as_pointer(), len(obj.data.vertices), obj.matrix_world) == (
                mesh,
                count,
                matrix,
            )
            assert not obj.hide_get(view_layer=result.view_layers[0])


def check_camera_light_isolation(importer):
    result = importer.scene
    grid = SimpleNamespace(
        min_x=-0.8, max_x=0.8, min_y=-0.8, max_y=0.8, width=1.6, height=1.6, min_z=0, max_z=0.3
    )
    collection = bpy.data.collections.new("Isolation check")
    result.collection.children.link(collection)
    owned = []

    def own(database, item):
        owned.append((database, item))
        return item

    camera = result.camera
    world = result.world
    pose = camera.matrix_world.copy()
    scale = camera.data.ortho_scale
    samples = result.cycles.preview_samples
    denoise = result.cycles.use_preview_denoising
    try:
        lighting.configure_lighting(grid, result, collection, own)
        assert result.camera == camera and camera.matrix_world == pose
        assert camera.data.ortho_scale == scale
        assert result.cycles.preview_samples == samples
        assert result.cycles.use_preview_denoising == denoise
        light_world = result.world
        result.view_layers[0].update()
        lights = {
            o.name: (o.data.energy, o.matrix_world.copy())
            for o in collection.objects
            if o.type == "LIGHT"
        }
        cameras.configure_cameras(importer.cfg, grid, result, collection, own)
        assert result.world == light_world
        assert lights == {
            o.name: (o.data.energy, o.matrix_world.copy())
            for o in collection.objects
            if o.type == "LIGHT"
        }
    finally:
        result.camera = camera
        result.world = world
        for database, item in reversed(owned):
            database.remove(item, do_unlink=True)
        bpy.data.collections.remove(collection)


if __name__ == "__main__":
    check_launcher()
    with tempfile.TemporaryDirectory(prefix="metrosim26-smoke-") as directory:
        importer = check_expansion(directory)
        check_reveal(importer)
        check_camera_light_isolation(importer)
    print("MetroSim 26 Blender smoke checks passed.")
