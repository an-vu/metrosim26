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
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from test_data import populate
from test_performance import performance_case
from test_simulation import config, data, omaha, rectangle, synthetic_baseline, visual

import omaha_runtime as runtime
import omaha_scene as geometry


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
            with patch.object(omaha, "build_baseline", return_value=baseline):
                omaha.prepare_simulation(cfg, destination=direct)
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
import omaha_simulation, omaha_runtime
_, _, baseline = performance_case()
baseline['urban'][:] = True
omaha_simulation.os.cpu_count = lambda: 20
omaha_simulation.build_baseline = lambda cfg, grid: baseline
raise SystemExit(omaha_runtime.run_job(sys.argv[2]))
"""
            process = subprocess.run(
                [sys.executable, "-c", script, str(Path(__file__).parent), str(job)],
                cwd=Path(runtime.__file__).parent,
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
            with patch.object(data.urllib.request, "urlopen", side_effect=AssertionError("HTTP")):
                omaha.prepare_simulation(cfg, destination=direct)
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
            self.assertIsNotNone(omaha.validate_saved_run(output, cfg))

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
                    "import sys; import omaha_runtime, omaha_scene, omaha_simulation; assert not {'bpy', 'mathutils', 'omaha_blender'} & sys.modules.keys()",
                ],
                cwd=Path(runtime.__file__).parent,
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
            importer.cleanup_step.side_effect = [False, True]
            importer.steps = iter([None, None])
            fake = MagicMock()
            fake.context.window_manager.windows = []
            with (
                patch.object(visual, "bpy", fake),
                patch.object(visual, "CalculationJob", return_value=job),
                patch.object(visual, "SceneImport", return_value=importer),
                patch.object(visual, "finalize_blender_output") as finalize,
            ):
                run = visual.BlenderJob(cfg, sys.executable)
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
                run = visual.BlenderJob(cfg, sys.executable)
                run.importer = importer
                run.cancel()
                self.assertEqual(run.tick(), 0.01)
                self.assertIsNone(run.tick())
                self.assertEqual(run.status["phase"], "cancelled")
                self.assertFalse(run.status["complete"])
                job.cancel.assert_called_once()
                job.poll.return_value = 1
                job.status.update(phase="error", error="Missing cache")
                run = visual.BlenderJob(cfg, sys.executable)
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
            grid = omaha.Grid(tuple(cfg["bounds"]), cfg["cell_km"])
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
                patch.object(visual, "bpy", fake),
                patch.object(visual, "_new_material"),
                patch.object(visual, "_configure_cycles"),
                patch.object(visual, "_configure_camera_and_lighting"),
                patch.object(visual, "_set_object_lifetime") as lifetime,
            ):
                importer = visual.SceneImport(tmp)
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
                partial = visual.SceneImport(tmp)
                while partial.done == 0:
                    next(partial.steps)
                owned = len(partial.owned)
                self.assertFalse(partial.cleanup_step())
                self.assertEqual(len(partial.owned), owned - 1)
                while not partial.cleanup_step():
                    pass
                self.assertFalse(importer.scene is None)


if __name__ == "__main__":
    unittest.main()
