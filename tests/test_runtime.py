"""External job, progress, geometry handoff, and incremental UI contracts."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from test_data import populate
from test_performance import performance_case
from test_simulation import config, data, rectangle, simulation, synthetic_baseline

import metrosim26.blender.job as blender_job
import metrosim26.blender.presentation as presentation
import metrosim26.blender.scene_import as scene_import
import metrosim26.blender_ui as visual
import metrosim26.runtime as runtime
import metrosim26.scene as geometry
from metrosim26.datasets import lodes as data_lodes


def small_config(directory):
    cfg = config.make_config(preview=True)
    cfg.update(
        bounds=[41.20, -96.05, 41.225, -96.015],
        end_year=2027,
        cache_dir=str(Path(directory) / "cache"),
        output_dir=str(Path(directory) / "output"),
        enable_parallel_compute=False,
        profile_performance=False,
        run_mode="SIMULATE",
    )
    return cfg


def wait_job(job, timeout=30):
    deadline = time.monotonic() + timeout
    while job.poll() is None:
        if time.monotonic() > deadline:
            job.cancel()
            job.process.wait(timeout=10)
            raise AssertionError("External job timed out")
        time.sleep(0.02)
    return job.status


def packet_faces(directory, packet):
    with np.load(Path(directory) / packet["file"], allow_pickle=False) as arrays:
        vertices, indices, offsets = arrays["vertices"], arrays["indices"], arrays["offsets"]
        return [vertices[indices[a:b]].tolist() for a, b in zip(offsets[:-1], offsets[1:])]


class RuntimeTests(unittest.TestCase):
    def test_external_process_keeps_existing_16_worker_broker_and_checkpoint_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg, grid, baseline = performance_case()
            baseline["urban"][:] = True
            cfg.update(
                output_dir=str(Path(tmp) / "output"),
                enable_parallel_compute=False,
                end_year=2029,
                parallel_min_tasks=1,
                parallel_min_batch_seconds=0,
                parallel_batch_size=16,
                cpu_workers=16,
                profile_performance=True,
                run_mode="SIMULATE",
            )
            direct = Path(tmp) / "direct"
            with patch.object(simulation, "build_baseline", return_value=baseline):
                simulation.prepare_simulation(cfg, destination=direct)
            cfg["enable_parallel_compute"] = True
            job = Path(tmp) / "job"
            job.mkdir()
            runtime.write_json(job / "config.json", cfg)
            # Substitute only synthetic input geography, inside a real standalone
            # process. The production runtime and its nested broker run unchanged.
            script = """
import sys
sys.path.insert(0, sys.argv[1])
from test_performance import performance_case
from metrosim26 import simulation, runtime
_, _, baseline = performance_case()
baseline['urban'][:] = True
simulation.os.cpu_count = lambda: 20
simulation.build_baseline = lambda cfg, grid: baseline
raise SystemExit(runtime.run_job(sys.argv[2]))
"""
            process = subprocess.run(
                [sys.executable, "-c", script, str(Path(__file__).parent), str(job)],
                cwd=Path(runtime.__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn("Worker pool ready: 16 spawned processes", process.stdout)
            for name in (
                "2026.npz",
                "2027.npz",
                "2028.npz",
                "2029.npz",
                "versions.json",
                "summary.csv",
            ):
                self.assertEqual(
                    (direct / "simulation" / name).read_bytes(),
                    (job / "result" / "simulation" / name).read_bytes(),
                    name,
                )

    def test_external_fresh_pipeline_and_replay_match_direct_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = small_config(tmp)
            populate(cfg)
            direct = Path(tmp) / "direct"
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("HTTP")):
                simulation.prepare_simulation(cfg, destination=direct)
            job = runtime.CalculationJob(cfg, sys.executable)
            self.assertEqual(
                wait_job(job)["phase"],
                "scene_ready",
                (job.directory / "calculation.log").read_text(),
            )
            output = job.directory / "result" / "simulation"
            for name in ("baseline.npz", "2026.npz", "2027.npz", "versions.json", "summary.csv"):
                self.assertEqual(
                    (output / name).read_bytes(), (direct / "simulation" / name).read_bytes(), name
                )
            self.assertTrue(job.status["complete"])
            self.assertIsNone(job.status["error"])
            manifest = json.loads((job.directory / "scene" / "manifest.json").read_text())
            self.assertTrue(manifest["packets"])
            pointer = config.run_directory(cfg) / "latest_completed.json"
            before = pointer.read_bytes()
            cfg["run_mode"] = "REPLAY"
            cfg["cache_dir"] = str(Path(tmp) / "missing")
            replay = runtime.CalculationJob(cfg, sys.executable)
            self.assertEqual(wait_job(replay)["phase"], "scene_ready")
            self.assertEqual(Path(replay.status["result_directory"]), output.parent.resolve())
            self.assertEqual(pointer.read_bytes(), before)
            cfg["run_mode"] = "SIMULATE"
            failed = runtime.CalculationJob(cfg, sys.executable)
            self.assertEqual(wait_job(failed)["phase"], "error")
            self.assertIn("input cache is incomplete", failed.status["error"])
            self.assertFalse(failed.status["complete"])
            self.assertEqual(pointer.read_bytes(), before)
            self.assertIsNotNone(simulation.validate_saved_run(output, cfg))

    def test_progress_phases_and_headless_import_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = small_config(tmp)
            populate(cfg)
            folder = Path(tmp) / "job"
            folder.mkdir()
            runtime.write_json(folder / "config.json", cfg)
            events = []
            update = runtime.Progress.update

            def record(reporter, **fields):
                if "phase" in fields:
                    events.append(fields["phase"])
                update(reporter, **fields)

            with patch.object(runtime.Progress, "update", record):
                self.assertEqual(runtime.run_job(folder), 0)
            for phase in (
                "validating_cache",
                "loading_osm",
                "parsing_osm",
                "loading_lodes",
                "deriving_baseline",
                "building_spatial_index",
                "saving_baseline",
                "simulation",
                "preparing_scene",
                "scene_ready",
            ):
                self.assertIn(phase, events)
            self.assertLess(events.index("saving_baseline"), events.index("simulation"))
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; import metrosim26.runtime, metrosim26.scene, metrosim26.simulation; assert not {'bpy', 'mathutils', 'metrosim26.blender_ui'} & sys.modules.keys()",
                ],
                cwd=Path(runtime.__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_atomic_progress_heartbeat_and_no_simulation_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            reporter = runtime.Progress(path, {"base_year": 2026, "end_year": 2076})
            try:
                first = json.loads(path.read_text())["heartbeat_utc"]
                reporter.update(phase="simulation", year=2042, worker_processes=16)
                deadline = time.monotonic() + 1.3
                while time.monotonic() < deadline:
                    status = json.loads(path.read_text())
                    self.assertEqual(status["year"], 2042)
                    time.sleep(0.005)
                self.assertGreater(status["heartbeat_utc"], first)
                self.assertGreater(status["elapsed_seconds"], 1)
                self.assertFalse(status["complete"])
            finally:
                reporter.close()

    @unittest.skipIf(os.name == "nt", "POSIX process groups; Windows uses taskkill /T")
    def test_cancel_terminates_parent_and_separate_broker_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # Reproduce the existing broker's separate process-group boundary.
            script = """
import json, pathlib, subprocess, sys, time
broker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(json.dumps({'phase': 'simulation', 'broker_pid': broker.pid}))
time.sleep(60)
"""
            job = runtime.CalculationJob.__new__(runtime.CalculationJob)
            job.directory = directory
            job.status = {}
            job.killer = None
            job.process = subprocess.Popen(
                [sys.executable, "-c", script, str(directory / "progress.json")],
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not job.status.get("broker_pid"):
                    job.poll()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                broker = job.status["broker_pid"]
                job.cancel()
                job.process.wait(timeout=5)
                self.assertTrue(job.cancellation_finished())
                # A dead orphan may briefly be a zombie before OS reaping.
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(broker)], capture_output=True, text=True
                ).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                job.cancel()


class SceneHandoffTests(unittest.TestCase):
    def test_timer_completion_cancellation_and_child_error(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = small_config(tmp)
            cfg["run_directory"] = tmp
            job = MagicMock()
            job.directory = Path(tmp)
            (job.directory / "calculation.log").touch()
            job.log_offset = 0
            job.status = dict(phase="starting", message="Starting", worker_processes=0)
            job.poll.return_value = None
            job.read_log.return_value = ""
            job.cancellation_finished.return_value = True
            importer = MagicMock()
            importer.cfg = cfg
            importer.scene.frame_end = 2
            importer.done = 1
            importer.total = 2
            importer.message = "Mesh import"
            importer.warning = ""
            importer.revealing = False
            importer.cleanup_step.side_effect = [False, True]
            importer.steps = iter([None, None])
            fake = MagicMock()
            fake.context.window_manager.windows = []
            with (
                patch.object(visual, "bpy", fake),
                patch.object(blender_job, "bpy", fake),
                patch.object(blender_job, "CalculationJob", return_value=job),
                patch.object(blender_job, "SceneImport", return_value=importer),
                patch.object(blender_job, "finalize_blender_output") as finalize,
            ):
                run = blender_job.BlenderJob(
                    cfg,
                    sys.executable,
                    read_settings=visual.setup_config,
                    start_build=visual.start_blender_job,
                    apply_palette=visual.palette_changed,
                )
                self.assertEqual(run.tick(), 0.25)
                job.poll.return_value = 0
                job.status.update(phase="scene_ready")
                self.assertEqual(run.tick(), 0.01)
                self.assertEqual(run.status["phase"], "building_scene")
                self.assertEqual(run.tick(), 0.01)
                self.assertIsNone(run.tick())
                self.assertEqual(run.status["phase"], "complete")
                self.assertFalse(run.active)
                finalize.assert_called_once()
                run = blender_job.BlenderJob(
                    cfg,
                    sys.executable,
                    read_settings=visual.setup_config,
                    start_build=visual.start_blender_job,
                    apply_palette=visual.palette_changed,
                )
                run.importer = importer
                run.cancel()
                self.assertEqual(run.tick(), 0.01)
                self.assertIsNone(run.tick())
                self.assertEqual(run.status["phase"], "cancelled")
                self.assertFalse(run.status["complete"])
                job.cancel.assert_called_once()
                job.poll.return_value = 1
                job.status.update(phase="error", error="Missing cache")
                run = blender_job.BlenderJob(
                    cfg,
                    sys.executable,
                    read_settings=visual.setup_config,
                    start_build=visual.start_blender_job,
                    apply_palette=visual.palette_changed,
                )
                self.assertEqual(run.tick(), 0.1)
                self.assertIsNone(run.tick())
                self.assertEqual(run.status["phase"], "error")
                self.assertIn("Missing cache", run.status["error"])

    def test_chunk_bound_and_exact_face_geometry_for_large_polygon(self):
        with tempfile.TemporaryDirectory() as tmp:
            packets = []
            vertices = [(float(i), float(i % 7), 0.0) for i in range(9000)]
            faces = [(i, i + 1, i + 2) for i in range(0, 9000, 3)]
            batch = geometry.PacketBatch(
                Path(tmp), packets, "future", "test", "mat", 1000, (2027, 2030, 2026)
            )
            batch.add(vertices, faces)
            batch.flush()
            rebuilt = []
            for packet in packets:
                with np.load(Path(tmp) / packet["file"]) as arrays:
                    self.assertLessEqual(len(arrays["vertices"]), 1000)
                self.assertEqual(packet["lifetime"], (2027, 2030, 2026))
                rebuilt.extend(packet_faces(tmp, packet))
            self.assertEqual(rebuilt, [[list(vertices[i]) for i in face] for face in faces])

    def test_export_and_incremental_import_preserve_faces_materials_and_lifetimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_config(tmp)
            grid = simulation.Grid(tuple(cfg["bounds"]), cfg["cell_km"])
            baseline = synthetic_baseline(grid)
            polygon = {"outer": rectangle(0, 0, 0.1, 0.1), "holes": []}
            baseline["buildings"] = [{"id": "a", "polygons": [polygon], "height_m": 6}]
            baseline["roads"] = [{"id": "r", "points": [(0, 0), (1, 1)], "width_km": 0.01}]
            baseline["land"] = [{"id": "w", "polygons": [polygon], "kind": "water"}]
            versions = [
                {
                    "id": "site",
                    "version": 0,
                    "start_year": 2027,
                    "end_year": None,
                    "buildings": [{"id": "future", "poly": polygon["outer"], "height_m": 9}],
                    "surfaces": [],
                }
            ]
            manifest = geometry.export_scene(
                cfg, grid, baseline, {"run_directory": tmp, "versions": versions}, tmp
            )
            packet = next(p for p in manifest["packets"] if p["name"].startswith("OSM_Buildings"))
            vertices, faces = geometry._building_geometry(polygon, 6 * cfg["vertical_exaggeration"])
            self.assertEqual(
                packet_faces(tmp, packet), [[list(vertices[i]) for i in face] for face in faces]
            )
            fake = MagicMock()
            fake.data.meshes.new.side_effect = lambda name: MagicMock(name=name)
            fake.data.objects.new.side_effect = lambda name, mesh: MagicMock(name=name)
            fake.context.screen = None
            with (
                patch.object(scene_import, "bpy", fake),
                patch.object(presentation, "bpy", fake),
                patch.object(scene_import, "_new_material"),
                patch.object(scene_import, "_configure_cycles"),
                patch.object(scene_import, "_configure_camera_and_lighting"),
                patch.object(scene_import, "_set_object_lifetime") as lifetime,
            ):
                importer = scene_import.SceneImport(tmp)
                while True:
                    count = fake.data.meshes.new.call_count
                    try:
                        next(importer.steps)
                    except StopIteration:
                        break
                    self.assertLessEqual(fake.data.meshes.new.call_count - count, 1)
                self.assertTrue(importer.finished)
                self.assertEqual(importer.done, len(manifest["packets"]))
                self.assertEqual(lifetime.call_args.args[1:], (2027, None, 2026))
                for call, packet in zip(fake.data.objects.new.call_args_list, manifest["packets"]):
                    mesh = call.args[1]
                    verts, _, imported_faces = mesh.from_pydata.call_args.args
                    self.assertEqual(
                        [[verts[i] for i in face] for face in imported_faces],
                        packet_faces(tmp, packet),
                    )
                # Cancellation removes at most one owned ID per tick.
                partial = scene_import.SceneImport(tmp)
                while partial.done == 0:
                    next(partial.steps)
                owned = len(partial.owned)
                self.assertFalse(partial.cleanup_step())
                self.assertEqual(len(partial.owned), owned - 1)
                while not partial.cleanup_step():
                    pass
                self.assertFalse(importer.scene is None)


class CircularAreaTests(unittest.TestCase):
    def test_units_identity_and_supported_region(self):
        base = config.make_config(preview=True)
        km = config.circle_config(base, 41.25, -95.99, 5)
        miles = config.circle_config(base, 41.25, -95.99, 5 / 1.609344, "MILES")
        self.assertEqual(config.run_directory(km), config.run_directory(miles))
        roundtrip = config.circle_config(
            base, float(np.float32(41.25)), float(np.float32(-95.99)), float(np.float32(5))
        )
        self.assertEqual(config.run_directory(km), config.run_directory(roundtrip))
        self.assertEqual(base["bounds"], list(config.PREVIEW_BOUNDS))
        visual = dict(km, progressive_build=False, job_action="PREFETCH")
        self.assertEqual(config.simulation_config(km), config.simulation_config(visual))
        for lat, lon, radius in [(41.25, -95.99, 160), (0, 0, 5), (41.25, -95.99, float("nan"))]:
            with self.assertRaises(ValueError):
                config.circle_config(base, lat, lon, radius)

    def test_edge_buildings_remain_collision_inputs_but_not_capacity(self):
        from metrosim26.workers import Grid, footprint_allowed

        cfg = config.circle_config(config.make_config(preview=True), 41.25, -95.99, 1)
        grid = Grid(cfg["bounds"], cfg["cell_km"])
        baseline = synthetic_baseline(grid)
        baseline["study_radius_km"] = 1
        # A crossing polygon remains protected even though it adds no full-building capacity.
        baseline["buildings"] = [
            {
                "id": "edge",
                "arch": 1,
                "height_m": 6,
                "polygons": [
                    {"outer": [(0.9, -0.1), (1.1, -0.1), (1.1, 0.1), (0.9, 0.1)], "holes": []}
                ],
            }
        ]
        baseline.pop("spatial_index", None)
        self.assertEqual(
            simulation.estimate_baseline_capacity(cfg, grid, baseline)["housing_capacity"], 0
        )
        cell = grid.index(0.99, 0)
        self.assertFalse(
            footprint_allowed(
                [(0.97, 0), (1.01, 0), (1.01, 0.02), (0.97, 0.02)], baseline, grid, *cell
            )
        )
        from metrosim26.infrastructure import clear_corridor

        grid.study_radius_km = 1
        self.assertFalse(clear_corridor([(0.9, 0), (1.1, 0), (1.1, 0.01)], grid, []))

    def test_circle_mesh_clipping_and_append_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            packets = []
            batch = geometry.PacketBatch(
                Path(tmp), packets, "baseline", "edge", "mat", 12000, radius=1
            )
            batch.add([(0, -2, 0), (2, 0, 0), (0, 2, 0)], [(0, 1, 2)])
            batch.flush()
            self.assertTrue(packets)
            for face in packet_faces(tmp, packets[0]):
                self.assertTrue(all(np.hypot(v[0], v[1]) <= 1 + 1e-10 for v in face))
            cfg = config.circle_config(config.make_config(preview=True), 41.25, -95.99, 0.5)
            from metrosim26.workers import Grid

            grid = Grid(cfg["bounds"], cfg["cell_km"])
            baseline = synthetic_baseline(grid)
            result = {"versions": [], "run_directory": tmp}
            initial = geometry.export_scene(
                cfg, grid, baseline, result, Path(tmp) / "scene", complete=False
            )
            before = {
                p["file"]: (Path(tmp) / "scene" / p["file"]).read_bytes()
                for p in initial["packets"]
            }
            final = geometry.export_scene(
                cfg, grid, baseline, result, Path(tmp) / "scene", append=True
            )
            self.assertTrue(final["complete"])
            self.assertEqual(final["packets"][: len(initial["packets"])], initial["packets"])
            for name, blob in before.items():
                self.assertEqual((Path(tmp) / "scene" / name).read_bytes(), blob)

    def test_prefetch_job_reuses_cache_and_generation_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = small_config(tmp)
            cfg = config.circle_config(cfg, 41.25, -95.99, 0.5)
            populate(cfg)
            prep = runtime.CalculationJob(dict(cfg, job_action="PREFETCH"), sys.executable)
            self.assertEqual(wait_job(prep)["phase"], "cache_ready")
            job = runtime.CalculationJob(cfg, sys.executable)
            self.assertEqual(
                wait_job(job)["phase"],
                "scene_ready",
                (job.directory / "calculation.log").read_text(),
            )
            manifest = json.loads((job.directory / "scene" / "manifest.json").read_text())
            self.assertTrue(manifest["complete"])
            self.assertEqual(
                len({p["file"] for p in manifest["packets"]}), len(manifest["packets"])
            )
            baseline = data.load_baseline(
                job.directory / "result" / "simulation" / "baseline.npz",
                __import__("metrosim26.workers", fromlist=["Grid"]).Grid(
                    cfg["bounds"], cfg["cell_km"]
                ),
            )
            self.assertFalse(np.any(baseline["urban"][~baseline["study_mask"]]))

    def test_estimates_ignore_corrupt_and_cached_download_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.circle_config(small_config(tmp), 41.25, -95.99, 1)
            output = Path(cfg["output_dir"])
            output.mkdir()
            runtime.write_json(
                output / "area_timings.json", [None, {}, {"action": "SIMULATE", "radius_km": 0}]
            )
            self.assertIn("not enough", " ".join(visual.estimate_text(cfg)))
            runtime.save_timing(dict(cfg, missing_inputs=0), "PREFETCH", 600)
            self.assertNotIn("roughly", " ".join(visual.estimate_text(cfg)))
            runtime.save_timing(dict(cfg, missing_inputs=7), "PREFETCH", 600)
            self.assertIn("roughly", " ".join(visual.estimate_text(cfg)))


class WorldMapTests(unittest.TestCase):
    def test_world_map_needs_no_employment_data_and_never_simulates(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = config.circle_config(dict(small_config(tmp), view_mode="MAP"), 51.5, -0.12, 0.5)
            cfg["end_year"] = cfg["base_year"]
            specs = data.expected_inputs(cfg)
            self.assertTrue(specs)
            self.assertTrue(all(spec.kind == "osm" for spec in specs))
            elements = [
                {"type": "node", "id": 1, "lat": 51.4999, "lon": -0.1201},
                {"type": "node", "id": 2, "lat": 51.4999, "lon": -0.1199},
                {"type": "node", "id": 3, "lat": 51.5001, "lon": -0.1199},
                {"type": "node", "id": 4, "lat": 51.5001, "lon": -0.1201},
                {"type": "way", "id": 5, "nodes": [1, 2, 3, 4, 1], "tags": {"building": "yes"}},
            ]
            for spec in specs:
                spec.path.parent.mkdir(parents=True, exist_ok=True)
                runtime.write_json(spec.path, {"elements": elements})
            jobdir = Path(tmp) / "map-job"
            jobdir.mkdir()
            runtime.write_json(jobdir / "config.json", cfg)
            with (
                patch.object(
                    simulation, "prepare_simulation", side_effect=AssertionError("Growth")
                ),
                patch.object(data_lodes, "load_lodes", side_effect=AssertionError("LODES")),
            ):
                self.assertEqual(runtime.run_job(jobdir), 0)
            manifest = json.loads((jobdir / "scene" / "manifest.json").read_text())
            self.assertTrue(any(p["collection"] == "baseline" for p in manifest["packets"]))
            self.assertFalse(any(p["collection"] == "future" for p in manifest["packets"]))
            self.assertFalse((config.run_directory(cfg) / "latest_completed.json").exists())
            with self.assertRaisesRegex(ValueError, "Growth is not supported"):
                config.circle_config(small_config(tmp), 51.5, -0.12, 0.5)

    def test_empty_coverage_has_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            cfg = config.circle_config(dict(small_config(tmp), view_mode="MAP"), -33.86, 151.2, 0.5)
            populate(cfg)
            jobdir = Path(tmp) / "empty-map"
            jobdir.mkdir()
            runtime.write_json(jobdir / "config.json", cfg)
            self.assertEqual(runtime.run_job(jobdir), 1)
            message = json.loads((jobdir / "progress.json").read_text())["message"]
            self.assertIn("OpenStreetMap", message)
            self.assertIn("No usable 3D features", message)
            layout = MagicMock()
            visual.readable_labels(layout, message, 24)
            self.assertGreater(layout.label.call_count, 2)
            self.assertTrue(
                all(len(call.kwargs["text"]) <= 24 for call in layout.label.call_args_list)
            )

    def test_legacy_cache_reuse_requires_full_cell_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.circle_config(
                dict(small_config(tmp), view_mode="MAP"), 22.2966, 113.951, 2, "MILES"
            )
            old = dict(cfg, bounds=[22.0, 113.0, 23.0, 115.0], osm_tiles=[1, 1])
            spec = data.legacy_osm_inputs(old)[0]
            spec.path.parent.mkdir(parents=True)
            runtime.write_json(spec.path, {"elements": []})
            record = data.validate_input(spec)
            data._write_cache_manifest(old, [spec], {str(spec.path): record}, [])
            target = data.osm_inputs(cfg)[0]
            self.assertTrue(data.reuse_legacy_tile(target, cfg))
            self.assertEqual(data.validate_osm(target.path), data.validate_osm(spec.path))
            target.path.unlink()
            self.assertFalse(
                data.reuse_legacy_tile(target, dict(cfg, osm_data_snapshot="2020-01-01T00:00:00Z"))
            )

    def test_smaller_circle_reuses_fixed_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = dict(small_config(tmp), view_mode="MAP")
            large = config.circle_config(base, 22.2966, 113.951, 5, "MILES")
            small = config.circle_config(base, 22.2966, 113.951, 2, "MILES")
            large_paths = {spec.path for spec in data.osm_inputs(large)}
            small_paths = {spec.path for spec in data.osm_inputs(small)}
            self.assertTrue(small_paths)
            self.assertLess(len(small_paths), len(large_paths))
            self.assertTrue(small_paths <= large_paths)
            historical = dict(small, osm_data_snapshot="2020-01-01T00:00:00Z")
            self.assertTrue(small_paths.isdisjoint({s.path for s in data.osm_inputs(historical)}))

    def test_units_and_names_are_presentation_settings(self):
        from types import SimpleNamespace

        cfg = config.circle_config(
            dict(config.make_config(), view_mode="MAP"), 41.25, -95.99, 2, "MILES"
        )
        cfg.update(display_units="MILES", location_name="Omaha / Nebraska")
        settings = SimpleNamespace()
        visual.apply_setup(settings, cfg)
        self.assertEqual(settings.units, "MILES")
        self.assertAlmostEqual(settings.radius, 2, places=4)
        self.assertTrue(config.run_directory(cfg).name.startswith("Omaha__Nebraska_"))
        self.assertEqual(
            config.simulation_config(cfg),
            config.simulation_config(dict(cfg, display_units="KM", location_name="Other")),
        )

    def test_countdown_cancel_preserves_downloads_and_does_not_build(self):
        run = visual.BlenderJob.__new__(visual.BlenderJob)
        run.auto_deadline = time.monotonic() + 5
        run.active = True
        run.status = {}
        with patch.object(visual, "start_blender_job") as start:
            run.cancel()
            self.assertFalse(run.active)
            self.assertIsNone(run.auto_deadline)
            start.assert_not_called()
            self.assertEqual(run.status["phase"], "cache_ready")

    def test_include_layers_filter_export_without_changing_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(
                small_config(tmp),
                include_buildings=False,
                include_roads=False,
                include_parks=False,
                include_water=False,
                include_ground=False,
            )
            grid = __import__("metrosim26.workers", fromlist=["Grid"]).Grid(
                cfg["bounds"], cfg["cell_km"]
            )
            from test_simulation import synthetic_baseline

            baseline = synthetic_baseline(grid)
            manifest = geometry.export_scene(
                cfg, grid, baseline, {"versions": [], "run_directory": tmp}, Path(tmp) / "scene"
            )
            self.assertEqual(manifest["packets"], [])
            self.assertTrue(baseline["urban"].any())

    def test_elevation_interpolates_and_coastline_puts_water_on_right(self):
        from metrosim26.geography import Geography

        geo = Geography.__new__(Geography)
        geo.grid = __import__("types").SimpleNamespace(
            center_lon=0.5,
            center_lat=0.5,
            cos_lat=1,
            min_x=-1,
            max_x=1,
            min_y=-1,
            max_y=1,
            width=2,
            height=2,
        )
        geo.tiles = {(0, 0): np.array([[100, 200], [300, 400]], dtype=np.int16)}
        self.assertAlmostEqual(geo.height(0, 0), 0.25)
        geo.segments = [((0, -2), (0, 2))]
        cells = list(geo.surface())
        self.assertTrue(any(sea for sea, _ in cells))
        for sea, points in cells:
            self.assertEqual(sea, sum(v[0] for v in points) / 4 > 0)

    def test_storage_folder_selection_reaches_input_and_job_paths(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                view_mode="MAP",
                latitude=51.5,
                longitude=-0.12,
                radius=0.5,
                units="KM",
                end_year=2076,
                run_mode="SIMULATE",
                progressive_build=True,
                cache_dir=tmp + "/inputs",
                output_dir=tmp + "/results",
            )
            cfg = visual.setup_config(settings)
            self.assertEqual(Path(cfg["cache_dir"]), Path(tmp).resolve() / "inputs")
            self.assertEqual(config.run_directory(cfg).parent, Path(tmp).resolve() / "results")
            self.assertTrue(
                all(
                    spec.path.is_relative_to(Path(tmp).resolve() / "inputs")
                    for spec in data.expected_inputs(cfg)
                )
            )
            with self.assertRaises(ValueError):
                visual.storage_path("")
            file = Path(tmp) / "file"
            file.write_text("test")
            with self.assertRaises(ValueError):
                visual.storage_path(str(file))


if __name__ == "__main__":
    unittest.main()


class CoastlineGeometryTests(unittest.TestCase):
    def test_diagonal_coast_cuts_cells_and_preserves_total_area(self):
        from types import SimpleNamespace

        from metrosim26.geography import Geography

        geo = Geography.__new__(Geography)
        geo.grid = SimpleNamespace(min_x=-1, max_x=1, min_y=-1, max_y=1, width=2, height=2)
        geo.segments = [((-2, -1.8), (2, 2.2))]
        total = 0
        intersections = 0
        for sea, points in geo.surface():
            total += (
                abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1])))
                / 2
            )
            sides = [y - x - 0.2 for x, y, _ in points]
            if sea:
                self.assertLessEqual(max(sides), 1e-8)
            else:
                self.assertGreaterEqual(min(sides), -1e-8)
            intersections += sum(abs(side) < 1e-8 for side in sides)
        self.assertAlmostEqual(total, 4, places=7)
        self.assertGreater(intersections, 10)

    def test_surface_packets_share_adjacent_vertices(self):
        with tempfile.TemporaryDirectory() as directory:
            packets = []
            batch = geometry.PacketBatch(
                Path(directory), packets, "Ground", "Study_Ground", "ground", 1000, weld=True
            )
            batch.add([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [[0, 1, 2, 3]])
            batch.add([(1, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)], [[0, 1, 2, 3]])
            self.assertEqual(len(batch.vertices), 6)
            self.assertEqual(len(batch.faces), 2)
            batch.flush()
            self.assertEqual(len(batch.vertex_lookup), 0)


class CoordinatePasteTests(unittest.TestCase):
    def test_decimal_compass_pair_and_reversed_compass_order(self):
        expected = {"latitude": 22.2966, "longitude": 113.951}
        for axis in ("latitude", "longitude"):
            self.assertEqual(visual.parse_coordinates("22.29660° N, 113.95100° E", axis), expected)
        self.assertEqual(visual.parse_coordinates("113.951 E, 22.2966 N"), expected)
        self.assertEqual(
            visual.parse_coordinates("41.25, -95.99"), {"latitude": 41.25, "longitude": -95.99}
        )
        self.assertEqual(visual.parse_coordinates("95.99° W", "longitude"), {"longitude": -95.99})

    def test_bad_coordinate_text_and_bounds_rejected(self):
        for text in ("hello", "100 N, 22 E", "22 N, 30 N", "22.3.4, 30", "22, 181"):
            with self.assertRaises(ValueError):
                visual.parse_coordinates(text)


class RingTerrainTests(unittest.TestCase):
    def test_road_triangles_follow_terrain_plane_with_clearance(self):
        from metrosim26.geography import Geography

        geo = Geography.__new__(Geography)
        geo.spacing = 0.03
        geo.tiles = {"synthetic": True}
        geo.height = lambda x, y: 0.1 + x * x + 0.7 * y * y
        points = geo.road_points([(-0.08, -0.05), (0.08, 0.07)])
        vertices, faces = geometry._road_geometry(points, 0.008)
        vertices, faces = geo.drape_road(vertices, faces)
        self.assertGreater(len(faces), 10)
        for face in faces:
            centroid = np.mean([vertices[index] for index in face], axis=0)
            self.assertAlmostEqual(
                centroid[2] - geo.surface_height(*centroid[:2]), 0.0003, places=8
            )

    def test_ring_ground_has_no_inner_faces_and_preserves_area(self):
        def area(batch):
            return sum(
                abs(
                    sum(
                        batch.vertices[a][0] * batch.vertices[b][1]
                        - batch.vertices[b][0] * batch.vertices[a][1]
                        for a, b in zip(face, face[1:] + face[:1])
                    )
                )
                / 2
                for face in batch.faces
            )

        with tempfile.TemporaryDirectory() as directory:
            vertices = [(-3, -3, 0), (3, -3, 0), (3, 3, 0), (-3, 3, 0)]
            batches = []
            for radius, inner in ((1, 0), (2, 1), (2, 0)):
                batch = geometry.PacketBatch(
                    Path(directory),
                    [],
                    "ground",
                    "Study_Ground",
                    "ground",
                    12000,
                    radius=radius,
                    inner_radius=inner,
                )
                batch.add(vertices, [[0, 1, 2, 3]])
                batches.append(batch)
            self.assertAlmostEqual(area(batches[0]) + area(batches[1]), area(batches[2]), places=7)
            self.assertTrue(
                all(np.hypot(v[0], v[1]) >= np.cos(np.pi / 96) - 1e-8 for v in batches[1].vertices)
            )


class MapFeatureWaterTests(unittest.TestCase):
    def test_map_packets_keep_feature_identity_and_water_clear_of_terrain(self):
        from metrosim26.geography import Geography
        from metrosim26.workers import Grid

        def initialize(geo, cfg, grid):
            geo.grid = grid
            geo.spacing = 0.03
            geo.tiles = {"synthetic": True}
            geo.segments = []
            geo.inner_radius = 0
            geo.height = lambda x, y: 0.02 + 0.3 * x * x + 0.2 * y * y

        with tempfile.TemporaryDirectory() as directory:
            cfg = config.circle_config(small_config(directory), 41.25, -95.99, 0.2, "KM")
            cfg.update(
                view_mode="MAP",
                include_ground=False,
                include_ocean=False,
                include_terrain=True,
                include_buildings=False,
            )
            grid = Grid(cfg["bounds"], cfg["cell_km"])
            baseline = synthetic_baseline(grid)
            baseline["roads"] = [
                {"id": f"way/{index}", "points": [(-0.08, y), (0.08, y)], "width_km": 0.008}
                for index, y in enumerate((-0.05, 0.05))
            ]
            baseline["land"] = [
                {
                    "id": "way/river",
                    "kind": "water",
                    "polygons": [
                        {
                            "outer": [(-0.09, -0.03), (0.09, -0.03), (0.09, 0.03), (-0.09, 0.03)],
                            "holes": [],
                        }
                    ],
                }
            ]
            folder = Path(directory) / "scene"
            with patch.object(Geography, "__init__", initialize):
                manifest = geometry.export_scene(
                    cfg, grid, baseline, {"versions": [], "run_directory": directory}, folder
                )
                geo = Geography(cfg, grid)
            roads = [
                p for p in manifest["packets"] if p["properties"].get("feature_kind") == "road"
            ]
            self.assertEqual({p["properties"]["feature_id"] for p in roads}, {"way/0", "way/1"})
            water = [
                p for p in manifest["packets"] if p["properties"].get("feature_id") == "way/river"
            ]
            self.assertTrue(water)
            for packet in water:
                with np.load(folder / packet["file"]) as mesh:
                    for a, b in zip(mesh["offsets"][:-1], mesh["offsets"][1:]):
                        center = mesh["vertices"][mesh["indices"][a:b]].mean(axis=0)
                        self.assertAlmostEqual(
                            center[2] - geo.surface_height(*center[:2]), 0.0003, places=8
                        )
