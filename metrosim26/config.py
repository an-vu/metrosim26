"""Scenario settings, validation, and saved-run identity."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

SEED = 872197  # Easy to change; all procedural randomness derives from this.
PREVIEW_MODE = False
RUN_MODE = "SIMULATE"  # AUTO | SIMULATE | REPLAY
ENABLE_PARALLEL_COMPUTE = True
CPU_WORKERS = 16
PROFILE_PERFORMANCE = True
PARALLEL_BATCH_SIZE = 32  # Bounded lookahead; no changes to candidate ranking.
PARALLEL_MIN_TASKS = 8  # Small cache-miss batches are cheaper to do serially.
PARALLEL_MIN_BATCH_SECONDS = 0.20  # Estimate before paying process startup costs.
WORKER_PYTHON = ""  # Auto-detect bundled Python; optional absolute override.
VALIDATE_PARALLEL_RESULTS = False  # Debug: costs a second, serial simulation.
BASE_YEAR = 2026
END_YEAR = 2076
PREVIEW_END_YEAR = 2035
ENABLE_PROJECTS = False
ENABLE_INFRASTRUCTURE = False
HISTORICAL_MODE = "DISABLED"  # DISABLED | EVIDENCE (partial reconstruction, never reverse growth)
TIMELINE_START_YEAR = None  # None uses BASE_YEAR; e.g. 2006 with HISTORICAL_MODE = "EVIDENCE"
HISTORICAL_EVIDENCE_FILE = ""  # Optional local JSON; undocumented historical visibility is unknown.
SAVE_CONTINUATION = False  # Export restart groundwork; EXTEND is not yet a supported run mode.
EVOLUTION_SCHEMA_VERSION = 1
CELL_KM = 0.35
FULL_BOUNDS = (40.99031149984831, -96.43156075030879, 41.453886499728576, -95.61498024942590)
PREVIEW_BOUNDS = (41.22, -96.06, 41.31, -95.89)

# Household baseline is estimated from OSM floorspace when None. Supply a known
# study-area household count here to replace that assumption. Jobs use WAC sum.
INITIAL_HOUSEHOLDS = None
INITIAL_JOBS = None
ANNUAL_HOUSEHOLD_GROWTH_RATE = 0.008
ANNUAL_JOB_GROWTH_RATE = 0.010
INITIAL_HOUSING_VACANCY = 0.04
INITIAL_JOB_VACANCY = 0.04
MIN_REDEVELOPMENT_AGE = 12
SUBSTANTIAL_WATER_FRACTION = 0.45
SUBCELL_SAMPLES = 8  # Fraction estimates only; placement uses polygon checks.

DATA_MODE = "OFFLINE"  # Blender is always offline; prefetch explicitly enables acquisition.
DOWNLOAD_WORKERS = 1  # Network requests are sequential; CPU_WORKERS is unrelated.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URLS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
DOWNLOAD_ATTEMPT_TIMEOUT = 300  # Hard wall-clock limit for a complete HTTP attempt.

LODES_YEAR = 2023
LODES_VERSION = "LODES8"
# Change vintage to deliberately obtain a revised release of the same year.
LODES_VINTAGE = "cached-release"
OSM_QUERY_VERSION = "metrosim26-v3-relations-1"
OSM_DATA_SNAPSHOT = None  # Optional Overpass historical ISO UTC date string.
MODEL_VERSION = "metrosim26-demand-sites-3.0"
STATE_SCHEMA_VERSION = 3
CACHE_ROOT = Path.home() / "Documents" / "Blender_MetroSim26_V3_Cache"
OUTPUT_ROOT = Path.home() / "Documents" / "Blender_MetroSim26_V3_Output"

# Visualization settings never affect generated simulation states.
VERTICAL_EXAGGERATION = 1.25
RENDER_SAMPLES = 128
PREVIEW_SAMPLES = 24
RENDER_RESOLUTION = (1920, 1080)
AUTO_FINAL_RENDER = False
RENDER_ANIMATION = False
SAVE_BLEND = False
REQUIRE_OPTIX = False  # True to fail instead of falling back to CPU.

EMPTY, SUBURBAN, URBAN_RES, MIXED, COMMERCIAL, RETAIL, INDUSTRIAL, OFFICE, HIGHRISE = range(9)
ARCH_NAMES = {
    EMPTY: "Undeveloped",
    SUBURBAN: "Suburban residential",
    URBAN_RES: "Urban residential",
    MIXED: "Mixed use",
    COMMERCIAL: "Commercial corridor",
    RETAIL: "Retail / mall",
    INDUSTRIAL: "Industrial / logistics",
    OFFICE: "Office / institutional",
    HIGHRISE: "High-rise",
}
LAND_NONE, LAND_RES, LAND_COMMERCIAL, LAND_RETAIL = 0, 1, 2, 3
LAND_INDUSTRIAL, LAND_INSTITUTIONAL, LAND_PARK, LAND_WATER = 4, 5, 8, 9


def make_config(preview=None):
    """Centralized serializable inputs, including explicit scenario assumptions."""
    preview_mode = PREVIEW_MODE if preview is None else bool(preview)
    return {
        "seed": SEED,
        "preview_mode": preview_mode,
        "run_mode": RUN_MODE,
        "enable_parallel_compute": ENABLE_PARALLEL_COMPUTE,
        "cpu_workers": CPU_WORKERS,
        "profile_performance": PROFILE_PERFORMANCE,
        "parallel_batch_size": PARALLEL_BATCH_SIZE,
        "parallel_min_tasks": PARALLEL_MIN_TASKS,
        "worker_python": WORKER_PYTHON,
        "parallel_min_batch_seconds": PARALLEL_MIN_BATCH_SECONDS,
        "worker_timeout": 120,
        "validate_parallel_results": VALIDATE_PARALLEL_RESULTS,
        "base_year": BASE_YEAR,
        "end_year": PREVIEW_END_YEAR if preview_mode else END_YEAR,
        "enable_projects": ENABLE_PROJECTS,
        "enable_infrastructure": ENABLE_INFRASTRUCTURE,
        "historical_mode": HISTORICAL_MODE,
        "timeline_start_year": TIMELINE_START_YEAR,
        "historical_evidence_file": str(
            (
                Path(__file__).resolve().parents[1] / Path(HISTORICAL_EVIDENCE_FILE).expanduser()
            ).resolve()
        )
        if HISTORICAL_EVIDENCE_FILE
        else "",
        "save_continuation": SAVE_CONTINUATION,
        "project_min_buildings": 2,
        "project_min_housing": 40.0,
        "project_min_jobs": 150.0,
        "project_construction_years": 3,
        "project_max_starts_per_year": 3,
        "project_feedback_cap": 0.15,
        "infrastructure_pressure_years": 3,
        "infrastructure_max_starts_per_year": 1,
        "infrastructure_max_projects": 24,
        "infrastructure_max_route_cells": 8,
        "infrastructure_construction_years": 3,
        "infrastructure_access_threshold": 0.40,
        "infrastructure_access_gain": 0.20,
        "infrastructure_width_km": 0.018,
        "bounds": list(PREVIEW_BOUNDS if preview_mode else FULL_BOUNDS),
        "cell_km": CELL_KM,
        "subcell_samples": SUBCELL_SAMPLES,
        "initial_households": INITIAL_HOUSEHOLDS,
        "initial_jobs": INITIAL_JOBS,
        "annual_household_growth_rate": ANNUAL_HOUSEHOLD_GROWTH_RATE,
        "annual_job_growth_rate": ANNUAL_JOB_GROWTH_RATE,
        "initial_housing_vacancy": INITIAL_HOUSING_VACANCY,
        "initial_job_vacancy": INITIAL_JOB_VACANCY,
        "min_redevelopment_age": MIN_REDEVELOPMENT_AGE,
        "substantial_water_fraction": SUBSTANTIAL_WATER_FRACTION,
        "max_sites_per_year": 160 if preview_mode else 600,
        "max_candidates_per_phase": 1200,
        # MODEL capacity: net usable floor area per home/job, in square meters.
        "usable_floor_area_ratio": 0.80,
        "floor_area_per_home_m2": 95.0,
        "commercial_area_per_job_m2": 38.0,
        "retail_area_per_job_m2": 45.0,
        "industrial_area_per_job_m2": 105.0,
        "office_area_per_job_m2": 24.0,
        "housing_share_by_archetype": {str(URBAN_RES): 1.0, str(MIXED): 0.65, str(HIGHRISE): 0.55},
        "minimum_spacing_by_archetype_km": {
            str(SUBURBAN): 0.007,
            str(URBAN_RES): 0.005,
            str(MIXED): 0.004,
            str(COMMERCIAL): 0.005,
            str(RETAIL): 0.006,
            str(INDUSTRIAL): 0.010,
            str(OFFICE): 0.006,
            str(HIGHRISE): 0.010,
        },
        "building_clearance_km": 0.003,
        "initial_parcel_fill": 0.65,
        "infill_fraction": 0.35,
        "substantial_park_fraction": 0.75,
        "minimum_road_access": 0.10,
        "highrise_min_centrality": 0.70,
        "highrise_min_job_signal": 0.55,
        "highrise_min_neighbor_fraction": 0.45,
        "highrise_min_housing_demand": 150.0,
        "highrise_min_job_demand": 100.0,
        "downtown_anchor": [-95.9345, 41.2565],
        "centrality_decay_km": 5.0,
        "lodes_year": LODES_YEAR,
        "lodes_version": LODES_VERSION,
        "lodes_vintage": LODES_VINTAGE,
        "lodes_states": ["ne", "ia"],
        "lodes_job_type": "JT00",
        "osm_query_version": OSM_QUERY_VERSION,
        "osm_data_snapshot": OSM_DATA_SNAPSHOT,
        "osm_tiles": [2, 2] if preview_mode else [8, 8],
        "data_mode": DATA_MODE,
        "download_workers": DOWNLOAD_WORKERS,
        "download_timeout": 240,
        "download_attempt_timeout": DOWNLOAD_ATTEMPT_TIMEOUT,
        "download_attempts": 3,
        "overpass_url": OVERPASS_URL,
        "overpass_fallback_urls": list(OVERPASS_FALLBACK_URLS),
        "download_backoff_initial": 30,
        "download_backoff_max": 300,
        "road_influence_km": 0.8,
        "cache_dir": str(CACHE_ROOT),
        "output_dir": str(OUTPUT_ROOT),
        "model_version": MODEL_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "vertical_exaggeration": VERTICAL_EXAGGERATION,
        "render_samples": PREVIEW_SAMPLES if preview_mode else RENDER_SAMPLES,
        "preview_samples": PREVIEW_SAMPLES,
        "render_resolution": list(RENDER_RESOLUTION),
        "render_fps": 2,
        "render_final": AUTO_FINAL_RENDER,
        "render_animation": RENDER_ANIMATION,
        "save_blend": SAVE_BLEND,
        "require_optix": REQUIRE_OPTIX,
        "render_device_name": "3080 Ti",
        "mesh_chunk_vertices": 120000,
    }


def validate_config(cfg):
    if cfg.get("timeline_start_year") is not None and type(cfg["timeline_start_year"]) is not int:
        raise ValueError("TIMELINE_START_YEAR must be an integer or None.")
    if cfg.get("historical_mode", "DISABLED") not in {"DISABLED", "EVIDENCE"}:
        raise ValueError("HISTORICAL_MODE must be DISABLED or EVIDENCE.")
    start = timeline_start(cfg)
    if start > cfg["base_year"]:
        raise ValueError("TIMELINE_START_YEAR must not follow BASE_YEAR.")
    if start < cfg["base_year"] and cfg.get("historical_mode", "DISABLED") == "DISABLED":
        raise ValueError("A pre-baseline timeline requires HISTORICAL_MODE='EVIDENCE'.")
    for name in (
        "project_min_buildings",
        "project_construction_years",
        "project_max_starts_per_year",
        "infrastructure_pressure_years",
        "infrastructure_max_starts_per_year",
        "infrastructure_max_projects",
        "infrastructure_max_route_cells",
        "infrastructure_construction_years",
    ):
        if name in cfg and (type(cfg[name]) is not int or cfg[name] < 1):
            raise ValueError(f"{name} must be a positive integer.")
    for name in ("project_min_housing", "project_min_jobs", "infrastructure_width_km"):
        if name in cfg and (not math.isfinite(cfg[name]) or cfg[name] <= 0):
            raise ValueError(f"{name} must be positive and finite.")
    for name in (
        "project_feedback_cap",
        "infrastructure_access_threshold",
        "infrastructure_access_gain",
    ):
        if name in cfg and not 0 <= cfg[name] <= 1:
            raise ValueError(f"{name} must be in [0, 1].")
    if cfg.get("data_mode", "OFFLINE").upper() not in {"OFFLINE", "ONLINE"}:
        raise ValueError("DATA_MODE must be OFFLINE or ONLINE.")
    if len(cfg["osm_tiles"]) != 2 or any(type(n) is not int or n < 1 for n in cfg["osm_tiles"]):
        raise ValueError("osm_tiles must contain two positive integers.")
    if cfg.get("download_workers", 1) != 1:
        raise ValueError("Data acquisition is sequential: DOWNLOAD_WORKERS must be 1.")
    if (
        int(cfg.get("download_attempts", 3)) < 1
        or cfg.get("download_timeout", 240) <= 0
        or cfg.get("download_attempt_timeout", 300) <= 0
    ):
        raise ValueError("Download attempts and timeout must be positive.")
    if any(cfg.get(key, 30) < 0 for key in ("download_backoff_initial", "download_backoff_max")):
        raise ValueError("Download backoff delays cannot be negative.")
    if cfg["run_mode"].upper() not in {"AUTO", "SIMULATE", "REPLAY"}:
        raise ValueError("RUN_MODE must be AUTO, SIMULATE, or REPLAY.")
    if cfg["end_year"] < cfg["base_year"]:
        raise ValueError("End year must not precede base year.")
    for name in ("annual_household_growth_rate", "annual_job_growth_rate"):
        if not 0 <= cfg[name] < 1:
            raise ValueError(f"{name} must be a fraction in [0, 1).")
    for name in ("initial_housing_vacancy", "initial_job_vacancy"):
        if not 0 <= cfg[name] < 1:
            raise ValueError(f"{name} must be a fraction in [0, 1).")
    if not 0 < cfg["substantial_water_fraction"] <= 1:
        raise ValueError("substantial_water_fraction must be in (0, 1].")
    if cfg["subcell_samples"] < 2:
        raise ValueError("Use at least two fractional coverage samples per axis.")
    for name in ("initial_households", "initial_jobs"):
        if cfg[name] is not None and cfg[name] < 0:
            raise ValueError(f"{name} cannot be negative.")
    for name in (
        "floor_area_per_home_m2",
        "commercial_area_per_job_m2",
        "retail_area_per_job_m2",
        "industrial_area_per_job_m2",
        "office_area_per_job_m2",
        "centrality_decay_km",
    ):
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise ValueError(f"{name} must be positive and finite.")
    for name in ("initial_parcel_fill", "infill_fraction", "usable_floor_area_ratio"):
        if not 0 < cfg[name] <= 1:
            raise ValueError(f"{name} must be in (0, 1].")
    if any(not 0 <= value <= 1 for value in cfg["housing_share_by_archetype"].values()):
        raise ValueError("Archetype housing shares must be in [0, 1].")
    if any(value < 0 for value in cfg["minimum_spacing_by_archetype_km"].values()):
        raise ValueError("Minimum building spacing cannot be negative.")


def simulation_config(cfg):
    cfg = dict(cfg)
    # Disabled additions must preserve legacy scenario identity as well as outputs.
    for enabled, prefix in (
        ("enable_projects", "project_"),
        ("enable_infrastructure", "infrastructure_"),
    ):
        if not cfg.get(enabled, False):
            cfg = {
                key: value
                for key, value in cfg.items()
                if key != enabled and not key.startswith(prefix)
            }
    if cfg.get("historical_mode", "DISABLED") == "DISABLED":
        for key in (
            "historical_mode",
            "timeline_start_year",
            "historical_evidence_file",
            "historical_evidence_sha256",
        ):
            cfg.pop(key, None)
    else:
        cfg["timeline_start_year"] = timeline_start(cfg)
        path = cfg.pop("historical_evidence_file", "")
        if path:
            cfg["historical_evidence_sha256"] = hashlib.sha256(
                Path(path).expanduser().read_bytes()
            ).hexdigest()
    cfg.pop("save_continuation", None)  # Export option, not a model parameter.
    if (
        cfg.get("enable_projects")
        or cfg.get("enable_infrastructure")
        or cfg.get("historical_mode") == "EVIDENCE"
    ):
        cfg["evolution_schema_version"] = EVOLUTION_SCHEMA_VERSION
    visual_keys = {
        "data_mode",
        "overpass_url",
        "overpass_fallback_urls",
        "download_backoff_initial",
        "download_backoff_max",
        "run_mode",
        "output_dir",
        "cache_dir",
        "run_directory",
        "download_workers",
        "download_timeout",
        "download_attempts",
        "download_attempt_timeout",
        "vertical_exaggeration",
        "render_samples",
        "preview_samples",
        "render_resolution",
        "render_fps",
        "render_final",
        "render_animation",
        "save_blend",
        "require_optix",
        "render_device_name",
        "mesh_chunk_vertices",
    }
    return {
        key: value
        for key, value in cfg.items()
        if key not in visual_keys and key not in PERFORMANCE_KEYS
    }


def run_directory(cfg):
    digest = hashlib.sha256(
        json.dumps(simulation_config(cfg), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    label = "preview" if cfg["preview_mode"] else "full"
    return Path(cfg["output_dir"]) / f"{label}_{cfg['seed']}_{digest}"


PERFORMANCE_KEYS = {
    "enable_parallel_compute",
    "cpu_workers",
    "profile_performance",
    "parallel_batch_size",
    "parallel_min_tasks",
    "worker_python",
    "worker_timeout",
    "validate_parallel_results",
    "parallel_min_batch_seconds",
}


ENGINE_SCHEMA_VERSION = 3


def timeline_start(cfg):
    value = cfg.get("timeline_start_year")
    return int(cfg["base_year"] if value is None else value)


def evolution_enabled(cfg):
    return bool(cfg.get("enable_projects") or cfg.get("enable_infrastructure"))
