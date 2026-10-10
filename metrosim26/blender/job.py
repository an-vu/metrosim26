"""Job polling, cancellation, import scheduling, and countdown. UI callbacks are injected."""

import math
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from metrosim26.config import timeline_start
from metrosim26.runtime import CalculationJob, save_timing, write_json

from .presentation import finalize_blender_output
from .scene_import import SceneImport

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


class BlenderJob:
    def __init__(self, cfg, executable, *, read_settings, start_build, apply_palette):
        self.read_settings = read_settings
        self.start_build = start_build
        self.apply_palette = apply_palette
        self.cfg = dict(cfg)
        self.auto_deadline = None
        self.child_complete = False
        self.progressive = bool(cfg.get("progressive_build"))
        self.job = CalculationJob(cfg, executable)
        self.status = dict(self.job.status)
        self.started = time.monotonic()
        self.scene_started = None
        self.importer = None
        self.active = True
        self.cancel_requested = False
        self.failure = None
        self.failure_message = None
        self.last_phase = None
        self.last_written = 0.0
        self.timer_callback = self.tick

    def cancel(self):
        if self.auto_deadline is not None:
            self.auto_deadline = None
            self.active = False
            self.status.update(
                phase="cache_ready", message="Automatic build cancelled. Data is saved."
            )
            return
        if self.active and not self.cancel_requested:
            self.cancel_requested = True
            self.job.cancel()
            self.status.update(
                phase="cancelling", message="Stopping calculation / removing partial scene"
            )

    def publish(self, force=False):
        now = time.monotonic()
        self.status["elapsed_seconds"] = now - self.started
        # The child owns progress.json until it exits, including during early import.
        if self.job.poll() is None:
            return
        if force or now - self.last_written >= 0.5:
            self.status["heartbeat_utc"] = time.time()
            write_json(self.job.directory / "progress.json", self.status)
            self.last_written = now

    def tick(self):
        assert bpy is not None
        try:
            delay = self._tick()
        except Exception as exc:
            self.failure_message = (
                self.status.get("message") if self.status.get("error") else str(exc)
            )
            self.failure = traceback.format_exc()
            print(self.failure)
            self.cancel()
            delay = 0.1
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        return delay

    def _tick(self):
        if not self.active:
            return None
        if self.auto_deadline is not None:
            settings = bpy.context.scene.metrosim26_setup
            remaining = max(0, math.ceil(self.auto_deadline - time.monotonic()))
            current = self.read_settings(settings)
            if any(
                current.get(key) != self.cfg.get(key)
                for key in (
                    "study_center",
                    "study_radius_km",
                    "cache_dir",
                    "output_dir",
                    "view_mode",
                )
            ):
                self.cancel()
                return None
            self.status.update(phase="auto_build", message=f"Building map in {remaining} seconds…")
            if remaining:
                return 0.1
            self.active = False
            self.auto_deadline = None
            self.start_build(current)
            return None
        if self.cancel_requested:
            if not self.job.cancellation_finished():
                return 0.1
            if self.importer is not None and not self.importer.cleanup_step():
                return 0.01
            self.status.update(
                phase="error" if self.failure else "cancelled",
                complete=False,
                error=self.failure,
                worker_processes=0,
                broker_pid=None,
                message=(self.failure_message or "Failed; see log")
                if self.failure
                else "Cancelled; saved checkpoints retained",
            )
            self.publish(force=True)
            self.active = False
            print("[MetroSim26] " + self.status["message"])
            return None
        code = self.job.poll()
        log = self.job.read_log()
        if log:
            print(log)
        self.status = dict(self.job.status)
        phase = self.status.get("phase")
        if phase != self.last_phase:
            print("[MetroSim26] " + self.status.get("message", str(phase)))
            self.last_phase = phase
        if code == 0 and phase == "cache_ready":
            if self.cfg.get("view_mode") == "MAP" and self.cfg.get("auto_build", True):
                self.auto_deadline = time.monotonic() + 10
                self.status.update(phase="auto_build", message="Building map in 10 seconds…")
                return 0.1
            self.active = False
            self.status.update(message="Data ready. Click Build Map.")
            return None
        if code == 0 and phase == "scene_ready":
            self.child_complete = True
        if code is not None and (code != 0 or not self.child_complete):
            raise RuntimeError(
                self.status.get("error")
                or f"Calculation exited with code {code}; see calculation.log"
            )
        if self.importer is None:
            manifest_path = self.job.directory / "scene" / "manifest.json"
            if code is None and not manifest_path.is_file():
                return 0.25
            if code is None and not self.progressive:
                return 0.25
            if (
                code is not None
                and self.job.log_offset < (self.job.directory / "calculation.log").stat().st_size
            ):
                return 0.01
            self.importer = SceneImport(self.job.directory / "scene")
            self.scene_started = time.monotonic()
            print("[MetroSim26] Blender scene construction: importing bounded mesh chunks")
        self.status.update(
            phase="building_scene",
            warning=self.importer.warning or self.status.get("warning", ""),
            complete=False,
            worker_processes=self.status.get("worker_processes", 0),
        )
        settings = getattr(bpy.context.scene, "metrosim26_setup", None)
        if settings is not None and getattr(settings, "pause_reveal", False) is True:
            return 0.1
        try:
            next(self.importer.steps)
        except StopIteration:
            if code is None:
                return 0.1
            cfg, scene = self.importer.cfg, self.importer.scene
            assert self.scene_started is not None
            seconds = time.monotonic() - self.scene_started
            print(f"Blender scene construction: {seconds:.3f} s")
            if cfg.get("study_radius_km"):
                save_timing(cfg, "IMPORT", seconds)
            self.status.update(phase="finalizing", message="Finalizing optional output")
            self.publish(force=True)
            self.apply_palette(scene.metrosim26_setup, SimpleNamespace(scene=scene))
            # Optional native save/render operations remain explicit, synchronous
            # exports; the default interactive workflow does not enable them.
            finalize_blender_output(cfg, scene)
            self.status.update(
                phase="complete",
                message="Ready",
                complete=True,
                scene_progress=1.0,
                year=cfg["end_year"],
            )
            self.publish(force=True)
            self.active = False
            print(
                f"Ready: frame 1 = {timeline_start(cfg)}; frame {scene.frame_end} = {cfg['end_year']}."
            )
            if cfg.get("view_mode") == "MAP":
                print("3D map ready. No growth simulation was run.")
            else:
                print(f"Yearly states and summary: {Path(cfg['run_directory']) / 'simulation'}")
            # Blender owns the finished scene; do not retain large ID lists here.
            self.importer.owned.clear()
            return None
        self.status.update(
            message=self.importer.message,
            scene_progress=self.importer.done / max(1, self.importer.total),
        )
        self.publish()
        if self.importer.revealing:
            return 1 / 30
        if self.cfg.get("view_mode") == "MAP":
            return 0.001
        delay = getattr(settings, "reveal_delay", 0.05) if self.progressive else 0.01
        return float(delay) if isinstance(delay, (int, float)) else 0.01
