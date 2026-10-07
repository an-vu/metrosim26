"""Evidence-led historical visibility, timeline mapping, and continuation contracts.

No reverse growth, invented historical roads, or mesh-based restart inference.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from omaha_config import (
    EVOLUTION_SCHEMA_VERSION,
    evolution_enabled,
    simulation_config,
    timeline_start,
)
from omaha_data import atomic_json, atomic_npz
from omaha_runtime import report_progress
from omaha_workers import _bbox, _bucket_cells, _ring_hits_polygon


def year_to_frame(year, cfg):
    return int(year) - timeline_start(cfg) + 1


def extra_output_names(cfg):
    names = []
    if evolution_enabled(cfg):
        names += ["projects.json", "events.json", "continuation.json"]
    elif cfg.get("save_continuation"):
        names.append("continuation.json")
    if cfg.get("enable_infrastructure"):
        names.append("infrastructure.json")
    if cfg.get("historical_mode") == "EVIDENCE":
        names += ["history.json", "events.json"]
        names += [f"history/{year}.npz" for year in range(timeline_start(cfg), cfg["base_year"])]
    return sorted(set(names))


def load_evidence(cfg, baseline):
    path = cfg.get("historical_evidence_file")
    evidence = (
        json.loads(Path(path).expanduser().read_text(encoding="utf8"))
        if path
        else {"schema_version": 1, "features": [], "events": []}
    )
    if evidence.get("schema_version") != 1:
        raise ValueError("Historical evidence requires schema_version=1.")
    if set(evidence) - {"schema_version", "features", "events"}:
        raise ValueError(
            "Unsupported historical evidence fields; this adapter accepts feature dates and events only."
        )
    known = {
        kind: {item["id"] for item in baseline[key]}
        for kind, key in (("building", "buildings"), ("road", "roads"), ("land", "land"))
    }
    records = {}
    for item in evidence.get("features", []):
        kind, identity, first = item["kind"], item["id"], item["start_year"]
        if kind not in known or identity not in known[kind]:
            raise ValueError(f"Evidence refers to unknown baseline feature: {kind}/{identity}")
        if type(first) is not int or first > cfg["base_year"]:
            raise ValueError("Historical start_year must be an integer no later than BASE_YEAR.")
        if item.get("end_year") is not None:
            raise ValueError(
                "This first evidence adapter supports surviving baseline features only; historical demolition needs separate geometry."
            )
        if not item.get("source") or item.get("provenance") not in {
            "observed",
            "historically_reconstructed",
        }:
            raise ValueError("Historical features need a source and explicit evidence provenance.")
        key = kind + ":" + identity
        if key in records:
            raise ValueError("Conflicting/duplicate historical evidence: " + key)
        records[key] = dict(item)
    events = []
    for i, item in enumerate(evidence.get("events", [])):
        if (
            type(item.get("year")) is not int
            or item["year"] >= cfg["base_year"]
            or not item.get("source")
            or item.get("provenance") not in {"observed", "historically_reconstructed"}
        ):
            raise ValueError("Historical events need a pre-baseline year, source, and provenance.")
        events.append(dict(item, event_id=f"historical_{i:08d}", fictional=False))
    return records, sorted(events, key=lambda e: (e["year"], e["event_id"]))


def build_history(cfg, grid, baseline, directory):
    records, events = load_evidence(cfg, baseline)
    lifetimes = {kind: {} for kind in ("building", "road", "land")}
    for kind, key in (("building", "buildings"), ("road", "roads"), ("land", "land")):
        for feature in baseline[key]:
            evidence = records.get(kind + ":" + feature["id"])
            lifetimes[kind][feature["id"]] = dict(
                start_year=evidence["start_year"] if evidence else cfg["base_year"],
                end_year=None,
                provenance=evidence["provenance"] if evidence else "observed",
                historical_coverage="evidence_supported" if evidence else "unknown",
                source=evidence["source"]
                if evidence
                else "current cached OSM; historical date unknown",
            )
    history: dict[str, Any] = dict(
        schema_version=EVOLUTION_SCHEMA_VERSION,
        provenance="historically_reconstructed",
        timeline_start_year=timeline_start(cfg),
        baseline_year=cfg["base_year"],
        policy="Partial evidence only. Hidden unknown features are not evidence of historical absence. Current geometry is a proxy; historical shapes and demolished features are not reconstructed.",
        lifetimes=lifetimes,
        events=events,
    )
    states = []
    for year in range(timeline_start(cfg), cfg["base_year"]):
        report_progress(
            "historical_reconstruction", f"Reconstructing historical year {year}", year=year
        )
        visibility = {
            kind: sorted(
                identity for identity, value in entries.items() if value["start_year"] <= year
            )
            for kind, entries in lifetimes.items()
        }
        urban = np.zeros(grid.shape, dtype=bool)
        arch = np.zeros(grid.shape, dtype=np.uint8)
        visible = set(visibility["building"])
        for building in baseline["buildings"]:
            if building["id"] not in visible:
                continue
            for polygon in building["polygons"]:
                for x, y in _bucket_cells(_bbox(polygon["outer"]), grid):
                    x0, y0, x1, y1 = grid.cell_bounds(x, y)
                    if _ring_hits_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], polygon):
                        urban[y, x] = True
                        arch[y, x] = building.get("arch", 0)
        state: dict[str, Any] = dict(
            year=year,
            provenance="historically_reconstructed",
            metrics_provenance="model_derived",
            demolished=[],
            sites=[],
            building_visibility=visibility["building"],
            road_visibility=visibility["road"],
            land_visibility=visibility["land"],
            events=[e for e in events if e["year"] == year],
            unknown_historical_features=sum(
                v["historical_coverage"] == "unknown"
                for entries in lifetimes.values()
                for v in entries.values()
            ),
            metrics=dict(
                year=year,
                evidence_supported_buildings=len(visible),
                evidence_supported_roads=len(visibility["road"]),
                developed_cells_from_visible_geometry=int(urban.sum()),
            ),
        )
        atomic_npz(
            Path(directory) / "history" / f"{year}.npz",
            schema_version=np.asarray(EVOLUTION_SCHEMA_VERSION),
            urban=urban,
            arch=arch,
            unknown_coverage=np.ones(grid.shape, dtype=bool),
            evidence_building_coverage=urban,
            payload_json=np.asarray(json.dumps(state, sort_keys=True)),
        )
        state.update(
            urban=urban,
            arch=arch,
            unknown_coverage=np.ones(grid.shape, dtype=bool),
            evidence_building_coverage=urban.copy(),
            new_development=np.zeros_like(urban),
            redevelopment=np.zeros_like(urban),
            infill=np.zeros_like(urban),
        )
        states.append(state)
    atomic_json(Path(directory) / "history.json", history)
    return history, states


def load_history(directory):
    directory = Path(directory)
    history = json.loads((directory / "history.json").read_text(encoding="utf8"))
    if history["schema_version"] != EVOLUTION_SCHEMA_VERSION:
        raise ValueError("Unsupported historical state schema.")
    states = []
    for year in range(history["timeline_start_year"], history["baseline_year"]):
        with np.load(directory / "history" / f"{year}.npz", allow_pickle=False) as saved:
            state = json.loads(str(saved["payload_json"].item()))
            state.update(
                {
                    key: saved[key].copy()
                    for key in ("urban", "arch", "unknown_coverage", "evidence_building_coverage")
                }
            )
            for key in ("new_development", "redevelopment", "infill"):
                state[key] = np.zeros_like(state["urban"])
            states.append(state)
    return history, states


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_continuation(
    directory, cfg, state, initial, projects, infrastructure, ledger, stable_hashes=None
):
    """Versioned complete-year restart contract; execution of EXTEND is deliberately deferred."""
    directory = Path(directory)
    parameters = simulation_config(cfg)
    parameters.pop("end_year", None)
    files = [f"{state['year']}.npz", "versions.json", "summary.csv"]
    files += [
        name
        for name in ("baseline.npz", "projects.json", "infrastructure.json", "events.json")
        if (directory / name).is_file()
    ]
    stable_hashes = {} if stable_hashes is None else stable_hashes
    if "baseline.npz" in files and "baseline.npz" not in stable_hashes:
        stable_hashes["baseline.npz"] = file_hash(directory / "baseline.npz")
    contract = dict(
        schema_version=1,
        completed_year=state["year"],
        next_year=state["year"] + 1,
        extension_execution_supported=False,
        simulation_parameters=parameters,
        baseline_capacity=initial,
        ledger=ledger,
        active_version_ids=[s["id"] + "@" + str(s["version"]) for s in state["sites"]],
        projects=projects.snapshot(),
        infrastructure=infrastructure.snapshot() if infrastructure else None,
        files={
            name: stable_hashes[name] if name in stable_hashes else file_hash(directory / name)
            for name in files
        },
        requirements=[
            "Load baseline and completed arrays",
            "Restore complete version history and active version IDs",
            "Restore demand/capacity ledger, pending phases, infrastructure pressure and corridor reservations",
            "Rebuild derived feedback from completed projects and opened segments; recreate worker cache",
            "Continue with next_year; prove equivalence against an uninterrupted run before enabling EXTEND",
        ],
    )
    atomic_json(directory / "continuation.json", contract)


def read_continuation(directory):
    directory = Path(directory)
    value = json.loads((directory / "continuation.json").read_text(encoding="utf8"))
    required = {
        "households",
        "jobs",
        "free_housing",
        "free_jobs",
        "unmet_housing",
        "unmet_jobs",
        "cumulative_housing_demand",
        "cumulative_job_demand",
        "cumulative_housing_served",
        "cumulative_jobs_served",
        "cumulative_housing_added",
        "cumulative_jobs_added",
    }
    if value.get("schema_version") != 1 or not required <= value.get("ledger", {}).keys():
        raise ValueError("Incomplete/unsupported continuation ledger.")
    if "baseline.npz" not in value["files"]:
        raise ValueError("Continuation requires a saved authoritative baseline.")
    for name, digest in value["files"].items():
        path = (directory / name).resolve()
        if (
            not path.is_relative_to(directory.resolve())
            or not path.is_file()
            or file_hash(path) != digest
        ):
            raise ValueError("Continuation file missing or changed: " + name)
    manifest = json.loads((directory / "versions.json").read_text(encoding="utf8"))
    ids = {s["id"] + "@" + str(s["version"]) for s in manifest["versions"]}
    if (
        manifest["completed_year"] != value["completed_year"]
        or value["next_year"] != value["completed_year"] + 1
        or not set(value["active_version_ids"]) <= ids
    ):
        raise ValueError("Continuation year/version history is inconsistent.")
    with np.load(directory / f"{value['completed_year']}.npz", allow_pickle=False) as saved:
        state = json.loads(str(saved["payload_json"].item()))
        if state["active_version_ids"] != value["active_version_ids"]:
            raise ValueError("Continuation active sites differ from the checkpoint.")
    return value
