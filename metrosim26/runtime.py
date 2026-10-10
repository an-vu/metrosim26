#!/usr/bin/env python3
"""External calculation jobs and atomic progress. This module never imports Blender."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

_REPORTER = None


def report_progress(phase, message, **fields):
    if _REPORTER is not None:
        _REPORTER.update(phase=phase, message=message, **fields)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers/scanners can briefly hold the destination open.
                if attempt == 4:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


class Progress:
    def __init__(self, path, cfg):
        self.path = Path(path)
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state: dict[str, Any] = dict(
            phase="starting",
            message="Starting calculation",
            year=None,
            start_year=cfg["base_year"],
            end_year=cfg["end_year"],
            worker_processes=0,
            complete=False,
            error=None,
        )
        self.update()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)
            self.state["elapsed_seconds"] = time.monotonic() - self.started
            self.state["heartbeat_utc"] = time.time()
            write_json(self.path, self.state)

    def _heartbeat(self):
        while not self.stop.wait(1):
            self.update()

    def close(self):
        self.stop.set()
        self.thread.join()


class CalculationJob:
    """Nonblocking child lifecycle; safe to poll from a Blender timer."""

    def __init__(self, cfg, executable):
        from metrosim26.config import run_directory

        self.directory = run_directory(cfg) / "jobs" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        (self.directory / "tmp").mkdir()
        write_json(self.directory / "config.json", dict(cfg, data_mode="OFFLINE"))
        self.status: dict[str, Any] = dict(
            phase="starting",
            message="Starting external calculation",
            year=None,
            worker_processes=0,
            elapsed_seconds=0,
            start_year=cfg["base_year"],
            end_year=cfg["end_year"],
            complete=False,
            error=None,
        )
        self.cancelled = False
        self.killer = None
        self.log_offset = 0
        self.pending_log = b""
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(self.directory / "tmp")
        # A disk log cannot fill a pipe and deadlock the calculation process.
        with (self.directory / "calculation.log").open("wb") as log:
            self.process = subprocess.Popen(
                [executable, "-u", "-m", "metrosim26.runtime", str(self.directory)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )

    def poll(self):
        code = self.process.poll()
        try:
            self.status = json.loads((self.directory / "progress.json").read_text(encoding="utf8"))
        except (OSError, ValueError):
            pass
        return code

    def read_log(self):
        with (self.directory / "calculation.log").open("rb") as stream:
            stream.seek(self.log_offset)
            block = stream.read(16384)
            self.log_offset = stream.tell()
        self.pending_log += block
        lines = self.pending_log.split(b"\n")
        self.pending_log = lines.pop()
        if (
            self.process.poll() is not None
            and self.log_offset == (self.directory / "calculation.log").stat().st_size
        ):
            lines.append(self.pending_log)
            self.pending_log = b""
        return b"\n".join(lines).decode("utf8", errors="replace")

    def cancel(self):
        self.cancelled = True
        self.poll()
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            if self.process.poll() is None and self.killer is None:
                self.killer = subprocess.Popen(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        else:
            # The existing broker deliberately creates its own process group.
            for pid in (self.status.get("broker_pid"), self.process.pid):
                if pid:
                    try:
                        os.killpg(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def cancellation_finished(self):
        return self.process.poll() is not None and (
            self.killer is None or self.killer.poll() is not None
        )


def run_job(directory):
    global _REPORTER
    from metrosim26.config import run_directory
    from metrosim26.scene import export_scene
    from metrosim26.simulation import prepare_simulation

    directory = Path(directory).resolve()
    cfg = json.loads((directory / "config.json").read_text(encoding="utf8"))
    cfg["data_mode"] = "OFFLINE"
    job_started = time.monotonic()
    reporter = Progress(directory / "progress.json", cfg)
    _REPORTER = reporter
    try:
        if cfg.get("job_action") == "PREFETCH":
            from metrosim26.data import expected_inputs, prefetch_data

            cfg["missing_inputs"] = sum(not item.path.is_file() for item in expected_inputs(cfg))
            report_progress("downloading", "Checking and downloading local inputs")
            if not prefetch_data(cfg):
                raise RuntimeError(
                    cfg.get("download_failure_summary", "Some downloads failed.")
                    + " Retry Download Area; completed files are kept."
                )
            reporter.update(
                phase="cache_ready", message="Data ready. Generate City next.", complete=True
            )
            return 0
        streaming = bool(
            cfg.get("progressive_build") and cfg.get("historical_mode", "DISABLED") == "DISABLED"
        )

        def baseline_ready(grid, baseline, result_directory):
            export_scene(
                cfg,
                grid,
                baseline,
                {"versions": [], "run_directory": result_directory},
                directory / "scene",
                complete=False,
            )

        if cfg.get("view_mode") == "MAP":
            from metrosim26.data import build_baseline
            from metrosim26.workers import Grid

            grid = Grid(cfg["bounds"], cfg["cell_km"])
            baseline = build_baseline(cfg, grid)
            result = {"versions": [], "run_directory": str(directory / "result")}
            if not baseline["buildings"]:
                reporter.update(
                    warning="No buildings mapped by OpenStreetMap here. Showing available roads, water and parks."
                )
        else:
            grid, baseline, result = prepare_simulation(
                cfg,
                destination=directory / "result",
                baseline_ready=baseline_ready if streaming else None,
            )

        # Publish only a completed simulation with recorded checksums. Previous complete
        # runs are never overwritten; cancellation leaves this pointer untouched.
        canonical = run_directory(cfg)
        result_directory = Path(result["run_directory"]).resolve()
        if cfg.get("view_mode") != "MAP" and result_directory.is_relative_to(canonical.resolve()):
            write_json(
                canonical / "latest_completed.json",
                {"directory": str(result_directory.relative_to(canonical.resolve()))},
            )
        report_progress(
            "preparing_scene",
            "Generating outer-ring mesh chunks"
            if cfg.get("expand_inner_radius_km")
            else "Preparing mesh chunks outside Blender",
            worker_processes=0,
            broker_pid=None,
            year=None,
            result_directory=str(result_directory),
        )
        export_scene(
            cfg,
            grid,
            baseline,
            result,
            directory / "scene",
            append=(directory / "scene" / "manifest.json").is_file(),
        )
        reporter.update(
            phase="scene_ready",
            message="Calculation and mesh preparation complete",
            complete=True,
            scene_directory=str(directory / "scene"),
        )
        return 0
    except BaseException as exc:
        error = traceback.format_exc()
        print(error, flush=True)
        reporter.update(
            phase="error",
            message=str(exc) or "Calculation failed; see calculation.log",
            error=error,
            complete=False,
            worker_processes=0,
        )
        return 1
    finally:
        if cfg.get("study_radius_km") and reporter.state.get("phase") in {
            "scene_ready",
            "cache_ready",
        }:
            save_timing(
                cfg,
                cfg.get("job_action", "MAP" if cfg.get("view_mode") == "MAP" else cfg["run_mode"]),
                time.monotonic() - job_started,
            )
        reporter.close()
        _REPORTER = None


def save_timing(cfg, action, seconds):
    path = Path(cfg["output_dir"]) / "area_timings.json"
    try:
        samples = json.loads(path.read_text())
        if not isinstance(samples, list):
            samples = []
    except (OSError, ValueError):
        samples = []
    samples.append(
        dict(
            action=action,
            view_mode=cfg.get("view_mode", "GROWTH"),
            missing_inputs=cfg.get("missing_inputs", 0),
            radius_km=cfg["study_radius_km"],
            center=cfg["study_center"],
            years=cfg["end_year"] - cfg["base_year"] + 1,
            seconds=seconds,
        )
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, samples[-30:])
    except OSError as error:
        print(f"Could not save timing sample: {error}", flush=True)


if __name__ == "__main__":
    # Data/simulation imports must see the same reporter as this script entry point.
    sys.modules["metrosim26.runtime"] = sys.modules[__name__]
    raise SystemExit(run_job(sys.argv[1]))
