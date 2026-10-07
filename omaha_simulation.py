"""Deterministic simulation, process-broker integration, and saved-run orchestration."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import omaha_workers
from omaha_config import (
    COMMERCIAL,
    EMPTY,
    ENGINE_SCHEMA_VERSION,
    HIGHRISE,
    INDUSTRIAL,
    MIXED,
    MODEL_VERSION,
    OFFICE,
    RETAIL,
    STATE_SCHEMA_VERSION,
    SUBURBAN,
    URBAN_RES,
    run_directory,
    simulation_config,
    validate_config,
)
from omaha_data import (
    atomic_json,
    atomic_npz,
    build_baseline,
    load_baseline,
    save_baseline,
)
from omaha_runtime import report_progress
from omaha_workers import (
    Grid,
    make_site_plan,
    polygon_area,
    site_id,
)


def estimate_baseline_capacity(cfg, grid, baseline):
    """OSM floor-space is a MODEL estimate of homes; RAC is never population."""
    housing_capacity = 0.0
    floor_jobs = 0.0
    for building in sorted(baseline["buildings"], key=lambda item: str(item["id"])):
        arch = int(building.get("arch", EMPTY))
        floors = max(1.0, building.get("height_m", 6.0) / 3.2)
        for polygon in building["polygons"]:
            area = max(
                0.0,
                abs(polygon_area(polygon["outer"]))
                - sum(abs(polygon_area(hole)) for hole in polygon.get("holes", [])),
            )
            share = (
                1.0 if arch == SUBURBAN else cfg["housing_share_by_archetype"].get(str(arch), 0.0)
            )
            tags = building.get("tags", {})
            if tags.get("building") in {
                "house",
                "detached",
                "semidetached_house",
                "terrace",
                "residential",
                "bungalow",
                "apartments",
                "dormitory",
            }:
                share = 1.0
            elif (
                tags.get("office")
                or tags.get("shop")
                or tags.get("building")
                in {
                    "office",
                    "commercial",
                    "retail",
                    "industrial",
                    "warehouse",
                    "school",
                    "university",
                    "hospital",
                    "church",
                    "garage",
                    "garages",
                    "shed",
                }
            ):
                share = 0.0
            floor_area = area * 1_000_000.0 * floors * cfg.get("usable_floor_area_ratio", 0.80)
            housing_capacity += floor_area * share / cfg.get("floor_area_per_home_m2", 95.0)
            floor_jobs += floor_area * (1.0 - share) / cfg.get("office_area_per_job_m2", 24.0)
    h_vacancy = float(cfg.get("initial_housing_vacancy", 0.04))
    j_vacancy = float(cfg.get("initial_job_vacancy", 0.04))
    if not (0.0 <= h_vacancy < 1.0 and 0.0 <= j_vacancy < 1.0):
        raise ValueError("Initial vacancy assumptions must be in [0, 1).")
    requested_housing = cfg.get("initial_households")
    requested_jobs = cfg.get("initial_jobs")
    households = (
        float(requested_housing)
        if requested_housing is not None
        else housing_capacity * (1.0 - h_vacancy)
    )
    wac_jobs = float(np.sum(baseline["jobs"], dtype=np.float64))
    jobs = float(requested_jobs) if requested_jobs is not None else wac_jobs
    # Missing workplace records do not silently become a population estimate.
    # The explicit fallback is employment floor space, labeled in metadata.
    job_source = (
        "configured scenario baseline"
        if requested_jobs is not None
        else "LODES WAC workplace employment"
    )
    if requested_jobs is None and jobs <= 0.0:
        jobs = floor_jobs * (1.0 - j_vacancy)
        job_source = "MODEL fallback: estimated OSM nonresidential floor-space capacity"
    if households < 0.0 or jobs < 0.0:
        raise ValueError("Scenario household/job baselines cannot be negative.")
    return {
        "households": households,
        "jobs": jobs,
        "housing_capacity": households / (1.0 - h_vacancy),
        "job_capacity": jobs / (1.0 - j_vacancy),
        "housing_baseline_source": (
            "configured scenario baseline"
            if requested_housing is not None
            else "MODEL: OSM residential floor space / assumed area per home, less assumed vacancy"
        ),
        "job_baseline_source": job_source,
        "rac_use": "Spatial distribution signal only: jobs associated with workers' residence locations; not population.",
    }


def neighbor_fraction(urban):
    """Eight-neighbor occupancy; vectorized, with outside-study cells undeveloped."""
    padded = np.pad(urban.astype(np.float32), 1)
    result = np.zeros(urban.shape, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            if dx != 1 or dy != 1:
                result += padded[dy : dy + urban.shape[0], dx : dx + urban.shape[1]]
    return result / 8.0


def highrise_allowed(cfg, baseline, gx, gy, neighbors):
    return bool(
        baseline["centrality"][gy, gx] >= cfg.get("highrise_min_centrality", 0.70)
        and baseline["job_signal"][gy, gx] >= cfg.get("highrise_min_job_signal", 0.55)
        and neighbors[gy, gx] >= cfg.get("highrise_min_neighbor_fraction", 0.45)
    )


def choose_archetype(cfg, baseline, gx, gy, neighbors, housing_need, job_need):
    """Future land-use is an explicit scenario decision informed by real signals."""
    centrality = float(baseline["centrality"][gy, gx])
    housing_weight = housing_need / max(housing_need + job_need * 0.5, 1e-9)
    if highrise_allowed(cfg, baseline, gx, gy, neighbors):
        if (
            housing_need > cfg["highrise_min_housing_demand"]
            and job_need > cfg["highrise_min_job_demand"]
        ):
            return HIGHRISE
    if housing_weight > 0.65:
        return URBAN_RES if centrality > 0.45 or neighbors[gy, gx] > 0.60 else SUBURBAN
    if housing_weight > 0.22 and centrality > 0.35:
        return MIXED
    preferences = [
        (float(baseline["industrial_signal"][gy, gx]) + (1.0 - centrality) * 0.20, INDUSTRIAL),
        (float(baseline["retail_signal"][gy, gx]) + 0.05, RETAIL),
        (
            max(
                float(baseline["office_signal"][gy, gx]),
                float(baseline["institution_signal"][gy, gx]),
            )
            + centrality * 0.15,
            OFFICE,
        ),
        (float(baseline["road_access"][gy, gx]) * 0.45, COMMERCIAL),
    ]
    # Stable numeric tie-breaking avoids dependence on dictionary iteration.
    return max(preferences, key=lambda pair: (pair[0], -pair[1]))[1]


def site_capacity(site):
    return (
        sum(item["housing"] for item in site["buildings"]),
        sum(item["jobs"] for item in site["buildings"]),
    )


def select_plan_buildings(plan, housing_need, job_need, limit):
    selected = []
    housing = jobs = 0.0
    for building in plan["buildings"]:
        if len(selected) >= limit:
            break
        helps_housing = housing_need > housing + 1e-8 and building["housing"] > 0.0
        helps_jobs = job_need > jobs + 1e-8 and building["jobs"] > 0.0
        if not (helps_housing or helps_jobs):
            continue
        selected.append(building)
        housing += building["housing"]
        jobs += building["jobs"]
    return selected


def create_site(
    cfg,
    grid,
    baseline,
    gx,
    gy,
    arch,
    year,
    housing_need,
    job_need,
    origin_type,
    old_site=None,
    redevelopment=False,
    plan_cache=None,
):
    """Construct a full active version. Infill preserves structures; replacement retires them."""
    if redevelopment:
        assert old_site is not None, "Redevelopment requires an existing site."
        epoch = old_site["layout_epoch"] + 1
    else:
        epoch = old_site["layout_epoch"] if old_site is not None else 0
    cache_key = (int(gx), int(gy), int(arch), int(epoch))
    if isinstance(plan_cache, _PlanningCache):
        template = plan_cache.template(gx, gy, arch, epoch, year)
    elif plan_cache is not None and cache_key in plan_cache:
        template = plan_cache[cache_key]
    else:
        template = make_site_plan(cfg, grid, baseline, gx, gy, arch, epoch, year)
        if plan_cache is not None:
            plan_cache[cache_key] = template
    # Cached templates describe fixed parcels. Only their construction year changes.
    plan = {
        "buildings": [dict(item, construction_year=int(year)) for item in template["buildings"]],
        "roads": [dict(item, construction_year=int(year)) for item in template["roads"]],
        "parking": {
            key: dict(item, construction_year=int(year))
            for key, item in template["parking"].items()
        },
    }
    plan_building_count = len(plan["buildings"])
    previous_buildings = [] if old_site is None or redevelopment else old_site["buildings"]
    previous_ids = {item["id"] for item in previous_buildings}
    plan["buildings"] = [item for item in plan["buildings"] if item["id"] not in previous_ids]
    if previous_buildings:
        limit = max(1, int(math.ceil(len(plan["buildings"]) * cfg.get("infill_fraction", 0.35))))
    elif redevelopment:
        limit = len(plan["buildings"])
    else:
        limit = max(
            1, int(math.ceil(len(plan["buildings"]) * cfg.get("initial_parcel_fill", 0.65)))
        )
    old_housing, old_jobs = site_capacity(old_site) if old_site else (0.0, 0.0)
    selected = select_plan_buildings(
        plan,
        housing_need + (old_housing if redevelopment else 0.0),
        job_need + (old_jobs if redevelopment else 0.0),
        limit,
    )
    if not selected:
        return None
    buildings = list(previous_buildings) + selected
    if redevelopment:
        housing = sum(item["housing"] for item in buildings)
        jobs = sum(item["jobs"] for item in buildings)
        # Conservative rule: replacement preserves all old capacity, even vacant space.
        if housing + 1e-8 < old_housing or jobs + 1e-8 < old_jobs:
            return None
        if not (
            (housing_need > 1e-8 and housing > old_housing + 1e-8)
            or (job_need > 1e-8 and jobs > old_jobs + 1e-8)
        ):
            return None
    if old_site is not None and not redevelopment:
        surfaces = list(old_site["surfaces"])
    else:
        surfaces = list(plan["roads"])
    surfaces.extend(
        plan["parking"][item["id"]] for item in selected if item["id"] in plan["parking"]
    )
    return {
        "id": site_id(gx, gy),
        "version": 0 if old_site is None else old_site["version"] + 1,
        "layout_epoch": epoch,
        "plan_building_count": plan_building_count,
        "gx": int(gx),
        "gy": int(gy),
        "arch": int(arch),
        "start_year": int(year),
        "end_year": None,
        "established_year": int(year) if old_site is None else old_site["established_year"],
        "last_redevelopment_year": int(year)
        if old_site is None or redevelopment
        else old_site["last_redevelopment_year"],
        "origin_type": origin_type,
        "lifecycle_type": "redevelopment"
        if redevelopment
        else "infill"
        if old_site or origin_type == "baseline_infill"
        else "greenfield",
        "buildings": buildings,
        "surfaces": surfaces,
    }


def ranked_cells(mask, score, limit):
    flat = np.flatnonzero(mask)
    if flat.size == 0:
        return []
    # lexsort's last key is primary; linear cell index is the deterministic tie-break.
    order = np.lexsort((flat, -score.ravel()[flat]))
    return flat[order[:limit]].tolist()


def redevelopment_archetypes(old_arch, can_highrise):
    options = {
        SUBURBAN: [URBAN_RES, MIXED],
        URBAN_RES: [MIXED],
        COMMERCIAL: [MIXED, OFFICE],
        RETAIL: [MIXED, OFFICE],
        OFFICE: [],
        MIXED: [],
        INDUSTRIAL: [],
        HIGHRISE: [],
    }.get(old_arch, [])
    if can_highrise and old_arch != INDUSTRIAL and old_arch != HIGHRISE:
        options = list(options) + [HIGHRISE]
    return options


def simulation_metrics(year, grid, baseline, urban, arch, actions, ledger):
    protected = baseline.get("protected_fraction")
    if protected is None:
        protected = np.clip(baseline["water_fraction"] + baseline["park_fraction"], 0.0, 1.0)
    widths = np.array(
        [grid.cell_bounds(gx, 0)[2] - grid.cell_bounds(gx, 0)[0] for gx in range(grid.nx)]
    )
    heights = np.array(
        [grid.cell_bounds(0, gy)[3] - grid.cell_bounds(0, gy)[1] for gy in range(grid.ny)]
    )
    metrics = {
        "year": int(year),
        "developed_area": float(
            np.sum(urban * (1.0 - protected) * heights[:, None] * widths[None, :])
        ),
        "new_greenfield_cells": int(actions["greenfield"]),
        "infill_sites": int(actions["infill"]),
        "redevelopment_sites": int(actions["redevelopment"]),
        "housing_capacity_added": float(actions["housing_net"]),
        "job_capacity_added": float(actions["jobs_net"]),
        "housing_capacity_built_gross": float(actions["housing_gross"]),
        "job_capacity_built_gross": float(actions["jobs_gross"]),
        "housing_capacity_demolished": float(actions["housing_demolished"]),
        "job_capacity_demolished": float(actions["jobs_demolished"]),
    }
    columns = {
        SUBURBAN: "suburban",
        URBAN_RES: "urban_residential",
        MIXED: "mixed",
        COMMERCIAL: "commercial",
        RETAIL: "retail",
        INDUSTRIAL: "industrial",
        OFFICE: "office",
        HIGHRISE: "highrise",
    }
    for code, name in columns.items():
        metrics[name + "_cells"] = int(np.count_nonzero((arch == code) & urban))
    metrics.update({key: float(value) for key, value in ledger.items()})
    return metrics


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
            print("\n================================================\nOMAHA PERFORMANCE")
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
        self.directory = tempfile.TemporaryDirectory(prefix="omaha_workers_")
        executable = _worker_python(self.cfg)
        omaha_workers.write_worker_state(self.directory.name, self.cfg, self.grid, self.baseline)
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
                str(Path(omaha_workers.__file__).resolve()),
                "--broker",
                self.directory.name,
                str(min(self.workers, len(tasks))),
            ],
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
                print(f"[Omaha] Multiprocessing unavailable; SERIAL FALLBACK: {exc}\n{detail}")
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


def run_simulation(cfg, grid, baseline, sim_dir):
    with _PlanningCache(cfg, grid, baseline) as cache:
        result = _run_simulation(cfg, grid, baseline, sim_dir, cache)
    report_progress(
        "simulation", "Simulation complete; finalizing results", worker_processes=0, broker_pid=None
    )
    if cfg.get("validate_parallel_results", False) and cache.counts["parallel_plans"]:
        # Debug-only: replay the calculation serially in temporary storage, then
        # compare exact array archives and authoritative JSON/CSV files.
        serial_cfg = dict(
            cfg,
            enable_parallel_compute=False,
            profile_performance=False,
            validate_parallel_results=False,
        )
        with tempfile.TemporaryDirectory(prefix="omaha_validation_") as tmp:
            run_simulation(serial_cfg, grid, baseline, tmp)
            for path in sorted(Path(tmp).iterdir()):
                if path.read_bytes() != (Path(sim_dir) / path.name).read_bytes():
                    raise AssertionError("Serial/parallel checkpoint mismatch: " + path.name)
        print("[Omaha] Serial/parallel validation: all authoritative files byte-identical.")
    elif cfg.get("validate_parallel_results", False):
        print("[Omaha] Validation skipped: no parallel plans were needed in this run.")
    return result


def _run_simulation(cfg, grid, baseline, sim_dir, plan_cache):
    """Compute/save annual states without bpy, timers, threads or wall-clock input."""
    sim_dir = Path(sim_dir)
    sim_dir.mkdir(parents=True, exist_ok=True)
    housing_rate = float(cfg.get("annual_household_growth_rate", 0.008))
    job_rate = float(cfg.get("annual_job_growth_rate", 0.010))
    if housing_rate < 0.0 or job_rate < 0.0:
        raise ValueError(
            "This growth-only model requires nonnegative annual scenario growth rates."
        )
    initial = estimate_baseline_capacity(cfg, grid, baseline)
    baseline.setdefault("metadata", {})["scenario_capacity_baseline"] = initial
    baseline["metadata"]["developed_area_metric"] = (
        "Approximate developed-cell envelope in km2, reduced by water/park fractions; not observed impervious area."
    )
    urban = baseline["urban"].copy()
    arch = baseline["arch"].copy()
    active = {}
    versions = []
    states = []
    summary = []
    households = initial["households"]
    jobs = initial["jobs"]
    free_housing = initial["housing_capacity"] - households
    free_jobs = initial["job_capacity"] - jobs
    unmet_housing = unmet_jobs = 0.0
    cumulative_housing_demand = cumulative_job_demand = 0.0
    cumulative_housing_served = cumulative_jobs_served = 0.0
    cumulative_housing_added = cumulative_jobs_added = 0.0
    candidate_limit = int(cfg.get("max_candidates_per_phase", 1200))
    action_limit = int(cfg.get("max_sites_per_year", 160 if cfg.get("preview_mode") else 600))
    protected_ok = (baseline["water_fraction"] < cfg.get("substantial_water_fraction", 0.45)) & (
        baseline["park_fraction"] < cfg.get("substantial_park_fraction", 0.75)
    )
    access_ok = baseline["road_access"] >= cfg.get("minimum_road_access", 0.10)
    for year in range(cfg["base_year"], cfg["end_year"] + 1):
        report_progress("simulation", f"Simulating {year} / {cfg['end_year']}", year=year)
        plan_cache.year_start()
        grid_started = time.perf_counter()
        new_development = np.zeros(grid.shape, dtype=bool)
        redevelopment = np.zeros(grid.shape, dtype=bool)
        infill = np.zeros(grid.shape, dtype=bool)
        demolished = []
        actions = {
            "greenfield": 0,
            "infill": 0,
            "redevelopment": 0,
            "housing_net": 0.0,
            "jobs_net": 0.0,
            "housing_gross": 0.0,
            "jobs_gross": 0.0,
            "housing_demolished": 0.0,
            "jobs_demolished": 0.0,
        }
        annual_housing = annual_jobs = served_housing = served_jobs = 0.0
        neighbors = neighbor_fraction(urban)
        touched = set()
        if year > cfg["base_year"]:
            annual_housing = households * housing_rate
            annual_jobs = jobs * job_rate
            # Demand follows the exogenous scenario trajectory, including unmet demand.
            households += annual_housing
            jobs += annual_jobs
            unmet_housing += annual_housing
            unmet_jobs += annual_jobs
            cumulative_housing_demand += annual_housing
            cumulative_job_demand += annual_jobs
            take_h = min(unmet_housing, free_housing)
            take_j = min(unmet_jobs, free_jobs)
            unmet_housing -= take_h
            unmet_jobs -= take_j
            free_housing -= take_h
            free_jobs -= take_j
            served_housing += take_h
            served_jobs += take_j

            def needs_capacity():
                return unmet_housing > 1e-8 or unmet_jobs > 1e-8

            def install(site, previous, phase):
                nonlocal \
                    free_housing, \
                    free_jobs, \
                    unmet_housing, \
                    unmet_jobs, \
                    served_housing, \
                    served_jobs
                old_h, old_j = site_capacity(previous) if previous is not None else (0.0, 0.0)
                new_h, new_j = site_capacity(site)
                delta_h, delta_j = new_h - old_h, new_j - old_j
                if previous is not None:
                    previous["end_year"] = int(year)
                if phase == "redevelopment":
                    assert previous is not None, "Redevelopment requires an existing site."
                    demolished.extend(item["id"] for item in previous["buildings"])
                    actions["housing_demolished"] += old_h
                    actions["jobs_demolished"] += old_j
                    actions["housing_gross"] += new_h
                    actions["jobs_gross"] += new_j
                    redevelopment[site["gy"], site["gx"]] = True
                else:
                    actions["housing_gross"] += delta_h
                    actions["jobs_gross"] += delta_j
                    (new_development if phase == "greenfield" else infill)[
                        site["gy"], site["gx"]
                    ] = True
                actions[phase] += 1
                actions["housing_net"] += delta_h
                actions["jobs_net"] += delta_j
                free_housing += delta_h
                free_jobs += delta_j
                take_h = min(unmet_housing, free_housing)
                take_j = min(unmet_jobs, free_jobs)
                unmet_housing -= take_h
                unmet_jobs -= take_j
                free_housing -= take_h
                free_jobs -= take_j
                served_housing += take_h
                served_jobs += take_j
                versions.append(site)
                active[site["id"]] = site
                touched.add(site["gy"] * grid.nx + site["gx"])
                urban[site["gy"], site["gx"]] = True
                arch[site["gy"], site["gx"]] = site["arch"]

            # Infill first: existing generated vacant parcels, then protected OSM neighborhoods.
            score = (
                baseline["road_access"] * 0.30
                + neighbors * 0.30
                + baseline["centrality"] * 0.20
                + baseline["housing_signal"] * (0.20 if unmet_housing > unmet_jobs else 0.05)
                + baseline["job_signal"] * (0.20 if unmet_jobs >= unmet_housing else 0.05)
            )
            generated_mask = np.zeros(grid.shape, dtype=bool)
            for site in active.values():
                generated_mask[site["gy"], site["gx"]] = True
            infill_score = score + generated_mask * 2.0
            infill_mask = urban & protected_ok & access_ok
            for site in active.values():
                if len(site["buildings"]) >= site["plan_building_count"]:
                    infill_mask[site["gy"], site["gx"]] = False
            plan_cache.seconds["grid"] += time.perf_counter() - grid_started
            candidates = plan_cache.ranked(infill_mask, infill_score, candidate_limit)

            def infill_tasks(flat):
                if not needs_capacity() or len(touched) >= action_limit:
                    return []
                gy, gx = divmod(flat, grid.nx)
                old = active.get(site_id(gx, gy))
                choice = (
                    old["arch"]
                    if old
                    else choose_archetype(
                        cfg, baseline, gx, gy, neighbors, unmet_housing, unmet_jobs
                    )
                )
                return [(gx, gy, choice, old["layout_epoch"] if old else 0, year)]

            for flat in plan_cache.candidates(candidates, infill_tasks):
                if not needs_capacity() or len(touched) >= action_limit:
                    break
                gy, gx = divmod(flat, grid.nx)
                previous = active.get(site_id(gx, gy))
                choice = (
                    previous["arch"]
                    if previous is not None
                    else choose_archetype(
                        cfg, baseline, gx, gy, neighbors, unmet_housing, unmet_jobs
                    )
                )
                site = create_site(
                    cfg,
                    grid,
                    baseline,
                    gx,
                    gy,
                    choice,
                    year,
                    unmet_housing,
                    unmet_jobs,
                    previous["origin_type"] if previous else "baseline_infill",
                    previous,
                    plan_cache=plan_cache,
                )
                if site is not None:
                    install(site, previous, "infill")

            # Replace only generated sites, after infill has had an opportunity.
            eligible = np.zeros(grid.shape, dtype=bool)
            for previous in active.values():
                flat = previous["gy"] * grid.nx + previous["gx"]
                if flat not in touched and year - previous["last_redevelopment_year"] >= cfg.get(
                    "min_redevelopment_age", 12
                ):
                    eligible[previous["gy"], previous["gx"]] = True

            def redevelopment_tasks(flat):
                if not needs_capacity() or len(touched) >= action_limit:
                    return []
                gy, gx = divmod(flat, grid.nx)
                old = active[site_id(gx, gy)]
                choices = redevelopment_archetypes(
                    old["arch"], highrise_allowed(cfg, baseline, gx, gy, neighbors)
                )
                return [(gx, gy, choice, old["layout_epoch"] + 1, year) for choice in choices]

            candidates = plan_cache.ranked(
                eligible & protected_ok & access_ok, score, candidate_limit
            )
            for flat in plan_cache.candidates(candidates, redevelopment_tasks):
                if not needs_capacity() or len(touched) >= action_limit:
                    break
                gy, gx = divmod(flat, grid.nx)
                previous = active[site_id(gx, gy)]
                choices = redevelopment_archetypes(
                    previous["arch"], highrise_allowed(cfg, baseline, gx, gy, neighbors)
                )
                for choice in choices:
                    site = create_site(
                        cfg,
                        grid,
                        baseline,
                        gx,
                        gy,
                        choice,
                        year,
                        unmet_housing,
                        unmet_jobs,
                        previous["origin_type"],
                        previous,
                        redevelopment=True,
                        plan_cache=plan_cache,
                    )
                    if site is not None:
                        install(site, previous, "redevelopment")
                        break

            # Outward growth remains contiguous with the developed city and road-served.
            outward_mask = (~urban) & protected_ok & access_ok & (neighbors >= 0.125)

            def outward_tasks(flat):
                if not needs_capacity() or len(touched) >= action_limit:
                    return []
                gy, gx = divmod(flat, grid.nx)
                choice = choose_archetype(
                    cfg, baseline, gx, gy, neighbors, unmet_housing, unmet_jobs
                )
                return [(gx, gy, choice, 0, year)]

            candidates = plan_cache.ranked(outward_mask, score, candidate_limit)
            for flat in plan_cache.candidates(candidates, outward_tasks):
                if not needs_capacity() or len(touched) >= action_limit:
                    break
                gy, gx = divmod(flat, grid.nx)
                choice = choose_archetype(
                    cfg, baseline, gx, gy, neighbors, unmet_housing, unmet_jobs
                )
                site = create_site(
                    cfg,
                    grid,
                    baseline,
                    gx,
                    gy,
                    choice,
                    year,
                    unmet_housing,
                    unmet_jobs,
                    "greenfield",
                    plan_cache=plan_cache,
                )
                if site is not None:
                    install(site, None, "greenfield")
        cumulative_housing_served += served_housing
        cumulative_jobs_served += served_jobs
        cumulative_housing_added += actions["housing_net"]
        cumulative_jobs_added += actions["jobs_net"]
        ledger = {
            "housing_demand": annual_housing,
            "job_demand": annual_jobs,
            "housing_demand_served": served_housing,
            "job_demand_served": served_jobs,
            "unmet_housing_demand": unmet_housing,
            "unmet_job_demand": unmet_jobs,
            "unused_housing_capacity": free_housing,
            "unused_job_capacity": free_jobs,
            "cumulative_housing_demand": cumulative_housing_demand,
            "cumulative_job_demand": cumulative_job_demand,
            "cumulative_housing_demand_served": cumulative_housing_served,
            "cumulative_job_demand_served": cumulative_jobs_served,
            "total_housing_capacity": initial["housing_capacity"] + cumulative_housing_added,
            "total_job_capacity": initial["job_capacity"] + cumulative_jobs_added,
            "scenario_households": households,
            "scenario_jobs": jobs,
        }
        metrics = simulation_metrics(year, grid, baseline, urban, arch, actions, ledger)
        state = {
            "year": year,
            "urban": urban.copy(),
            "arch": arch.copy(),
            "new_development": new_development,
            "redevelopment": redevelopment,
            "infill": infill,
            "demolished": sorted(demolished),
            "sites": [active[key] for key in sorted(active)],
            "metrics": metrics,
        }
        checkpoint_started = time.perf_counter()
        save_year_state(sim_dir, state)
        states.append(state)
        summary.append(metrics)
        # Complete-year checkpoint retained for inspection and future restart tools.
        # Normal replay validates a complete run before reading these files.
        atomic_json(
            sim_dir / "versions.json",
            {
                "schema_version": ENGINE_SCHEMA_VERSION,
                "completed_year": year,
                "versions": versions,
                "baseline_capacity": initial,
            },
            compact=True,
        )
        save_summary_csv(sim_dir / "summary.csv", summary)
        plan_cache.seconds["checkpoint"] += time.perf_counter() - checkpoint_started
        plan_cache.year_end(year)
        print(
            f"Year {year}: +{actions['greenfield']} greenfield, {actions['infill']} infill, "
            f"{actions['redevelopment']} redevelopment; unmet homes={unmet_housing:.1f}, jobs={unmet_jobs:.1f}"
        )
    return {
        "states": states,
        "versions": versions,
        "summary": summary,
        "baseline_capacity": initial,
        "metadata": {
            "scenario_capacity_baseline": initial,
            "developed_area_metric": baseline["metadata"]["developed_area_metric"],
        },
    }


def save_year_state(sim_dir, state):
    payload = {key: state[key] for key in ("year", "demolished", "metrics")}
    payload["active_version_ids"] = [
        site["id"] + "@" + str(site["version"]) for site in state["sites"]
    ]
    atomic_npz(
        Path(sim_dir) / f"{state['year']}.npz",
        schema_version=np.asarray(ENGINE_SCHEMA_VERSION, dtype=np.int32),
        year=np.asarray(state["year"], dtype=np.int32),
        urban=state["urban"],
        arch=state["arch"],
        new_development=state["new_development"],
        redevelopment=state["redevelopment"],
        infill=state["infill"],
        payload_json=np.asarray(json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    )


def save_summary_csv(path, rows):
    if not rows:
        return
    path = Path(path)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def load_simulation(sim_dir, cfg, grid):
    sim_dir = Path(sim_dir)
    manifest = json.loads((sim_dir / "versions.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != ENGINE_SCHEMA_VERSION:
        raise ValueError("Saved simulation schema does not match this engine. Recompute the run.")
    if int(manifest["completed_year"]) < int(cfg["end_year"]):
        raise ValueError(
            "Saved run is incomplete for the requested end year; completed annual checkpoints remain available."
        )
    version_map = {site["id"] + "@" + str(site["version"]): site for site in manifest["versions"]}
    states = []
    for year in range(cfg["base_year"], cfg["end_year"] + 1):
        with np.load(sim_dir / f"{year}.npz", allow_pickle=False) as saved:
            if int(saved["schema_version"]) != ENGINE_SCHEMA_VERSION or int(saved["year"]) != year:
                raise ValueError(f"Invalid saved state for {year}.")
            state = json.loads(str(saved["payload_json"].item()))
            state["sites"] = [version_map[key] for key in state.pop("active_version_ids")]
            for key in ("urban", "arch", "new_development", "redevelopment", "infill"):
                state[key] = saved[key].copy()
                if state[key].shape != grid.shape:
                    raise ValueError(f"Saved {year} grid shape differs from requested study grid.")
            states.append(state)
    return {
        "states": states,
        "versions": manifest["versions"],
        "summary": [state["metrics"] for state in states],
        "baseline_capacity": manifest["baseline_capacity"],
        "metadata": {
            "scenario_capacity_baseline": manifest["baseline_capacity"],
            "developed_area_metric": "Approximate developed-cell envelope in km2, reduced by water/park fractions; not observed impervious area.",
        },
    }


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_saved_run(sim_dir, cfg):
    """Fail clearly on incompatible/incomplete checkpoints; never silently mix runs."""
    metadata_path = Path(sim_dir) / "metadata.json"
    if not metadata_path.exists():
        return None
    with metadata_path.open(encoding="utf8") as source:
        metadata = json.load(source)
    if not metadata.get("complete"):
        return None
    if simulation_config(metadata.get("simulation_config", {})) != simulation_config(cfg):
        raise ValueError("Saved simulation configuration differs; use RUN_MODE='SIMULATE'.")
    expected = {"baseline.npz", "versions.json", "summary.csv"}
    expected.update(f"{year}.npz" for year in range(cfg["base_year"], cfg["end_year"] + 1))
    manifest = metadata.get("files", {})
    if not expected.issubset(manifest):
        raise ValueError("Saved run has an incomplete file manifest; recalculate it.")
    for name in sorted(expected):
        path = Path(sim_dir) / name
        if not path.is_file() or file_sha256(path) != manifest[name]:
            raise ValueError(f"Saved simulation file missing or changed: {name}. Recalculate it.")
    return metadata


def _saved_run_directory(cfg, directory):
    """Find pre-split runs whose identity included transport-only settings.

    Never guess between multiple matching historical runs; their source bytes
    could differ. New runs always use the canonical transport-independent path.
    """
    pointer = directory / "latest_completed.json"
    if pointer.is_file():
        candidate = (
            directory / json.loads(pointer.read_text(encoding="utf8"))["directory"]
        ).resolve()
        if not candidate.is_relative_to(directory.resolve()):
            raise ValueError("Completed-run pointer is outside its scenario directory.")
        if validate_saved_run(candidate / "simulation", cfg) is not None:
            return candidate
        raise ValueError("Completed-run pointer refers to an incomplete run.")
    if (directory / "simulation" / "metadata.json").is_file():
        return directory
    label = "preview" if cfg["preview_mode"] else "full"
    matches = []
    for candidate in sorted(Path(cfg["output_dir"]).glob(f"{label}_{cfg['seed']}_*")):
        try:
            metadata = json.loads(
                (candidate / "simulation" / "metadata.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if (
            isinstance(metadata, dict)
            and metadata.get("complete")
            and simulation_config(metadata.get("simulation_config", {})) == simulation_config(cfg)
        ):
            matches.append(candidate)
    if len(matches) > 1:
        raise ValueError(
            "Multiple matching legacy saved runs; cannot choose source data implicitly: "
            + ", ".join(map(str, matches))
            + ". Use SIMULATE to create the canonical run."
        )
    return matches[0] if matches else directory


def prepare_simulation(cfg, *, destination=None):
    """Pure calculation orchestration; also usable from a standalone NumPy process."""
    validate_config(cfg)
    preparation_started = time.perf_counter()
    grid = Grid(tuple(cfg["bounds"]), cfg["cell_km"])
    directory = run_directory(cfg)
    mode = cfg["run_mode"].upper()
    if mode != "SIMULATE":
        report_progress("loading_saved", "Validating and locating saved results")
        directory = _saved_run_directory(cfg, directory)
    sim_dir = directory / "simulation"
    print(f"Omaha scenario | seed {cfg['seed']} | {cfg['base_year']}–{cfg['end_year']}")
    print(f"Study area {grid.width:.2f} × {grid.height:.2f} km; grid {grid.nx} × {grid.ny}")
    print(f"Output: {directory}")

    metadata = None if mode == "SIMULATE" else validate_saved_run(sim_dir, cfg)
    if metadata is not None:
        report_progress("loading_saved", "Validating and loading saved results")
        print("Loading saved baseline and yearly states; no downloads or simulation.")
        baseline = load_baseline(sim_dir / "baseline.npz", grid)
        result = load_simulation(sim_dir, cfg, grid)
    else:
        if mode == "REPLAY":
            raise FileNotFoundError(
                f"No complete matching simulation at {sim_dir}; run AUTO first."
            )
        if destination is not None:
            directory = Path(destination)
            sim_dir = directory / "simulation"
        sim_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "complete": False,
            "schema_version": STATE_SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
            "seed": cfg["seed"],
            "bounds": cfg["bounds"],
            "base_year": cfg["base_year"],
            "end_year": cfg["end_year"],
            "lodes_year": cfg["lodes_year"],
            "simulation_config": simulation_config(cfg),
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "interpretation": "Experimental data-grounded scenario; not a calibrated forecast.",
            "data_roles": {
                "real": "OSM geometry/tags; LODES workplace jobs and jobs by residence.",
                "derived": "Classification, intensity, accessibility, fractional coverage.",
                "assumed": "Household baseline when estimated, growth, capacity, vacancy, transitions.",
                "generated": "Future parcels, streets, buildings and parking.",
            },
        }
        started = time.perf_counter()
        baseline = build_baseline(cfg, grid)
        # An offline preflight failure must not invalidate an existing completed run.
        atomic_json(sim_dir / "metadata.json", metadata)
        report_progress("saving_baseline", "Saving baseline")
        save_baseline(sim_dir / "baseline.npz", baseline)
        if cfg.get("profile_performance", True):
            print(
                f"Data preparation + baseline save: {time.perf_counter() - preparation_started:.3f} s"
            )
        result = run_simulation(cfg, grid, baseline, sim_dir)
        metadata["data"] = baseline.get("metadata", {})
        metadata["simulation"] = result.get("metadata", {})
        metadata["calculation_seconds"] = round(time.perf_counter() - started, 3)
        paths = [sim_dir / "baseline.npz", sim_dir / "versions.json", sim_dir / "summary.csv"]
        paths.extend(
            sim_dir / f"{year}.npz" for year in range(cfg["base_year"], cfg["end_year"] + 1)
        )
        metadata["files"] = {path.name: file_sha256(path) for path in paths}
        metadata["complete"] = True
        atomic_json(sim_dir / "metadata.json", metadata)
    result["run_directory"] = str(directory)
    result["metadata"] = metadata
    return grid, baseline, result
