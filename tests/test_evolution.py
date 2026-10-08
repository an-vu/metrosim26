"""Optional narrative, infrastructure, history, and restart-contract invariants."""

import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import numpy as np
from test_performance import performance_case
from test_simulation import config, rectangle, simulation, synthetic_baseline, visual, workers

from metrosim26.history import build_history, read_continuation, year_to_frame
from metrosim26.infrastructure import Infrastructure, clear_corridor, generated_index
from metrosim26.projects import Projects, capacity
from metrosim26.scene import _visual_lifetime_records, export_scene


class ProjectTests(unittest.TestCase):
    def test_phasing_capacities_names_and_bounded_feedback(self):
        cfg, grid, baseline = performance_case()
        cfg.update(enable_projects=True, project_min_buildings=3, project_min_housing=1)
        records = []
        for _ in range(2):
            events = []
            manager = Projects(cfg, grid, events)
            buildings = [
                dict(
                    id=f"house_{i}",
                    housing=1.0,
                    jobs=0.0,
                    construction_year=2027,
                    poly=rectangle(i * 0.02, 0, i * 0.02 + 0.01, 0.01),
                    height_m=6,
                )
                for i in range(6)
            ]
            site = dict(
                id="site",
                version=0,
                layout_epoch=0,
                arch=simulation.SUBURBAN,
                gx=2,
                gy=2,
                start_year=2027,
                end_year=None,
                buildings=buildings,
                surfaces=[],
                plan_building_count=6,
            )
            staged = manager.stage(site, None, "greenfield", 2027, baseline, 100, 0)
            self.assertEqual(staged["buildings"], [])
            self.assertEqual(capacity(staged["buildings"]), (0, 0))
            active = {"site": staged}
            versions = [staged]
            for year in (2028, 2029, 2030):
                phases = manager.due(year, active)
                self.assertEqual(len(phases), 1)
                new, previous = phases[0]
                previous["end_year"] = year
                active["site"] = new
                versions.append(new)
                manager.refresh(active)
                self.assertEqual(capacity(new["buildings"])[0], 2 * (year - 2027))
                self.assertTrue(all(b["construction_year"] <= year for b in new["buildings"]))
            project = next(iter(manager.records.values()))
            self.assertEqual(project["status"], "complete")
            self.assertTrue(project["fictional"])
            self.assertEqual(project["completion_year"], 2030)
            lifetimes = _visual_lifetime_records(versions)
            self.assertEqual(
                [s["start_year"] for s in lifetimes], [2028, 2028, 2029, 2029, 2030, 2030]
            )
            for key in ("housing_signal", "job_signal"):
                updated = manager.feedback(baseline)[key]
                self.assertTrue(np.all(updated >= baseline[key]))
                self.assertLessEqual(
                    float(np.max(updated - baseline[key])), cfg["project_feedback_cap"] + 1e-6
                )
            records.append((manager.snapshot(), events))
        self.assertEqual(records[0], records[1])

    def test_project_simulation_parallel_replay_and_ordinary_growth(self):
        cfg, grid, baseline = performance_case()
        cfg.update(
            enable_projects=True,
            enable_parallel_compute=False,
            end_year=2031,
            project_min_buildings=1,
            project_min_housing=1,
            project_min_jobs=1,
            project_max_starts_per_year=1,
            parallel_min_tasks=1,
            parallel_min_batch_seconds=0,
        )
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            one, two = Path(tmp) / "serial", Path(tmp) / "parallel"
            first = simulation.run_simulation(cfg, grid, deepcopy(baseline), one)
            cfg.update(enable_parallel_compute=True, cpu_workers=2, validate_parallel_results=True)
            second = simulation.run_simulation(cfg, grid, deepcopy(baseline), two)
            self.assertTrue(first["projects"])
            self.assertTrue(any("project_id" not in s for s in first["versions"]))
            self.assertEqual(first["projects"], second["projects"])
            self.assertEqual(first["events"], second["events"])
            for path in one.iterdir():
                self.assertEqual(path.read_bytes(), (two / path.name).read_bytes(), path.name)
            loaded = simulation.load_simulation(two, cfg, grid)
            self.assertEqual(loaded["projects"], first["projects"])
            self.assertEqual(loaded["events"], first["events"])
            for state in first["states"]:
                metrics = state["metrics"]
                housing = sum(simulation.site_capacity(s)[0] for s in state["sites"])
                self.assertAlmostEqual(
                    metrics["total_housing_capacity"],
                    first["baseline_capacity"]["housing_capacity"] + housing,
                )
                self.assertGreaterEqual(metrics["unused_housing_capacity"], -1e-8)

    def test_disabled_flags_preserve_old_identity_and_bytes(self):
        cfg, grid, baseline = performance_case()
        cfg.update(enable_parallel_compute=False, end_year=2028)
        old = {
            k: v
            for k, v in cfg.items()
            if not (
                k.startswith(("project_", "infrastructure_", "historical_"))
                or k
                in (
                    "enable_projects",
                    "enable_infrastructure",
                    "timeline_start_year",
                    "save_continuation",
                )
            )
        }
        self.assertEqual(config.run_directory(cfg), config.run_directory(old))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            a, b = Path(tmp) / "new", Path(tmp) / "old"
            simulation.run_simulation(cfg, grid, deepcopy(baseline), a)
            simulation.run_simulation(old, grid, deepcopy(baseline), b)
            self.assertEqual(
                sorted(p.name for p in a.iterdir()), sorted(p.name for p in b.iterdir())
            )
            for path in a.iterdir():
                self.assertEqual(path.read_bytes(), (b / path.name).read_bytes())


class InfrastructureTests(unittest.TestCase):
    def test_highway_crossing_is_not_an_implicit_interchange(self):
        cfg, grid, baseline = self.fixture()
        x, _ = grid.center(3, 3)
        baseline["roads"].append(
            dict(
                id="highway",
                points=[(x, grid.min_y), (x, grid.max_y)],
                width_km=0.04,
                **{"class": "motorway"},
            )
        )
        workers.prepare_spatial_index(baseline, grid)
        infra = Infrastructure(cfg, grid, baseline, [])
        self.assertIsNone(infra.route((6, 5), grid.center(1, 5), [infra.static_index]))

    def test_named_project_can_trigger_access_without_unmet_capacity(self):
        cfg, grid, baseline = self.fixture()
        events = []
        projects = Projects(cfg, grid, events)
        projects.records["test_project"] = dict(
            project_id="test_project", location=[3, 3], status="construction"
        )
        infra = Infrastructure(cfg, grid, baseline, events)
        for year in (2027, 2028):
            infra.propose(
                year,
                baseline["urban"],
                simulation.neighbor_fraction(baseline["urban"]),
                0,
                0,
                {},
                projects,
            )
        self.assertTrue(infra.records)
        self.assertEqual(infra.records[0]["type"], "major_access")
        self.assertEqual(infra.records[0]["related_project_ids"], ["test_project"])

    def fixture(self):
        cfg = config.make_config(preview=True)
        cfg.update(
            enable_infrastructure=True,
            infrastructure_pressure_years=2,
            infrastructure_max_projects=1,
        )
        grid = simulation.Grid((41.20, -96.05, 41.225, -96.015), 0.35)
        cfg["bounds"] = list(grid.bounds)
        baseline = synthetic_baseline(grid)
        baseline["road_access"][:] = 0.12
        baseline["roads"] = [
            dict(
                id="road",
                points=[grid.center(0, 5), grid.center(1, 5)],
                width_km=0.012,
                **{"class": "residential"},
            )
        ]
        workers.prepare_spatial_index(baseline, grid)
        return cfg, grid, baseline

    def test_deterministic_connected_safe_corridors_and_opening_effects(self):
        snapshots = []
        for _ in range(2):
            cfg, grid, baseline = self.fixture()
            events = []
            projects = Projects(cfg, grid, events)
            infra = Infrastructure(cfg, grid, baseline, events)
            neighbors = simulation.neighbor_fraction(baseline["urban"])
            infra.propose(2027, baseline["urban"], neighbors, 100, 100, {}, projects)
            self.assertFalse(infra.records)
            infra.propose(2028, baseline["urban"], neighbors, 100, 100, {}, projects)
            self.assertEqual(len(infra.records), 1)
            record = infra.records[0]
            self.assertEqual(record["connected_road_ids"], ["road"])
            self.assertIn(
                tuple(record["geometry"][0]), [tuple(p) for p in baseline["roads"][0]["points"]]
            )
            self.assertGreaterEqual(len(record["affected_cells"]), 2)
            for segment in record["segments"]:
                self.assertTrue(clear_corridor(segment["poly"], grid, [infra.static_index]))
            first = min(s["opening_year"] for s in record["segments"])
            np.testing.assert_array_equal(infra.accessibility(first - 1), baseline["road_access"])
            self.assertGreater(
                float(infra.accessibility(first).max()), float(baseline["road_access"].max())
            )
            for year in range(2029, record["completion_year"] + 1):
                infra.begin_year(year)
            self.assertEqual(record["status"], "complete")
            self.assertTrue(
                np.all(
                    infra.accessibility(2100) - baseline["road_access"]
                    <= cfg["infrastructure_access_gain"] + 1e-6
                )
            )
            snapshots.append((infra.snapshot(), events))
        self.assertEqual(*snapshots)

    def test_blocked_wall_and_pending_buildings_are_not_crossed(self):
        cfg, grid, baseline = self.fixture()
        x, _ = grid.center(3, 3)
        wall = {"outer": rectangle(x - 0.03, grid.min_y, x + 0.03, grid.max_y), "holes": []}
        baseline["land"] = [dict(id="water", kind="water", polygons=[wall])]
        workers.prepare_spatial_index(baseline, grid)
        infra = Infrastructure(cfg, grid, baseline, [])
        self.assertIsNone(infra.route((6, 5), grid.center(1, 5), [infra.static_index]))
        baseline["land"] = []
        workers.prepare_spatial_index(baseline, grid)
        infra = Infrastructure(cfg, grid, baseline, [])
        pending = {
            "site": dict(buildings=[dict(id="future-house", poly=wall["outer"])], surfaces=[])
        }
        self.assertIsNone(
            infra.route(
                (6, 5), grid.center(1, 5), [infra.static_index, generated_index(grid, {}, pending)]
            )
        )

    def test_layer_integration_reserves_corridors_and_exports_lifetimes(self):
        cfg, grid, baseline = self.fixture()
        cfg.update(
            enable_parallel_compute=False,
            end_year=2033,
            initial_households=10000,
            initial_jobs=10000,
            initial_housing_vacancy=0,
            initial_job_vacancy=0,
            annual_household_growth_rate=0.08,
            annual_job_growth_rate=0.08,
            max_sites_per_year=1,
        )
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            result = simulation.run_simulation(cfg, grid, baseline, Path(tmp) / "simulation")
            self.assertTrue(result["infrastructure"])
            record = result["infrastructure"][0]
            for site in result["versions"]:
                index = generated_index(grid, {site["id"]: site}, {})
                for segment in record["segments"]:
                    self.assertTrue(clear_corridor(segment["poly"], grid, [index]))
            result["run_directory"] = tmp
            manifest = export_scene(cfg, grid, baseline, result, Path(tmp) / "scene")
            packets = [p for p in manifest["packets"] if p["collection"] == "infrastructure"]
            self.assertTrue(packets)
            self.assertEqual(
                [p["lifetime"][0] for p in packets], [s["opening_year"] for s in record["segments"]]
            )
            self.assertTrue(all(p["properties"]["fictional"] for p in packets))


class HistoricalTests(unittest.TestCase):
    def test_pending_projects_and_infrastructure_are_in_continuation_contract(self):
        cfg, grid, baseline = performance_case()
        cfg.update(
            enable_projects=True,
            enable_infrastructure=True,
            enable_parallel_compute=False,
            project_min_buildings=1,
            project_min_housing=1,
            project_min_jobs=1,
            historical_mode="EVIDENCE",
            timeline_start_year=2025,
            end_year=2028,
        )
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg["output_dir"] = tmp
            with patch.object(simulation, "build_baseline", return_value=baseline):
                _, _, result = simulation.prepare_simulation(cfg)
            directory = Path(result["run_directory"]) / "simulation"
            contract = read_continuation(directory)
            self.assertTrue(contract["projects"]["pending_sites"])
            self.assertIn("pressure_years", contract["infrastructure"])
            self.assertEqual(contract["projects"]["projects"], result["projects"])
            pending = contract["projects"]["pending_sites"]
            self.assertTrue(
                any(b["construction_year"] > 2028 for s in pending.values() for b in s["buildings"])
            )
            cfg["run_mode"] = "REPLAY"
            _, _, loaded = simulation.prepare_simulation(cfg)
            self.assertEqual(loaded["projects"], result["projects"])
            self.assertEqual(loaded["history"], result["history"])
            cfg.update(run_mode="SIMULATE", end_year=2030)
            with patch.object(simulation, "build_baseline", return_value=deepcopy(baseline)):
                _, _, long = simulation.prepare_simulation(cfg)
            later = Path(long["run_directory"]) / "simulation"
            for year in (2026, 2027, 2028):
                self.assertEqual(
                    (directory / f"{year}.npz").read_bytes(), (later / f"{year}.npz").read_bytes()
                )

    def test_sparse_evidence_timeline_replay_and_restart_contract(self):
        cfg, grid, baseline = performance_case()
        cfg.update(
            enable_parallel_compute=False,
            end_year=2028,
            historical_mode="EVIDENCE",
            timeline_start_year=2006,
            save_continuation=True,
        )
        first = baseline["buildings"][0]
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg["output_dir"] = tmp
            evidence = Path(tmp) / "evidence.json"
            evidence.write_text(
                json.dumps(
                    dict(
                        schema_version=1,
                        features=[
                            dict(
                                kind="building",
                                id=first["id"],
                                start_year=2010,
                                source="synthetic test evidence",
                                provenance="historically_reconstructed",
                            )
                        ],
                        events=[],
                    )
                )
            )
            cfg["historical_evidence_file"] = str(evidence)
            with patch.object(simulation, "build_baseline", return_value=baseline):
                _, _, result = simulation.prepare_simulation(cfg)
            self.assertEqual(result["states"][0]["year"], 2006)
            self.assertEqual(result["states"][0]["provenance"], "historically_reconstructed")
            self.assertFalse(result["states"][0]["building_visibility"])
            self.assertEqual(result["states"][4]["building_visibility"], [first["id"]])
            self.assertFalse(result["states"][4]["road_visibility"])
            self.assertGreater(result["states"][0]["unknown_historical_features"], 0)
            self.assertEqual(year_to_frame(2026, cfg), 21)
            self.assertEqual(year_to_frame(2076, cfg), 71)
            directory = Path(result["run_directory"]) / "simulation"
            contract = read_continuation(directory)
            self.assertEqual(contract["next_year"], 2029)
            self.assertFalse(contract["extension_execution_supported"])
            self.assertNotIn("end_year", contract["simulation_parameters"])
            self.assertEqual(
                contract["ledger"]["households"], result["summary"][-1]["scenario_households"]
            )
            cfg["run_mode"] = "REPLAY"
            with patch.object(
                simulation, "build_baseline", side_effect=AssertionError("Rebuilt baseline")
            ):
                _, _, loaded = simulation.prepare_simulation(cfg)
            self.assertEqual(loaded["history"], result["history"])
            manifest = export_scene(cfg, grid, baseline, loaded, Path(tmp) / "mesh")
            historical = [
                p
                for p in manifest["packets"]
                if p["collection"] == "baseline" and p["lifetime"][0] == 2010
            ]
            self.assertTrue(historical)
            self.assertEqual(historical[0]["lifetime"], (2010, None, 2006))
            (directory / "2028.npz").write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "changed"):
                read_continuation(directory)

    def test_no_evidence_means_unknown_not_reverse_growth(self):
        cfg, grid, baseline = performance_case()
        cfg.update(historical_mode="EVIDENCE", timeline_start_year=2024)
        with tempfile.TemporaryDirectory() as tmp:
            history, states = build_history(cfg, grid, baseline, tmp)
            self.assertTrue(
                all(not s["building_visibility"] and not s["road_visibility"] for s in states)
            )
            self.assertTrue(all(np.all(s["unknown_coverage"]) for s in states))
            self.assertTrue(
                all(v["start_year"] == 2026 for v in history["lifetimes"]["building"].values())
            )

    def test_lifetime_driver_uses_timeline_start(self):
        from unittest.mock import MagicMock

        obj = MagicMock()
        with patch.object(visual, "bpy", MagicMock()):
            visual._set_object_lifetime(obj, 2010, 2020, 2006)
        self.assertEqual(obj.driver_add.return_value.driver.expression, "frame < 5 or frame >= 15")

    def test_evidence_content_changes_identity_and_unsupported_extend_rejected(self):
        cfg = config.make_config(preview=True)
        cfg.update(historical_mode="EVIDENCE", timeline_start_year=2006)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text('{"schema_version":1,"features":[]}')
            cfg["historical_evidence_file"] = str(path)
            before = config.run_directory(cfg)
            path.write_text('{"schema_version":1,"features":[],"events":[]}')
            self.assertNotEqual(before, config.run_directory(cfg))
            cfg["run_mode"] = "EXTEND"
            with self.assertRaisesRegex(ValueError, "RUN_MODE"):
                config.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
