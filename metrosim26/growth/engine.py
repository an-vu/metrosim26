"""Growth engine. No Blender dependencies."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

from metrosim26.config import (
    ENGINE_SCHEMA_VERSION,
    EVOLUTION_SCHEMA_VERSION,
    evolution_enabled,
)
from metrosim26.data import (
    atomic_json,
)
from metrosim26.growth.capacity import (
    choose_archetype,
    create_site,
    estimate_baseline_capacity,
    highrise_allowed,
    neighbor_fraction,
    redevelopment_archetypes,
    simulation_metrics,
    site_capacity,
)
from metrosim26.growth.checkpoints import save_summary_csv, save_year_state
from metrosim26.growth.planning import _PlanningCache
from metrosim26.history import (
    load_evidence,
    save_continuation,
)
from metrosim26.infrastructure import Infrastructure
from metrosim26.projects import Projects
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    site_id,
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
        with tempfile.TemporaryDirectory(prefix="metrosim26_validation_") as tmp:
            saved_baseline = Path(sim_dir) / "baseline.npz"
            if (
                evolution_enabled(cfg) or cfg.get("save_continuation")
            ) and saved_baseline.is_file():
                shutil.copyfile(saved_baseline, Path(tmp) / "baseline.npz")
            run_simulation(serial_cfg, grid, baseline, tmp)
            for path in sorted(Path(tmp).iterdir()):
                if path.read_bytes() != (Path(sim_dir) / path.name).read_bytes():
                    raise AssertionError("Serial/parallel checkpoint mismatch: " + path.name)
        print("[MetroSim26] Serial/parallel validation: all authoritative files byte-identical.")
    elif cfg.get("validate_parallel_results", False):
        print("[MetroSim26] Validation skipped: no parallel plans were needed in this run.")
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
    if "study_mask" in baseline:
        protected_ok &= baseline["study_mask"]
    access_ok = baseline["road_access"] >= cfg.get("minimum_road_access", 0.10)
    source_baseline = baseline
    events = load_evidence(cfg, baseline)[1] if cfg.get("historical_mode") == "EVIDENCE" else []
    projects = Projects(cfg, grid, events)
    infrastructure = (
        Infrastructure(cfg, grid, baseline, events) if cfg.get("enable_infrastructure") else None
    )
    continuation_hashes = {}
    for year in range(cfg["base_year"], cfg["end_year"] + 1):
        if evolution_enabled(cfg):
            baseline = (
                projects.feedback(source_baseline)
                if cfg.get("enable_projects")
                else dict(source_baseline)
            )
            if infrastructure is not None:
                infrastructure.begin_year(year)
                baseline["road_access"] = infrastructure.accessibility(year)
            access_ok = baseline["road_access"] >= cfg.get("minimum_road_access", 0.10)
            if infrastructure is not None:
                access_ok &= ~infrastructure.reserved()
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

            def install(site, previous, phase, scheduled=False):
                nonlocal \
                    free_housing, \
                    free_jobs, \
                    unmet_housing, \
                    unmet_jobs, \
                    served_housing, \
                    served_jobs
                if not scheduled:
                    site = projects.stage(
                        site, previous, phase, year, baseline, unmet_housing, unmet_jobs
                    )
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

            # Previously committed construction opens before new opportunities.
            # It credits only delivered capacity; any excess remains vacant.
            for site, previous in projects.due(year, active):
                install(site, previous, "infill", scheduled=True)
            if cfg.get("enable_projects"):
                for sid in projects.pending:
                    reserved = active[sid]
                    access_ok[reserved["gy"], reserved["gx"]] = False
                for flat in touched:
                    access_ok.flat[flat] = False

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
        projects.refresh(active)
        if infrastructure is not None:
            infrastructure.propose(
                year, urban, neighbor_fraction(urban), unmet_housing, unmet_jobs, active, projects
            )
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
        if evolution_enabled(cfg) or cfg.get("historical_mode") == "EVIDENCE":
            state["provenance"] = (
                "model_derived" if year == cfg["base_year"] else "future_generated"
            )
        if evolution_enabled(cfg):
            state["projects"] = projects.snapshot()["projects"]
            state["infrastructure"] = (
                [
                    dict(
                        infrastructure_id=r["infrastructure_id"],
                        status=r["status"],
                        phase=r["phase"],
                    )
                    for r in infrastructure.records
                ]
                if infrastructure
                else []
            )
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
        if evolution_enabled(cfg):
            atomic_json(
                sim_dir / "projects.json",
                dict(
                    schema_version=EVOLUTION_SCHEMA_VERSION,
                    completed_year=year,
                    **projects.snapshot(),
                ),
                compact=True,
            )
            atomic_json(
                sim_dir / "events.json",
                dict(schema_version=EVOLUTION_SCHEMA_VERSION, completed_year=year, events=events),
                compact=True,
            )
            if infrastructure is not None:
                atomic_json(
                    sim_dir / "infrastructure.json",
                    dict(
                        schema_version=EVOLUTION_SCHEMA_VERSION,
                        completed_year=year,
                        **infrastructure.snapshot(),
                    ),
                    compact=True,
                )
        if evolution_enabled(cfg) or cfg.get("save_continuation"):
            save_continuation(
                sim_dir,
                cfg,
                state,
                initial,
                projects,
                infrastructure,
                dict(
                    households=households,
                    jobs=jobs,
                    free_housing=free_housing,
                    free_jobs=free_jobs,
                    unmet_housing=unmet_housing,
                    unmet_jobs=unmet_jobs,
                    cumulative_housing_demand=cumulative_housing_demand,
                    cumulative_job_demand=cumulative_job_demand,
                    cumulative_housing_served=cumulative_housing_served,
                    cumulative_jobs_served=cumulative_jobs_served,
                    cumulative_housing_added=cumulative_housing_added,
                    cumulative_jobs_added=cumulative_jobs_added,
                ),
                continuation_hashes,
            )
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
        **(
            {
                "projects": projects.snapshot()["projects"],
                "events": events,
                "infrastructure": infrastructure.snapshot()["infrastructure"]
                if infrastructure
                else [],
            }
            if evolution_enabled(cfg)
            else {}
        ),
        "metadata": {
            "scenario_capacity_baseline": initial,
            "developed_area_metric": baseline["metadata"]["developed_area_metric"],
        },
    }
