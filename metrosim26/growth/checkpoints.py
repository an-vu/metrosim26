"""Growth checkpoints. No Blender dependencies."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np

from metrosim26.config import (
    ENGINE_SCHEMA_VERSION,
    EVOLUTION_SCHEMA_VERSION,
    evolution_enabled,
    simulation_config,
)
from metrosim26.data import (
    atomic_npz,
    replace_checkpoint,
)
from metrosim26.history import (
    extra_output_names,
    load_history,
)


def save_year_state(sim_dir, state):
    payload = {key: state[key] for key in ("year", "demolished", "metrics")}
    for key in ("provenance", "projects", "infrastructure"):
        if key in state:
            payload[key] = state[key]
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
    replace_checkpoint(temp_path, path)


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
    result = {
        "states": states,
        "versions": manifest["versions"],
        "summary": [state["metrics"] for state in states],
        "baseline_capacity": manifest["baseline_capacity"],
        "metadata": {
            "scenario_capacity_baseline": manifest["baseline_capacity"],
            "developed_area_metric": "Approximate developed-cell envelope in km2, reduced by water/park fractions; not observed impervious area.",
        },
    }
    if evolution_enabled(cfg):
        for name in ("projects", "events", "infrastructure"):
            if name == "infrastructure" and not cfg.get("enable_infrastructure"):
                result[name] = []
                continue
            record = json.loads((sim_dir / f"{name}.json").read_text(encoding="utf8"))
            if (
                record.get("schema_version") != EVOLUTION_SCHEMA_VERSION
                or record.get("completed_year") != cfg["end_year"]
            ):
                raise ValueError(f"Incompatible/incomplete {name} history.")
            result[name] = record[name]
    if cfg.get("historical_mode") == "EVIDENCE":
        history, historical_states = load_history(sim_dir)
        result["history"] = history
        result["states"] = historical_states + states
        if not evolution_enabled(cfg):
            result["events"] = history["events"]
    return result


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
    expected.update(extra_output_names(cfg))
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
