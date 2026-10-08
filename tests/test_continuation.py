"""Extension must preserve history and match uninterrupted execution."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import test_evolution
from test_performance import performance_case
from test_simulation import simulation

from metrosim26 import config
from metrosim26.continuation import inspect_extension
from metrosim26.history import read_continuation


class ContinuationTests(unittest.TestCase):
    def test_general_launcher_restores_settings_and_chains(self):
        from metrosim26.continuation import extension_config, scene_source

        short = self.prepare(self.cfg, "automatic-source")
        cfg = extension_config(short["run_directory"], 2029, output_dir=self.root / "outputs")
        self.assertEqual(cfg["bounds"], self.cfg["bounds"])
        self.assertEqual(cfg["seed"], self.cfg["seed"])
        cfg["enable_parallel_compute"] = False
        extended = simulation.prepare_simulation(cfg)[2]
        next_cfg = extension_config(extended["run_directory"], 2030)
        inspect_extension(next_cfg)
        scene = {
            "configuration_json": json.dumps(dict(cfg, run_directory=extended["run_directory"]))
        }
        self.assertEqual(scene_source(scene), extended["run_directory"])
        self.assertEqual(scene_source({}), "")
        with self.assertRaisesRegex(ValueError, "later"):
            extension_config(short["run_directory"], 2028)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg, self.grid, self.baseline = performance_case()
        self.cfg.update(output_dir=str(self.root), enable_parallel_compute=False, end_year=2028)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def prepare(self, cfg, name):
        with patch.object(simulation, "build_baseline", return_value=deepcopy(self.baseline)):
            return simulation.prepare_simulation(cfg, destination=self.root / name)[2]

    def assert_equivalent(self, extended, full):
        self.assertEqual(extended["summary"], full["summary"])
        self.assertEqual(
            json.dumps(extended["versions"], sort_keys=True),
            json.dumps(full["versions"], sort_keys=True),
        )
        for key in ("projects", "events", "infrastructure", "history"):
            self.assertEqual(
                json.dumps(extended.get(key), sort_keys=True),
                json.dumps(full.get(key), sort_keys=True),
                key,
            )
        for a, b in zip(extended["states"], full["states"]):
            for key in ("urban", "arch", "infill", "redevelopment", "new_development"):
                np.testing.assert_array_equal(a[key], b[key])
        first, second = (
            Path(extended["run_directory"]) / "simulation",
            Path(full["run_directory"]) / "simulation",
        )
        for path in second.rglob("*"):
            if path.is_file() and path.name not in {"metadata.json", "continuation.json"}:
                self.assertEqual(
                    path.read_bytes(), (first / path.relative_to(second)).read_bytes(), path.name
                )

    def split_case(self, **settings):
        self.cfg.update(settings)
        short = self.prepare(self.cfg, "short")
        source = Path(short["run_directory"]) / "simulation"
        original_bytes = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        long_cfg = dict(self.cfg, end_year=2032, save_continuation=True)
        full = self.prepare(long_cfg, "full")
        extension_cfg = dict(long_cfg, run_mode="EXTEND", extend_from=str(source))
        years = []
        with (
            patch.object(
                simulation, "build_baseline", side_effect=AssertionError("Rebuilt baseline")
            ),
            patch.object(
                simulation,
                "report_progress",
                side_effect=lambda phase, message, **kw: years.append(kw.get("year")),
            ),
        ):
            extended = simulation.prepare_simulation(
                extension_cfg, destination=self.root / "extended"
            )[2]
        self.assertEqual([y for y in years if y is not None], list(range(2029, 2033)))
        self.assert_equivalent(extended, full)
        for name, content in original_bytes.items():
            self.assertEqual((source / name).read_bytes(), content)
        read_continuation(Path(extended["run_directory"]) / "simulation")
        return source, extended, full, extension_cfg

    def test_legacy_without_contract_and_repeated_extension(self):
        _, extended, _, cfg = self.split_case()
        cfg.update(end_year=2034, extend_from=extended["run_directory"])
        again = simulation.prepare_simulation(cfg, destination=self.root / "again")[2]
        full = self.prepare(dict(self.cfg, end_year=2034, save_continuation=True), "full34")
        self.assert_equivalent(again, full)

    def test_pending_projects_and_spawned_extension(self):
        self.cfg.update(
            enable_projects=True,
            project_min_buildings=1,
            project_min_housing=1,
            project_min_jobs=1,
            parallel_min_tasks=1,
            parallel_min_batch_seconds=0,
        )
        source, _, full, cfg = self.split_case()
        self.assertTrue(read_continuation(source)["projects"]["pending_sites"])
        cfg.update(enable_parallel_compute=True, cpu_workers=2, validate_parallel_results=True)
        result = simulation.prepare_simulation(cfg, destination=self.root / "parallel")[2]
        self.assert_equivalent(result, full)

    def test_infrastructure_pressure_and_pending_segments(self):
        cfg, self.grid, self.baseline = test_evolution.InfrastructureTests().fixture()
        self.cfg.update(cfg)
        self.cfg.update(
            output_dir=str(self.root),
            end_year=2028,
            enable_parallel_compute=False,
            profile_performance=False,
            initial_households=10000,
            initial_jobs=10000,
            initial_housing_vacancy=0,
            initial_job_vacancy=0,
            max_sites_per_year=1,
            annual_household_growth_rate=0.08,
            annual_job_growth_rate=0.08,
        )
        source, _, _, _ = self.split_case()
        self.assertTrue(read_continuation(source)["infrastructure"]["infrastructure"])

    def test_history_is_preserved(self):
        self.split_case(historical_mode="EVIDENCE", timeline_start_year=2024)

    def test_historical_spawn_validation(self):
        source, _, full, cfg = self.split_case(historical_mode="EVIDENCE", timeline_start_year=2024)
        cfg.update(
            enable_parallel_compute=True,
            cpu_workers=2,
            parallel_min_tasks=1,
            parallel_min_batch_seconds=0,
            validate_parallel_results=True,
        )
        actual = simulation.prepare_simulation(cfg, destination=self.root / "historical-parallel")[
            2
        ]
        self.assert_equivalent(actual, full)

    def test_redevelopment_across_extension_boundary(self):
        from test_simulation import synthetic_baseline

        self.grid = simulation.Grid((41.20, -96.05, 41.203, -96.047), 0.35)
        self.baseline = synthetic_baseline(self.grid)
        self.baseline["urban"][:] = True
        self.baseline["arch"][:] = simulation.SUBURBAN
        self.baseline["centrality"][:] = 0.2
        self.cfg.update(
            bounds=list(self.grid.bounds),
            initial_households=1000,
            initial_jobs=0,
            annual_household_growth_rate=0.1,
            annual_job_growth_rate=0,
            min_redevelopment_age=3,
        )
        _, extended, _, _ = self.split_case()
        self.assertTrue(
            any(row["redevelopment_sites"] for row in extended["summary"] if row["year"] > 2028)
        )

    def test_external_job_and_scene_export(self):
        from test_runtime import wait_job

        from metrosim26.runtime import CalculationJob

        short = self.prepare(self.cfg, "short")
        cfg = dict(
            self.cfg,
            run_mode="EXTEND",
            extend_from=short["run_directory"],
            end_year=2030,
            cache_dir=str(self.root / "missing-cache"),
        )
        job = CalculationJob(cfg, sys.executable)
        status = wait_job(job)
        self.assertEqual(status["phase"], "scene_ready", status.get("error"))
        self.assertEqual(status["start_year"], 2029)
        manifest = json.loads((job.directory / "scene" / "manifest.json").read_text())
        self.assertEqual(manifest["cfg"]["end_year"], 2030)
        self.assertTrue(manifest["packets"])
        cfg["run_mode"] = "REPLAY"
        replay = simulation.prepare_simulation(cfg)[2]
        self.assertEqual(replay["states"][-1]["year"], 2030)

    def test_omaha_rename_migration(self):
        short = self.prepare(self.cfg, "omaha")
        source = Path(short["run_directory"]) / "simulation"
        path = source / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["model_version"] = "omaha-demand-sites-3.0"
        metadata["simulation_config"].update(
            model_version="omaha-demand-sites-3.0", osm_query_version="omaha-v3-relations-1"
        )
        path.write_text(json.dumps(metadata))
        cfg = dict(self.cfg, run_mode="EXTEND", extend_from=str(source), end_year=2032)
        result = simulation.prepare_simulation(cfg, destination=self.root / "migrated")[2]
        self.assert_equivalent(result, self.prepare(dict(self.cfg, end_year=2032), "full"))

    def test_invalid_sources_and_destinations_are_rejected(self):
        short = self.prepare(self.cfg, "short")
        source = Path(short["run_directory"]) / "simulation"
        cfg = dict(self.cfg, run_mode="EXTEND", extend_from=str(source), end_year=2032)
        for change, error in [
            ({"seed": 1}, "settings"),
            ({"enable_projects": True}, "settings"),
            ({"end_year": 2028}, "later"),
        ]:
            with self.assertRaisesRegex(ValueError, error):
                inspect_extension(dict(cfg, **change))
        with self.assertRaisesRegex(ValueError, "separate"):
            simulation.prepare_simulation(cfg, destination=source.parent)
        with self.assertRaisesRegex(ValueError, "empty"):
            occupied = self.root / "occupied"
            occupied.mkdir()
            (occupied / "keep.txt").write_text("keep")
            simulation.prepare_simulation(cfg, destination=occupied)
        (source / "2076.npz").write_bytes(b"irrelevant")
        (source / "2028.npz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            inspect_extension(cfg)

    def test_automatic_destination_and_replay(self):
        short = self.prepare(self.cfg, "short")
        cfg = dict(self.cfg, run_mode="EXTEND", extend_from=short["run_directory"], end_year=2030)
        result = simulation.prepare_simulation(cfg)[2]
        cfg["run_mode"] = "REPLAY"
        with patch.object(simulation, "build_baseline", side_effect=AssertionError("Rebuilt")):
            replay = simulation.prepare_simulation(cfg)[2]
        self.assertEqual(result["summary"], replay["summary"])
        canonical = config.run_directory(cfg)
        chained = dict(cfg, run_mode="EXTEND", extend_from=str(canonical), end_year=2032)
        self.assertEqual(inspect_extension(chained)[1]["end_year"], 2030)

    def test_checkpoint_retries_transient_file_lock(self):
        from metrosim26 import data

        original = data.os.replace
        calls = 0

        def briefly_locked(temporary, destination):
            nonlocal calls
            calls += 1
            if calls % 3:
                raise PermissionError("Temporary reader lock")
            original(temporary, destination)

        with (
            patch.object(data.os, "replace", side_effect=briefly_locked),
            patch.object(data.time, "sleep"),
        ):
            data.atomic_json(self.root / "checkpoint.json", {"year": 2077})
            data.atomic_npz(self.root / "checkpoint.npz", year=np.asarray(2077))
            simulation.save_summary_csv(self.root / "summary.csv", [{"year": 2077}])
        self.assertEqual(calls, 9)
        self.assertEqual(json.loads((self.root / "checkpoint.json").read_text()), {"year": 2077})
        with np.load(self.root / "checkpoint.npz") as saved:
            self.assertEqual(int(saved["year"]), 2077)
        self.assertEqual((self.root / "summary.csv").read_text(), "year\n2077\n")


if __name__ == "__main__":
    unittest.main()
