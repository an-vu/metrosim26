"""Growth simulation. No Blender dependencies."""

from __future__ import annotations

import os
import time
from pathlib import Path

from metrosim26 import workers as workers
from metrosim26.config import (
    EVOLUTION_SCHEMA_VERSION,
    MODEL_VERSION,
    STATE_SCHEMA_VERSION,
    evolution_enabled,
    run_directory,
    simulation_config,
    validate_config,
)
from metrosim26.config import HIGHRISE as HIGHRISE
from metrosim26.config import SUBURBAN as SUBURBAN
from metrosim26.data import (
    atomic_json,
    build_baseline,
    load_baseline,
    save_baseline,
)

# Stable public calculation API; implementations are separated by responsibility.
from metrosim26.growth.capacity import choose_archetype as choose_archetype
from metrosim26.growth.capacity import create_site as create_site
from metrosim26.growth.capacity import estimate_baseline_capacity as estimate_baseline_capacity
from metrosim26.growth.capacity import highrise_allowed as highrise_allowed
from metrosim26.growth.capacity import neighbor_fraction as neighbor_fraction
from metrosim26.growth.capacity import ranked_cells as ranked_cells
from metrosim26.growth.capacity import redevelopment_archetypes as redevelopment_archetypes
from metrosim26.growth.capacity import select_plan_buildings as select_plan_buildings
from metrosim26.growth.capacity import simulation_metrics as simulation_metrics
from metrosim26.growth.capacity import site_capacity as site_capacity
from metrosim26.growth.checkpoints import (
    _saved_run_directory,
    file_sha256,
    load_simulation,
    validate_saved_run,
)
from metrosim26.growth.checkpoints import save_summary_csv as save_summary_csv
from metrosim26.growth.checkpoints import save_year_state as save_year_state
from metrosim26.growth.engine import _run_simulation as _run_simulation
from metrosim26.growth.engine import run_simulation
from metrosim26.growth.planning import _PlanningCache as _PlanningCache
from metrosim26.growth.planning import _worker_python as _worker_python
from metrosim26.history import (
    build_history,
    extra_output_names,
)
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    Grid,
)
from metrosim26.workers import make_site_plan as make_site_plan


def prepare_simulation(cfg, *, destination=None, baseline_ready=None):
    """Pure calculation orchestration; also usable from a standalone NumPy process."""
    validate_config(cfg)
    if cfg["run_mode"].upper() == "EXTEND":
        from metrosim26.continuation import extend_simulation

        return extend_simulation(cfg, destination=destination)
    preparation_started = time.perf_counter()
    grid = Grid(tuple(cfg["bounds"]), cfg["cell_km"])
    directory = run_directory(cfg)
    mode = cfg["run_mode"].upper()
    if mode != "SIMULATE":
        report_progress("loading_saved", "Validating and locating saved results")
        directory = _saved_run_directory(cfg, directory)
    sim_dir = directory / "simulation"
    print(f"MetroSim26 scenario | seed {cfg['seed']} | {cfg['base_year']}–{cfg['end_year']}")
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
        if baseline_ready is not None:
            baseline_ready(grid, baseline, str(directory))
        result = run_simulation(cfg, grid, baseline, sim_dir)
        if cfg.get("historical_mode") == "EVIDENCE":
            report_progress("historical_reconstruction", "Preparing evidence-led historical states")
            history, historical_states = build_history(cfg, grid, baseline, sim_dir)
            result["history"] = history
            result["states"] = historical_states + result["states"]
            if not evolution_enabled(cfg):
                result["events"] = history["events"]
                atomic_json(
                    sim_dir / "events.json",
                    dict(schema_version=EVOLUTION_SCHEMA_VERSION, events=history["events"]),
                )
        metadata["data"] = baseline.get("metadata", {})
        metadata["simulation"] = result.get("metadata", {})
        metadata["calculation_seconds"] = round(time.perf_counter() - started, 3)
        paths = [sim_dir / "baseline.npz", sim_dir / "versions.json", sim_dir / "summary.csv"]
        paths.extend(
            sim_dir / f"{year}.npz" for year in range(cfg["base_year"], cfg["end_year"] + 1)
        )
        paths.extend(sim_dir / name for name in extra_output_names(cfg))
        metadata["files"] = {
            str(path.relative_to(sim_dir)).replace(os.sep, "/"): file_sha256(path) for path in paths
        }
        metadata["complete"] = True
        atomic_json(sim_dir / "metadata.json", metadata)
    result["run_directory"] = str(directory)
    result["metadata"] = metadata
    return grid, baseline, result
