"""Offline behavioral tests: python -m unittest discover -s tests -v.

Requires NumPy, not Blender or downloaded datasets. Synthetic geography is used
so these checks are repeatable and cannot modify a user's Blender scene.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import omaha_config as config
import omaha_data as data
import omaha_simulation as omaha
import omaha_blender as visual
import omaha_workers as workers


def rectangle(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def synthetic_baseline(grid):
    b = {key: np.zeros(grid.shape, dtype=np.float32) for key in (
        "water_fraction", "park_fraction", "developed_fraction", "road_access",
        "road_angle", "centrality", "housing_signal", "job_signal", "industrial_signal",
        "retail_signal", "office_signal", "institution_signal", "jobs", "residence_jobs",
    )}
    b["urban"] = np.zeros(grid.shape, dtype=bool)
    b["arch"] = np.zeros(grid.shape, dtype=np.uint8)
    b["urban"][2:4, 2:4] = True
    b["arch"][b["urban"]] = omaha.SUBURBAN
    b["developed_fraction"][b["urban"]] = 0.15
    b["jobs"][2:4, 2:4] = 100
    b["residence_jobs"][2:4, 2:4] = 100
    b["road_access"][:] = 0.65
    b["housing_signal"][:] = 0.7
    b["job_signal"][:] = 0.7
    b["office_signal"][:] = 0.7
    b["retail_signal"][:] = 0.5
    b["industrial_signal"][:] = 0.5
    yy, xx = np.indices(grid.shape)
    b["centrality"] = np.exp(-np.hypot(xx - 3, yy - 3) / 5).astype(np.float32)
    b.update(buildings=[], land=[], roads=[], pois=[], metadata={"synthetic": True})
    b["spatial_index"] = workers.prepare_spatial_index(b, grid)
    return b


class SpatialTests(unittest.TestCase):
    def setUp(self):
        self.grid = omaha.Grid((41.20, -96.05, 41.225, -96.015), 0.35)
        self.b = synthetic_baseline(self.grid)

    def test_grid_rejects_just_outside_west_and_partial_edges(self):
        g = self.grid
        self.assertIsNone(g.index(g.min_x - 0.0001, g.min_y + 0.1))
        self.assertIsNone(g.index(g.max_x, g.max_y - 0.1))
        x0, y0, x1, y1 = g.cell_bounds(g.nx - 1, g.ny - 1)
        self.assertAlmostEqual(x1, g.max_x)
        self.assertAlmostEqual(y1, g.max_y)
        self.assertEqual(g.index(*g.center(g.nx - 1, g.ny - 1)), (g.nx - 1, g.ny - 1))

    def test_intersection_and_spacing(self):
        a = rectangle(0, 0, 1, 1)
        self.assertTrue(workers.rings_intersect(a, rectangle(.5, .5, 1.5, 1.5)))
        self.assertTrue(workers.rings_intersect(a, rectangle(.2, .2, .3, .3)))
        self.assertFalse(workers.rings_intersect(a, rectangle(1.1, 0, 2, 1)))
        self.assertTrue(workers.rings_intersect(a, rectangle(1.01, 0, 2, 1), clearance=.02))

    def test_triangulation_preserves_holes_and_area(self):
        poly = {"outer": rectangle(0, 0, 1, 1), "holes": [rectangle(.25, .25, .75, .75)]}
        triangles = workers.mesh_triangles(poly)
        area = sum(workers.polygon_area(t) for t in triangles)
        self.assertAlmostEqual(area, .75, places=8)
        for triangle in triangles:
            x, y = np.mean(triangle, axis=0)
            self.assertFalse(.25 < x < .75 and .25 < y < .75)

    def test_water_hole_and_protected_building(self):
        g = self.grid
        gx, gy = 1, 1
        x0, y0, x1, y1 = g.cell_bounds(gx, gy)
        hole = rectangle(x0+.05, y0+.05, x1-.05, y1-.05)
        self.b["land"] = [{"id": "water:1", "kind": "water", "polygons": [
            {"outer": rectangle(x0, y0, x1, y1), "holes": [hole]}]}]
        self.b["spatial_index"] = workers.prepare_spatial_index(self.b, g)
        island = rectangle(x0+.10, y0+.10, x0+.15, y0+.15)
        self.assertTrue(workers.footprint_allowed(island, self.b, g, gx, gy))
        water = rectangle(x0+.01, y0+.01, x0+.04, y0+.04)
        self.assertFalse(workers.footprint_allowed(water, self.b, g, gx, gy))
        self.b["buildings"] = [{"id": "way:99", "polygons": [{"outer": island, "holes": []}],
                                 "height_m": 8., "arch": omaha.SUBURBAN}]
        self.b["spatial_index"] = workers.prepare_spatial_index(self.b, g)
        self.assertFalse(workers.footprint_allowed(island, self.b, g, gx, gy))

    def test_road_buffer_blocks_crossing(self):
        g = self.grid
        x, y = g.center(1, 1)
        self.b["roads"] = [{"id": "way:2", "points": [(x-.15, y), (x+.15, y)],
                            "width_km": .02, "class": "primary"}]
        self.b["spatial_index"] = workers.prepare_spatial_index(self.b, g)
        self.assertFalse(workers.footprint_allowed(rectangle(x-.02, y+.002, x+.02, y+.008),
                                               self.b, g, 1, 1))
        self.assertTrue(workers.footprint_allowed(rectangle(x-.02, y+.04, x+.02, y+.08),
                                              self.b, g, 1, 1))

    def test_baseline_roundtrip_without_pickle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.npz"
            data.save_baseline(path, self.b)
            loaded = data.load_baseline(path, self.grid)
            for key, value in self.b.items():
                if isinstance(value, np.ndarray):
                    np.testing.assert_array_equal(value, loaded[key])
            self.assertEqual(loaded["buildings"], self.b["buildings"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.grid = omaha.Grid((41.20, -96.05, 41.225, -96.015), 0.35)
        self.baseline = synthetic_baseline(self.grid)
        self.cfg = config.make_config(preview=True)
        self.cfg.update(bounds=list(self.grid.bounds), base_year=2026, end_year=2031,
                        initial_households=800., initial_jobs=1200.,
                        initial_housing_vacancy=0., initial_job_vacancy=0.,
                        annual_household_growth_rate=.06, annual_job_growth_rate=.07,
                        min_redevelopment_age=1)

    def test_all_archetypes_fit_and_do_not_overlap(self):
        for arch in range(omaha.SUBURBAN, omaha.HIGHRISE + 1):
            with self.subTest(arch=arch):
                self.baseline["road_angle"][:] = .31
                plan = workers.make_site_plan(self.cfg, self.grid, self.baseline, 4, 4, arch, 0, 2027)
                buildings = plan["buildings"]
                self.assertGreater(len(buildings), 0)
                for i, building in enumerate(buildings):
                    self.assertTrue(workers.footprint_allowed(building["poly"], self.baseline,
                                                          self.grid, 4, 4, .003))
                    for other in buildings[i+1:]:
                        self.assertFalse(workers.rings_intersect(building["poly"], other["poly"]))
                    for road in plan["roads"]:
                        self.assertFalse(workers.rings_intersect(building["poly"], road["poly"]))
                    for parking in plan["parking"].values():
                        self.assertFalse(workers.rings_intersect(building["poly"], parking["poly"]))

    def test_infill_retains_existing_buildings_and_adds_distinct_parcels(self):
        site = omaha.create_site(self.cfg, self.grid, self.baseline, 4, 4, omaha.SUBURBAN,
                                 2027, 2., 0., "greenfield")
        expanded = omaha.create_site(self.cfg, self.grid, self.baseline, 4, 4, omaha.SUBURBAN,
                                     2028, 1., 0., "greenfield", site)
        self.assertEqual(expanded["id"], site["id"])
        self.assertEqual(expanded["lifecycle_type"], "infill")
        previous = {item["id"] for item in site["buildings"]}
        current = {item["id"] for item in expanded["buildings"]}
        self.assertLess(previous, current)
        for item in site["buildings"]:
            self.assertIn(item, expanded["buildings"])
        self.assertEqual(len(current), len(expanded["buildings"]))

    def test_determinism_reload_and_demand_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = omaha.run_simulation(self.cfg, self.grid, self.baseline, Path(tmp)/"first")
            second = omaha.run_simulation(self.cfg, self.grid, synthetic_baseline(self.grid), Path(tmp)/"second")
            loaded = omaha.load_simulation(Path(tmp)/"first", self.cfg, self.grid)
            self.assertGreater(len(first["versions"]), 0)
            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(first["summary"], loaded["summary"])
            canonical = lambda value: json.dumps(value, sort_keys=True)
            self.assertEqual(canonical(first["versions"]), canonical(second["versions"]))
            self.assertEqual(canonical(first["versions"]), canonical(loaded["versions"]))
            for a, b in zip(first["states"], loaded["states"]):
                for key in ("urban", "arch", "new_development", "redevelopment", "infill"):
                    np.testing.assert_array_equal(a[key], b[key])
                self.assertEqual(canonical(a["sites"]), canonical(b["sites"]))
                m = a["metrics"]
                for name in ("housing", "job"):
                    self.assertAlmostEqual(m[f"cumulative_{name}_demand"],
                        m[f"cumulative_{name}_demand_served"] + m[f"unmet_{name}_demand"], places=6)
                    self.assertAlmostEqual(m[f"{name}_capacity_added"],
                        m[f"{name}_capacity_built_gross"] - m[f"{name}_capacity_demolished"], places=6)
                self.assertGreaterEqual(m["unused_housing_capacity"], -1e-7)
                self.assertGreaterEqual(m["unused_job_capacity"], -1e-7)
            # Checkpoints reference version geometry; they do not duplicate it per year.
            with np.load(Path(tmp)/"first"/"2028.npz", allow_pickle=False) as saved:
                payload = json.loads(str(saved["payload_json"].item()))
                self.assertIn("active_version_ids", payload)
                self.assertNotIn("sites", payload)

    def test_zero_growth_builds_nothing(self):
        self.cfg.update(annual_household_growth_rate=0., annual_job_growth_rate=0.)
        with tempfile.TemporaryDirectory() as tmp:
            result = omaha.run_simulation(self.cfg, self.grid, self.baseline, tmp)
        self.assertEqual(result["versions"], [])
        for state in result["states"]:
            np.testing.assert_array_equal(state["urban"], self.baseline["urban"])
            self.assertEqual(state["metrics"]["housing_capacity_added"], 0.)

    def test_unused_capacity_is_consumed_before_construction(self):
        self.cfg.update(initial_housing_vacancy=.5, initial_job_vacancy=.5,
                        annual_household_growth_rate=.01, annual_job_growth_rate=.01)
        with tempfile.TemporaryDirectory() as tmp:
            result = omaha.run_simulation(self.cfg, self.grid, self.baseline, tmp)
        self.assertEqual(result["versions"], [])
        self.assertGreater(result["summary"][-1]["cumulative_housing_demand_served"], 0.)
        self.assertEqual(result["summary"][-1]["unmet_housing_demand"], 0.)

    def test_outward_growth_is_adjacent_and_preserves_real_footprints(self):
        b, g = self.baseline, self.grid
        b["urban"][:] = False
        b["urban"][3, 3] = True
        b["arch"][:] = 0
        b["arch"][3, 3] = omaha.SUBURBAN
        b["centrality"][:] = .2
        bounds = g.cell_bounds(3, 3)
        b["buildings"] = [{"id": "way/real", "polygons": [{"outer": rectangle(*bounds), "holes": []}],
                           "height_m": 6., "arch": omaha.SUBURBAN}]
        b["spatial_index"] = workers.prepare_spatial_index(b, g)
        self.cfg.update(initial_households=500., initial_jobs=0., end_year=2027,
                        annual_household_growth_rate=.1, annual_job_growth_rate=0.)
        with tempfile.TemporaryDirectory() as tmp:
            result = omaha.run_simulation(self.cfg, g, b, tmp)
        self.assertGreater(result["summary"][-1]["new_greenfield_cells"], 0)
        for site in result["states"][-1]["sites"]:
            self.assertEqual(site["lifecycle_type"], "greenfield")
            self.assertLessEqual(abs(site["gx"]-3), 1)
            self.assertLessEqual(abs(site["gy"]-3), 1)
            for item in site["buildings"]:
                self.assertTrue(workers.footprint_allowed(item["poly"], b, g, site["gx"], site["gy"], .003))
        self.assertEqual(b["buildings"][0]["id"], "way/real")

    def test_excluded_land_carries_unmet_demand_without_building(self):
        self.baseline["water_fraction"][:] = 1.
        with tempfile.TemporaryDirectory() as tmp:
            result = omaha.run_simulation(self.cfg, self.grid, self.baseline, tmp)
        self.assertEqual(result["versions"], [])
        self.assertGreater(result["summary"][-1]["unmet_housing_demand"], 0.)
        self.assertGreater(result["summary"][-1]["unmet_job_demand"], 0.)

    def test_redevelopment_retires_only_generated_buildings(self):
        grid = omaha.Grid((41.20, -96.05, 41.203, -96.047), .35)
        b = synthetic_baseline(grid)
        b["urban"][:] = True
        b["arch"][:] = omaha.SUBURBAN
        b["centrality"][:] = .2
        cfg = dict(self.cfg, bounds=list(grid.bounds), end_year=2034,
                   initial_households=1000., initial_jobs=0., annual_household_growth_rate=.1,
                   annual_job_growth_rate=0., min_redevelopment_age=1)
        with tempfile.TemporaryDirectory() as tmp:
            result = omaha.run_simulation(cfg, grid, b, tmp)
        self.assertTrue(any(row["redevelopment_sites"] for row in result["summary"]))
        for state in result["states"]:
            active = [site for site in result["versions"] if site["start_year"] <= state["year"]
                      and (site["end_year"] is None or state["year"] < site["end_year"])]
            self.assertEqual(len({site["id"] for site in active}), len(active))
            self.assertEqual(active, state["sites"])
            active_building_ids = {item["id"] for site in active for item in site["buildings"]}
            self.assertFalse(active_building_ids.intersection(state["demolished"]))

    def test_highrise_requires_signal_and_density_thresholds(self):
        self.baseline["centrality"][:] = .2
        neighbors = np.ones(self.grid.shape, dtype=np.float32)
        self.assertFalse(omaha.highrise_allowed(self.cfg, self.baseline, 3, 3, neighbors))
        self.assertNotEqual(omaha.choose_archetype(self.cfg, self.baseline, 3, 3, neighbors,
                                                  500., 500.), omaha.HIGHRISE)


class OSMTests(unittest.TestCase):
    class UnitGrid:
        min_x = min_y = south = west = 0.
        max_x = max_y = north = east = 1.
        nx = ny = 1
        shape = (1, 1)
        cell_km = 1.
        def project(self, lon, lat):
            return lon, lat
        def index(self, x, y):
            return (0, 0) if 0 <= x < self.max_x and 0 <= y < self.max_y else None
        def cell_bounds(self, gx, gy):
            return gx, gy, min(gx+1, self.max_x), min(gy+1, self.max_y)

    def elements(self):
        outer = rectangle(0, 0, 1, 1)
        hole = rectangle(.25, .25, .75, .75)
        nodes = [{"type": "node", "id": i, "lon": x, "lat": y}
                 for i, (x, y) in enumerate(outer + hole, 1)]
        return nodes + [
            {"type": "way", "id": 10, "nodes": [1, 2, 3], "tags": {"natural": "water"}},
            {"type": "way", "id": 11, "nodes": [1, 4, 3]},
            {"type": "way", "id": 12, "nodes": [5, 6, 7, 8, 5], "tags": {"natural": "water"}},
            {"type": "relation", "id": 20, "tags": {"type": "multipolygon", "natural": "water"},
             "members": [{"type": "way", "ref": 12, "role": "inner"},
                         {"type": "way", "ref": 11, "role": "outer"},
                         {"type": "way", "ref": 10, "role": "outer"}]},
        ]

    def test_split_reversed_relation_dedup_and_fractional_hole(self):
        elements, grid = self.elements(), self.UnitGrid()
        parsed = data.parse_osm(elements, grid)
        self.assertEqual(len(parsed["land"]), 1)
        self.assertEqual(parsed, data.parse_osm(list(reversed(elements)), grid))
        zeros = {key: np.zeros(grid.shape, dtype=np.float32) for key in (
            "jobs", "residence_jobs", "industrial_jobs", "retail_jobs", "office_jobs", "institution_jobs")}
        baseline = data.derive_baseline({"subcell_samples": 8}, grid, parsed, zeros)
        self.assertEqual(float(baseline["water_fraction"][0, 0]), .75)
        self.assertEqual(float(baseline["protected_fraction"][0, 0]), .75)
        elements.append({"type": "relation", "id": 21,
                         "tags": {"type": "multipolygon", "natural": "water"},
                         "members": [{"type": "relation", "ref": 20, "role": "outer"}]})
        self.assertEqual(len(data.parse_osm(elements, grid)["land"]), 1)

    def test_clipped_edge_fraction(self):
        grid = self.UnitGrid()
        grid.max_x, grid.nx, grid.shape = 1.25, 2, (1, 2)
        mask = np.zeros((8, 16), dtype=bool)
        data._rasterize_polygon(mask, {"outer": rectangle(1, 0, 1.125, 1), "holes": []}, grid, 8)
        np.testing.assert_array_equal(mask.reshape(1, 8, 2, 8).mean(axis=(1, 3)), [[0., .5]])

    def test_building_relation_keeps_one_real_outline(self):
        grid = self.UnitGrid()
        elements = [{"type": "node", "id": i, "lon": x, "lat": y}
                    for i, (x, y) in enumerate(rectangle(.1, .1, .9, .9), 1)]
        elements.extend([
            {"type": "way", "id": 10, "nodes": [1, 2, 3, 4, 1],
             "tags": {"building": "office", "height": "25"}},
            {"type": "relation", "id": 20, "tags": {"type": "building"},
             "members": [{"type": "way", "ref": 10, "role": "outline"}]},
        ])
        parsed = data.parse_osm(elements, grid)
        self.assertEqual(len(parsed["buildings"]), 1)
        self.assertEqual(parsed["buildings"][0]["id"], "relation/20")
        self.assertEqual(parsed["buildings"][0]["height_m"], 25.)
        self.assertAlmostEqual(workers.polygon_area(parsed["buildings"][0]["polygons"][0]["outer"]), .64)

    def test_site_without_perimeter_does_not_invent_land(self):
        grid = self.UnitGrid()
        elements = [{"type": "node", "id": 1, "lon": .2, "lat": .2},
                    {"type": "node", "id": 2, "lon": .8, "lat": .8},
                    {"type": "relation", "id": 20,
                     "tags": {"type": "site", "amenity": "university"},
                     "members": [{"type": "node", "ref": 1, "role": ""},
                                 {"type": "node", "ref": 2, "role": ""}]}]
        parsed = data.parse_osm(elements, grid)
        self.assertEqual(parsed["land"], [])
        self.assertEqual(len(parsed["pois"]), 1)
        self.assertEqual(parsed["diagnostics"]["sites_without_area_boundary"], 1)

    def test_query_identity_changes_with_bounds_and_snapshot(self):
        first = data._osm_query([0, 0, 1, 1])
        second = data._osm_query([0, 0, 1, 2])
        historical = data._osm_query([0, 0, 1, 1], "2026-01-01T00:00:00Z")
        self.assertNotEqual(data._data_signature(first), data._data_signature(second))
        self.assertNotEqual(data._data_signature(first), data._data_signature(historical))
        self.assertIn('[date:"2026-01-01T00:00:00Z"]', historical)


class PlaybackGeometryTests(unittest.TestCase):
    def test_infill_does_not_duplicate_persistent_visual_geometry(self):
        a = {"id": "a", "construction_year": 2027}
        b = {"id": "b", "construction_year": 2028}
        records = visual._visual_lifetime_records([
            {"id": "site", "version": 0, "start_year": 2027, "end_year": 2028,
             "buildings": [a], "surfaces": []},
            {"id": "site", "version": 1, "start_year": 2028, "end_year": 2030,
             "buildings": [a, b], "surfaces": []},
        ])
        self.assertEqual(len(records), 2)
        self.assertEqual([(r["id"], r["start_year"], r["end_year"]) for r in records],
                         [("a", 2027, 2030), ("b", 2028, 2030)])

    def test_holed_nonconvex_building_roof_and_floor(self):
        polygon = {"outer": [(0, 0), (5, 0), (5, 5), (3, 5), (3, 4), (0, 4)],
                   "holes": [rectangle(1, 1, 2, 2)]}
        vertices, faces = visual._building_geometry(polygon, 12, 3)
        for z in (.01508, .00308):
            faces_at_z = [[vertices[i] for i in face] for face in faces
                          if len(face) == 3 and all(abs(vertices[i][2]-z) < 1e-12 for i in face)]
            area = sum(workers.polygon_area([(v[0], v[1]) for v in f]) for f in faces_at_z)
            self.assertAlmostEqual(area, 21.)

    def test_random_access_visibility_is_exclusive(self):
        from types import SimpleNamespace
        class DriverObject(dict):
            def __init__(self):
                super().__init__()
                self.drivers = {}
            def driver_add(self, name):
                self.drivers[name] = SimpleNamespace(driver=SimpleNamespace())
                return self.drivers[name]
        old, replacement = DriverObject(), DriverObject()
        visual._set_object_lifetime(old, 2027, 2030, 2026)
        visual._set_object_lifetime(replacement, 2030, None, 2026)
        for year in (2035, 2026, 2030, 2029, 2027, 2034, 2028, 2026):
            visible = []
            for obj in (old, replacement):
                flags = [bool(eval(d.driver.expression, {"__builtins__": {}}, {"frame": year-2026+1}))
                         for d in obj.drivers.values()]
                self.assertEqual(flags[0], flags[1])
                visible.append(not flags[0])
            self.assertEqual(visible, [2027 <= year < 2030, year >= 2030])


class PipelineTests(unittest.TestCase):
    def test_complete_replay_skips_data_loading_and_detects_corruption(self):
        from unittest.mock import patch
        cfg = config.make_config(preview=True)
        cfg.update(bounds=[41.20, -96.05, 41.225, -96.015], end_year=2028,
                   initial_households=800., initial_jobs=1200., initial_housing_vacancy=0.,
                   initial_job_vacancy=0.)
        with tempfile.TemporaryDirectory() as tmp:
            cfg.update(output_dir=tmp, cache_dir=tmp)
            with patch.object(omaha, "build_baseline", side_effect=lambda c, g: synthetic_baseline(g)):
                _, baseline, first = omaha.prepare_simulation(cfg)
            cfg["run_mode"] = "REPLAY"
            with patch.object(omaha, "build_baseline", side_effect=AssertionError("Replay downloaded data")):
                _, restored, loaded = omaha.prepare_simulation(cfg)
            self.assertEqual(first["summary"], loaded["summary"])
            np.testing.assert_array_equal(baseline["urban"], restored["urban"])
            self.assertTrue(loaded["metadata"]["complete"])
            path = Path(first["run_directory"]) / "simulation" / "2027.npz"
            path.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                omaha.prepare_simulation(cfg)


if __name__ == "__main__":
    unittest.main()
