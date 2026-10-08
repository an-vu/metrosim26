"""Read-only source validation and isolated extension of completed simulations."""

from __future__ import annotations

import csv
import json
import math
import shutil
import time
import uuid
from pathlib import Path

from metrosim26.config import (
    ENGINE_SCHEMA_VERSION,
    MODEL_VERSION,
    evolution_enabled,
    run_directory,
    simulation_config,
)
from metrosim26.data import atomic_json, load_baseline
from metrosim26.history import extra_output_names, read_continuation
from metrosim26.runtime import report_progress, write_json
from metrosim26.workers import Grid


def source_directory(path):
    directory = Path(path).expanduser().resolve()
    if (directory / "metadata.json").is_file():
        return directory
    if (directory / "simulation" / "metadata.json").is_file():
        return directory / "simulation"
    pointer = directory / "latest_completed.json"
    if pointer.is_file():
        target = (directory / json.loads(pointer.read_text(encoding="utf8"))["directory"]).resolve()
        if not target.is_relative_to(directory):
            raise ValueError("Completed-run pointer is outside its scenario directory.")
        if (target / "simulation" / "metadata.json").is_file():
            return target / "simulation"
    raise ValueError("EXTEND_FROM must identify a completed run or its simulation folder.")


def comparable_parameters(cfg):
    parameters = simulation_config(cfg)
    parameters.pop("end_year", None)
    parameters.pop("extension_source_sha256", None)
    # Explicitly supported rename, not an arbitrary model-version override.
    if parameters.get("model_version") == "omaha-demand-sites-3.0":
        parameters["model_version"] = MODEL_VERSION
        if parameters.get("osm_query_version") == "omaha-v3-relations-1":
            parameters["osm_query_version"] = "metrosim26-v3-relations-1"
    return parameters


def extension_config(source, end_year, *, output_dir=None):
    """Restore saved scenario inputs; current defaults supply runtime/visual settings only."""
    from metrosim26.config import make_config, validate_config

    directory = source_directory(source)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf8"))
    if not metadata.get("complete"):
        raise ValueError("Select a completed saved run; partial runs cannot be extended.")
    if metadata.get("schema_version") != ENGINE_SCHEMA_VERSION or metadata.get(
        "model_version"
    ) not in {MODEL_VERSION, "omaha-demand-sites-3.0"}:
        raise ValueError("Unsupported saved model/schema; retain the original run for migration.")
    original = metadata["simulation_config"]
    cfg = make_config(preview=original["preview_mode"])
    # These fields were intentionally absent in legacy/disabled-feature metadata.
    cfg.update(
        enable_projects=False,
        enable_infrastructure=False,
        historical_mode="DISABLED",
        historical_evidence_file="",
        extend_from="",
    )
    cfg.update(original)
    cfg.pop("extension_source_sha256", None)
    if cfg["model_version"] == "omaha-demand-sites-3.0":
        cfg["model_version"] = MODEL_VERSION
        if cfg.get("osm_query_version") == "omaha-v3-relations-1":
            cfg["osm_query_version"] = "metrosim26-v3-relations-1"
    cfg.update(
        run_mode="EXTEND",
        extend_from=str(directory),
        end_year=int(end_year),
        data_mode="OFFLINE",
        save_continuation=True,
        save_blend=False,
    )
    if output_dir is not None:
        cfg["output_dir"] = str(Path(output_dir).expanduser().resolve())
    validate_config(cfg)
    if cfg["end_year"] <= original["end_year"]:
        raise ValueError(f"Choose an end year later than {original['end_year']}.")
    if comparable_parameters(cfg) != comparable_parameters(original):
        raise ValueError("Saved settings cannot be restored exactly by this version.")
    return cfg


def scene_source(scene):
    """Find explicit or legacy scene linkage; never infer a run from visible geometry."""
    linked = scene.get("metrosim26_saved_run")
    if linked:
        return str(linked)
    try:
        saved = json.loads(scene.get("configuration_json", "{}"))
    except (TypeError, ValueError):
        return ""
    if saved.get("model_version") not in {MODEL_VERSION, "omaha-demand-sites-3.0"}:
        return ""
    return saved.get("run_directory", "")


def reconstruct_ledger(result):
    """Recover legacy scalars in original accumulation order, without subtractive rounding."""
    final = result["summary"][-1]
    mapping = {
        "households": "scenario_households",
        "jobs": "scenario_jobs",
        "free_housing": "unused_housing_capacity",
        "free_jobs": "unused_job_capacity",
        "unmet_housing": "unmet_housing_demand",
        "unmet_jobs": "unmet_job_demand",
        "cumulative_housing_demand": "cumulative_housing_demand",
        "cumulative_job_demand": "cumulative_job_demand",
        "cumulative_housing_served": "cumulative_housing_demand_served",
        "cumulative_jobs_served": "cumulative_job_demand_served",
    }
    ledger = {key: final[value] for key, value in mapping.items()}
    for name, column in (("housing", "housing_capacity_added"), ("jobs", "job_capacity_added")):
        total = 0.0
        for row in result["summary"]:
            total += row[column]
        ledger[f"cumulative_{name}_added"] = total
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in ledger.values()):
        raise ValueError("Saved demand/capacity ledger contains invalid numbers.")
    return ledger


def inspect_extension(cfg):
    """Validate and restore a source; never writes or recalculates any year."""
    from metrosim26.simulation import file_sha256, load_simulation, validate_saved_run

    source = source_directory(cfg["extend_from"])
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf8"))
    if not metadata.get("complete"):
        raise ValueError("EXTEND requires a completed source run, not partial checkpoints.")
    if metadata.get("schema_version") != ENGINE_SCHEMA_VERSION or metadata.get(
        "model_version"
    ) not in {MODEL_VERSION, "omaha-demand-sites-3.0"}:
        raise ValueError("Unsupported source model/schema; no automatic migration is available.")
    original = metadata["simulation_config"]
    expected, actual = comparable_parameters(original), comparable_parameters(cfg)
    differences = sorted(
        k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k)
    )
    if differences:
        raise ValueError(
            "Extension must preserve source scenario settings: " + ", ".join(differences)
        )
    if cfg["end_year"] <= original["end_year"]:
        raise ValueError("END_YEAR must be later than the source run's completed year.")
    # Validate every manifest entry, including optional exports not requested now.
    for name, digest in metadata["files"].items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or not path.is_file() or file_sha256(path) != digest:
            raise ValueError("Saved simulation file missing or changed: " + name)
    validate_saved_run(source, original)
    grid = Grid(tuple(original["bounds"]), original["cell_km"])
    baseline = load_baseline(source / "baseline.npz", grid)
    result = load_simulation(source, original, grid)
    # JSON checkpoints sort keys; retain the authoritative CSV column order.
    with (source / "summary.csv").open(encoding="utf8", newline="") as stream:
        columns = next(csv.reader(stream))
    if set(columns) != set(result["summary"][-1]):
        raise ValueError("Saved summary columns disagree with annual metrics.")
    result["summary"] = [{key: row[key] for key in columns} for row in result["summary"]]
    manifest = json.loads((source / "versions.json").read_text(encoding="utf8"))
    if manifest["completed_year"] != original["end_year"]:
        raise ValueError("Source completed year does not match its configuration.")
    ledger = reconstruct_ledger(result)
    contract = None
    if (source / "continuation.json").exists():
        if "continuation.json" not in metadata["files"]:
            raise ValueError("Continuation contract is not covered by the completed-run manifest.")
        contract = read_continuation(source)
        if comparable_parameters(contract["simulation_parameters"]) != expected:
            raise ValueError("Continuation parameters disagree with source metadata.")
        if (
            contract["ledger"] != ledger
            or contract["baseline_capacity"] != result["baseline_capacity"]
        ):
            raise ValueError("Continuation ledger disagrees with saved annual states.")
    if evolution_enabled(original) and contract is None:
        raise ValueError("Projects/infrastructure require a saved continuation contract.")
    if evolution_enabled(original):
        for key in ("projects", "infrastructure"):
            if key == "infrastructure" and not original.get("enable_infrastructure"):
                continue
            saved = json.loads((source / f"{key}.json").read_text(encoding="utf8"))
            snapshot = {
                k: v for k, v in saved.items() if k not in {"schema_version", "completed_year"}
            }
            if contract[key] != snapshot:
                raise ValueError(f"Continuation {key} snapshot disagrees with saved records.")
    return source, metadata, grid, baseline, dict(result=result, ledger=ledger, contract=contract)


def extend_simulation(cfg, *, destination=None):
    from metrosim26.simulation import file_sha256, run_simulation

    started = time.perf_counter()
    report_progress("loading_saved", "Validating source run for extension")
    source, metadata, grid, baseline, resume = inspect_extension(cfg)
    canonical = run_directory(cfg).resolve()
    directory = (
        Path(destination).resolve()
        if destination is not None
        else canonical / "extensions" / uuid.uuid4().hex
    )
    # Sources are immutable, including when a caller supplies its own destination.
    source_run = source.parent
    if (
        directory == source_run
        or directory.is_relative_to(source_run)
        or source_run.is_relative_to(directory)
    ):
        raise ValueError("Extension destination must be separate from the source run.")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            "Extension destination must be empty; existing results are never overwritten."
        )
    sim_dir = directory / "simulation"
    sim_dir.mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg, save_continuation=True)
    previous_end = metadata["end_year"]
    new_metadata = dict(
        metadata,
        complete=False,
        model_version=MODEL_VERSION,
        end_year=cfg["end_year"],
        simulation_config=simulation_config(cfg),
        created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        extension={
            "source_directory": str(source),
            "source_metadata_sha256": file_sha256(source / "metadata.json"),
            "source_model_version": metadata["model_version"],
            "restored_year": previous_end,
            "first_calculated_year": previous_end + 1,
            "legacy_ledger_reconstructed": resume["contract"] is None,
        },
        files={},
    )
    atomic_json(sim_dir / "metadata.json", new_metadata)
    for name in metadata["files"]:
        target = sim_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    print(
        f"Extending completed {previous_end} state through {cfg['end_year']}; source retained: {source}"
    )
    report_progress(
        "simulation",
        "Restored source; calculating additional years",
        start_year=previous_end + 1,
        end_year=cfg["end_year"],
        year=previous_end + 1,
    )
    result = run_simulation(cfg, grid, baseline, sim_dir, resume=resume)
    if "history" in resume["result"]:
        result["history"] = resume["result"]["history"]
        result["states"] = [
            s for s in resume["result"]["states"] if s["year"] < cfg["base_year"]
        ] + result["states"]
        if not evolution_enabled(cfg):
            result["events"] = result["history"]["events"]
    names = {"baseline.npz", "versions.json", "summary.csv", *extra_output_names(cfg)}
    names.update(f"{y}.npz" for y in range(cfg["base_year"], cfg["end_year"] + 1))
    new_metadata.update(
        complete=True,
        calculation_seconds=round(time.perf_counter() - started, 3),
        files={name: file_sha256(sim_dir / name) for name in sorted(names)},
    )
    atomic_json(sim_dir / "metadata.json", new_metadata)
    if directory.is_relative_to(canonical):
        canonical.mkdir(parents=True, exist_ok=True)
        write_json(
            canonical / "latest_completed.json",
            {"directory": str(directory.relative_to(canonical))},
        )
    result.update(run_directory=str(directory), metadata=new_metadata)
    return grid, baseline, result
