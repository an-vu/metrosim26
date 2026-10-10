"""Growth planning. No Blender dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from metrosim26 import workers
from metrosim26.growth.capacity import ranked_cells
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    make_site_plan,
)


def _worker_python(cfg):
    # Inspect an already-running Blender only; this module never imports bpy.
    bpy = sys.modules.get("bpy")
    explicit = cfg.get("worker_python")
    if explicit:
        candidates = [Path(explicit)]
    elif bpy is None:
        candidates = [Path(sys.executable)]
    else:
        filename = "python.exe" if os.name == "nt" else "python3"
        candidates = [
            Path(sys.prefix) / "bin" / filename,
            Path(sys.prefix) / filename,
            Path(sys.base_prefix) / "bin" / filename,
        ]
        if os.name != "nt":
            candidates += sorted((Path(sys.prefix) / "bin").glob("python3.*"))
        # Never start blender.exe as a Python multiprocessing interpreter.
        candidates.append(
            Path(bpy.app.binary_path).parent
            / f"{bpy.app.version[0]}.{bpy.app.version[1]}"
            / "python"
            / "bin"
            / filename
        )
    for candidate in candidates:
        if candidate.is_file():
            # Keep a venv's executable path: resolving its symlink loses the venv.
            return str(candidate.absolute())
    raise RuntimeError(
        "No standalone Python found; set WORKER_PYTHON to a Python with matching NumPy."
    )


class _PlanningCache(dict):
    """Bounded speculative planning. Results never choose or commit a city action."""

    def __init__(self, cfg, grid, baseline):
        super().__init__()
        self.cfg, self.grid, self.baseline = cfg, grid, baseline
        self.workers = max(1, min(int(cfg.get("cpu_workers", 16)), os.cpu_count() or 1))
        self.enabled = bool(cfg.get("enable_parallel_compute", True)) and self.workers > 1
        self.profile = bool(cfg.get("profile_performance", True))
        self.process: subprocess.Popen[str] | None = None
        self.directory = None
        self.error_file = None
        self.started = time.perf_counter()
        self.batch_size = max(1, int(cfg.get("parallel_batch_size", 32)))
        self.counts = {"cache_hits": 0, "serial_plans": 0, "parallel_plans": 0, "batches": 0}
        self.recent_costs = []
        self.seconds = {"planning": 0.0, "ranking": 0.0, "grid": 0.0, "checkpoint": 0.0}
        if self.profile:
            print("\n================================================\nMETROSIM26 PERFORMANCE")
            print(f"Logical CPUs detected: {os.cpu_count()}")
            print(f"Parallel compute: {'ON (lazy startup)' if self.enabled else 'OFF — serial'}")
            print(f"Worker processes: {self.workers if self.enabled else 0}")
            print("================================================")

    def __enter__(self):
        return self

    def _kill_tree(self):
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            import signal

            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait()

    def _receive(self):
        assert self.process is not None and self.process.stdout is not None
        # Reader does no Blender work and is always joined before leaving this call.
        with ThreadPoolExecutor(max_workers=1) as reader:
            future = reader.submit(self.process.stdout.readline)
            try:
                line = future.result(timeout=float(self.cfg.get("worker_timeout", 120)))
            except BaseException:
                self._kill_tree()
                raise
        if not line:
            raise RuntimeError(
                "Worker broker exited; see worker stderr in the reported fallback/error."
            )
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError("Worker calculation failed:\n" + reply["error"])
        return reply

    def _start(self, tasks):
        self.directory = tempfile.TemporaryDirectory(prefix="metrosim26_workers_")
        executable = _worker_python(self.cfg)
        workers.write_worker_state(self.directory.name, self.cfg, self.grid, self.baseline)
        self.error_file = open(Path(self.directory.name) / "stderr.log", "w+", encoding="utf8")
        env = dict(os.environ)
        for name in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            env[name] = "1"
        self.process = subprocess.Popen(
            [
                executable,
                "-m",
                "metrosim26.workers",
                "--broker",
                self.directory.name,
                str(min(self.workers, len(tasks))),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.error_file,
            text=True,
            encoding="utf8",
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        report_progress("simulation", "Starting planning broker", broker_pid=self.process.pid)
        reply = self._receive()
        if not reply.get("ready") or reply.get("numpy") != np.__version__:
            raise RuntimeError("Worker NumPy version must match Blender NumPy " + np.__version__)
        report_progress("simulation", "Planning workers ready", worker_processes=reply["workers"])
        if self.profile:
            print(
                f"Worker pool ready: {reply['workers']} spawned processes; read-only mapped geometry."
            )

    def _close(self):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    # communicate drains pending output too, including on cancellation.
                    self.process.communicate(input='{"stop":true}\n', timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                self._kill_tree()
            finally:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream:
                        stream.close()
                self.process = None
                report_progress(
                    "simulation", "Planning worker pool closed", worker_processes=0, broker_pid=None
                )
        if self.error_file is not None:
            self.error_file.close()
            self.error_file = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def __exit__(self, kind, value, trace):
        self._close()
        if self.profile and kind is None:
            if self.enabled and not self.counts["parallel_plans"]:
                print(
                    "Pool not started: available batches were too small/cheap to justify startup."
                )
            elapsed = time.perf_counter() - self.started
            years = self.cfg["end_year"] - self.cfg["base_year"] + 1
            print(f"Simulation total (including worker startup/shutdown): {elapsed:.3f} s")
            print(f"Average/saved year: {elapsed / years:.3f} s | {self.counts}")

    def template(self, gx, gy, arch, epoch, year):
        key = (int(gx), int(gy), int(arch), int(epoch))
        if key in self:
            self.counts["cache_hits"] += 1
            return self[key]
        started = time.perf_counter()
        value = make_site_plan(self.cfg, self.grid, self.baseline, gx, gy, arch, epoch, year)
        cost = time.perf_counter() - started
        self.seconds["planning"] += cost
        self.recent_costs = (self.recent_costs + [cost])[-16:]
        self.counts["serial_plans"] += 1
        self[key] = value
        return value

    def prefetch(self, tasks):
        if not self.enabled:
            return
        missing = {tuple(task[:4]): task for task in tasks if tuple(task[:4]) not in self}
        tasks = list(missing.values())
        if len(tasks) < int(self.cfg.get("parallel_min_tasks", 8)):
            return  # Tiny batches stay serial; don't pay process/IPC startup for them.
        threshold = float(self.cfg.get("parallel_min_batch_seconds", 0.20))
        if self.process is None and threshold > 0:
            if not self.recent_costs:
                for task in tasks[:2]:
                    self.template(*task)
                tasks = [task for task in tasks if tuple(task[:4]) not in self]
            estimate = sum(self.recent_costs) / len(self.recent_costs) * len(tasks)
            if not tasks or estimate < threshold:
                return
        started = time.perf_counter()
        if self.process is None:
            try:
                self._start(tasks)
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                detail = ""
                if self.error_file is not None:
                    self.error_file.flush()
                    self.error_file.seek(0)
                    detail = self.error_file.read()[-1500:]
                self._close()
                self.enabled = False
                print(f"[MetroSim26] Multiprocessing unavailable; SERIAL FALLBACK: {exc}\n{detail}")
                self.seconds["planning"] += time.perf_counter() - started
                return
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps({"tasks": tasks}) + "\n")
        self.process.stdin.flush()
        values = self._receive()["results"]
        if len(values) != len(tasks):
            raise RuntimeError("Incomplete worker batch; no results were committed.")
        for task, plan in zip(tasks, values):
            self[tuple(task[:4])] = plan
        self.counts["parallel_plans"] += len(tasks)
        self.counts["batches"] += 1
        self.seconds["planning"] += time.perf_counter() - started

    def candidates(self, indexes, task_for_cell):
        for i, flat in enumerate(indexes):
            if i % self.batch_size == 0 and self.enabled:
                tasks = []
                for candidate in indexes[i : i + self.batch_size]:
                    tasks.extend(task_for_cell(candidate))
                self.prefetch(tasks)
            yield flat

    def ranked(self, mask, score, limit):
        started = time.perf_counter()
        result = ranked_cells(mask, score, limit)
        self.seconds["ranking"] += time.perf_counter() - started
        return result

    def year_start(self):
        self.year_started = time.perf_counter()
        self.seconds = dict.fromkeys(self.seconds, 0.0)

    def year_end(self, year):
        if self.profile:
            total = time.perf_counter() - self.year_started
            serial = max(0.0, total - sum(self.seconds.values()))
            print(
                f"Year {year} timings: grid={self.seconds['grid']:.3f}s, "
                f"ranking={self.seconds['ranking']:.3f}s, planning/IPC={self.seconds['planning']:.3f}s, "
                f"serial decisions/commit={serial:.3f}s, checkpoint={self.seconds['checkpoint']:.3f}s, "
                f"total={total:.3f}s"
            )
