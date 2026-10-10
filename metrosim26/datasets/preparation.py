"""Data preparation: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import csv
import hashlib
import re
import zlib
from pathlib import Path

from metrosim26.datasets.download import _download_cached
from metrosim26.datasets.files import _data_signature, atomic_json
from metrosim26.datasets.inputs import expected_inputs
from metrosim26.datasets.validation import _validation_path, validate_input
from metrosim26.runtime import report_progress


def cache_manifest_path(cfg):
    identity = [str(spec.path.relative_to(Path(cfg["cache_dir"]))) for spec in expected_inputs(cfg)]
    return Path(cfg["cache_dir"]) / f"input_manifest_{_data_signature(identity)}.json"


def _write_cache_manifest(cfg, specs, valid, problems):
    atomic_json(
        cache_manifest_path(cfg),
        {
            "schema_version": 1,
            "complete": not problems and len(valid) == len(specs),
            "bounds": cfg["bounds"],
            "osm_tiles": cfg["osm_tiles"],
            "osm_query_version": cfg["osm_query_version"],
            "osm_data_snapshot": cfg.get("osm_data_snapshot"),
            "lodes_year": cfg["lodes_year"],
            "lodes_version": cfg["lodes_version"],
            "lodes_vintage": cfg["lodes_vintage"],
            "files": [
                {
                    "label": spec.label,
                    "file": str(spec.path.relative_to(Path(cfg["cache_dir"]))),
                    "query_sha256": hashlib.sha256(spec.query.encode()).hexdigest()
                    if spec.query
                    else None,
                    "validation": valid.get(str(spec.path)),
                }
                for spec in specs
            ],
            "problems": [{"label": spec.label, "reason": reason} for spec, reason in problems],
        },
    )

    name = re.sub(r"[^\w -]", "", cfg.get("location_name", "")).strip()[:60]
    if name:
        atomic_json(
            Path(cfg["cache_dir"]) / "Locations" / name / "area.json",
            {
                "name": cfg["location_name"],
                "center": cfg.get("study_center"),
                "radius_km": cfg.get("study_radius_km"),
                "manifest": str(cache_manifest_path(cfg)),
                "note": "Downloaded cells are shared in Cache/osm; this file identifies the named area.",
            },
        )


def print_cache_report(cfg, specs, valid, problems):
    print("\n================================================\nMETROSIM26 DATA CACHE")
    print(f"Study area bounds (S, W, N, E): {cfg['bounds']}")
    osm = [spec for spec in specs if spec.kind == "osm"]
    print(f"OSM tiles: {sum(str(spec.path) in valid for spec in osm)} / {len(osm)} valid")
    for state in sorted({spec.state for spec in specs if spec.state}):
        files = [spec for spec in specs if spec.state == state]
        ready = all(str(spec.path) in valid for spec in files)
        print(f"{state.upper()} LODES: {'complete' if ready else 'incomplete'}")
    if problems:
        print("\nCACHE INCOMPLETE\nMissing/invalid:")
        for spec, reason in problems:
            print(f"- {spec.label}: {reason}\n  {spec.path}")
        print(
            "Re-run scripts/prefetch_data.py with the same options. Completed files will be reused."
        )
    else:
        print("\nCACHE READY FOR BLENDER")
    print("================================================", flush=True)


def prefetch_data(cfg, check_only=False):
    """All acquisition is per-file resumable; this never prepares a baseline."""
    online = dict(cfg, data_mode="ONLINE")
    specs = expected_inputs(online)
    valid, failures = {}, {}
    try:
        for index, spec in enumerate(specs, 1):
            prefix = f"[{index:02d}/{len(specs)}] {spec.label}"
            report_progress(
                "downloading",
                f"Checking {index}/{len(specs)} · Ready {len(valid)}/{len(specs)}",
                download_progress=len(valid) / len(specs),
            )
            try:
                record = validate_input(spec, force=check_only)
                print(prefix + ": cached / valid", flush=True)
                if not check_only:
                    atomic_json(_validation_path(spec.path), record)
            except (OSError, ValueError, EOFError, csv.Error, zlib.error) as exc:
                if check_only:
                    failures[str(spec.path)] = str(exc)
                    print(prefix + ": missing / invalid", flush=True)
                    continue
                print(prefix + ": downloading...", flush=True)
                try:
                    record = _download_cached(spec, online)
                except RuntimeError as exc:
                    failures[str(spec.path)] = str(exc)
                    continue
            valid[str(spec.path)] = record
            if not check_only:
                pending = [
                    (item, failures.get(str(item.path), "not yet validated"))
                    for item in specs
                    if str(item.path) not in valid
                ]
                _write_cache_manifest(cfg, specs, valid, pending)
    except KeyboardInterrupt:
        print("\nPrefetch interrupted; completed files are retained.", flush=True)
        raise
    finally:
        problems = [
            (spec, failures.get(str(spec.path), "not yet validated"))
            for spec in specs
            if str(spec.path) not in valid
        ]
        if not check_only:
            _write_cache_manifest(cfg, specs, valid, problems)
        print_cache_report(cfg, specs, valid, problems)
    cfg["download_failure_summary"] = (
        f"Ready {len(valid)}/{len(specs)} files. {len(problems)} failed. "
        + (problems[0][1] if problems else "")
    )
    return not problems
