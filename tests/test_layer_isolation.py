"""Feature switches and reveal choices must not modify unrelated geometry."""

import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
from test_simulation import rectangle, synthetic_baseline

from metrosim26 import config, scene
from metrosim26.geography import Geography
from metrosim26.geometry import land, water
from metrosim26.geometry.packets import PacketBatch
from metrosim26.workers import Grid


def terrain_fixture(geo, cfg, grid):
    geo.grid = grid
    geo.spacing = 0.03
    geo.inner_radius = 0
    geo.tiles = {"synthetic": True}
    geo.segments = [((-1, 0), (1, 0))]
    geo.height = lambda x, y: 0.02 + 0.3 * x * x + 0.2 * y * y


def faces_by_layer(manifest, folder):
    """Compare actual face coordinates, ignoring filenames and packet boundaries."""
    result = defaultdict(list)
    for packet in manifest["packets"]:
        key = (packet["collection"], packet["material"], packet["properties"].get("feature_id"))
        with np.load(folder / packet["file"]) as mesh:
            for start, end in zip(mesh["offsets"][:-1], mesh["offsets"][1:]):
                vertices = mesh["vertices"][mesh["indices"][start:end]]
                result[key].append(tuple(sorted(tuple(v) for v in np.round(vertices, 9))))
    return {key: sorted(faces) for key, faces in result.items()}


class LayerIsolationTests(unittest.TestCase):
    def test_ground_water_and_reveal_switches_preserve_other_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config.circle_config(config.make_config(preview=True), 41.25, -95.99, 0.2, "KM")
            cfg.update(view_mode="MAP", include_terrain=True, include_ocean=True)
            grid = Grid(cfg["bounds"], cfg["cell_km"])
            baseline = synthetic_baseline(grid)
            baseline["buildings"] = [
                {
                    "id": "building/1",
                    "height_m": 20,
                    "polygons": [{"outer": rectangle(-0.02, 0.05, 0.02, 0.09), "holes": []}],
                }
            ]
            baseline["roads"] = [
                {
                    "id": "road/1",
                    "points": [(-0.15, 0.11), (0.15, 0.11)],
                    "width_km": 0.008,
                }
            ]
            baseline["land"] = [
                {
                    "id": kind,
                    "kind": kind,
                    "polygons": [{"outer": rectangle(-0.05, y, 0.05, y + 0.02), "holes": []}],
                }
                for kind, y in (("water", 0.02), ("park", 0.14))
            ]
            outputs = {}
            for name, changes in (
                ("all", {}),
                ("without_ground", {"include_ground": False}),
                ("without_water", {"include_water": False}),
                ("without_reveal", {"progressive_build": False}),
            ):
                folder = Path(directory) / name
                with patch.object(Geography, "__init__", terrain_fixture):
                    manifest = scene.export_scene(
                        dict(cfg, **changes),
                        grid,
                        baseline,
                        {"versions": [], "run_directory": directory},
                        folder,
                    )
                outputs[name] = faces_by_layer(manifest, folder)
            complete = outputs["all"]
            self.assertEqual(outputs["without_reveal"], complete)
            self.assertEqual(
                outputs["without_ground"],
                {key: value for key, value in complete.items() if key[0] != "ground"},
            )
            self.assertEqual(
                outputs["without_water"],
                {key: value for key, value in complete.items() if key[2] != "water"},
            )
            self.assertGreaterEqual(len(complete), 5)

    def test_surface_policy_is_explicit_and_independent_of_object_names(self):
        with tempfile.TemporaryDirectory() as directory:

            def make_batch(collection, name, material, limit, **options):
                return PacketBatch(
                    Path(directory),
                    [],
                    collection,
                    "Renamed surface",
                    material,
                    limit,
                    radius=1,
                    **options,
                )

            ground = land.ground_batch(make_batch, "ground", 12000)
            ocean = water.ocean_batch(make_batch, "water", 12000)
            for batch in (ground, ocean):
                for x in (0.01, 0.6):
                    batch.add([(x, 0, 0), (x + 0.01, 0, 0), (x, 0.01, 0)], [[0, 1, 2]])
                batch.flush()
            self.assertEqual(len(ground.packets), 2)
            self.assertEqual(len(ocean.packets), 1)


if __name__ == "__main__":
    unittest.main()
