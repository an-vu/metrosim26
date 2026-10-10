"""Data cache: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import zlib
from pathlib import Path

from metrosim26.datasets.files import InputFile, atomic_json
from metrosim26.datasets.inputs import expected_inputs
from metrosim26.datasets.validation import validate_input
from metrosim26.runtime import report_progress


def cache_roots(cfg):
    """Search only selected storage and known MetroSim download folders."""
    home = Path.home()
    candidates = [Path(cfg["cache_dir"])]
    if cfg.get("reuse_cache_dir"):
        candidates.append(Path(cfg["reuse_cache_dir"]))
    candidates += [
        home / "Desktop/MetroSim 26 Data/Cache",
        home / "Desktop/metrosim26-cache",
        home / "Documents/Blender_MetroSim26_V3_Cache",
    ]
    return list(dict.fromkeys(path.expanduser().resolve() for path in candidates if path.is_dir()))


def reuse_cached_input(spec, cfg):
    """Copy validated matching geographic inputs; leave source downloads untouched."""
    import shutil

    target_root = Path(cfg["cache_dir"]).resolve()
    relative = spec.path.resolve().relative_to(target_root)
    for root in cache_roots(cfg):
        source = root / relative
        if source.resolve() == spec.path.resolve():
            continue
        try:
            validate_input(InputFile(spec.label, spec.kind, source, spec.url, query=spec.query))
            spec.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=spec.path.parent, delete=False) as temp:
                temporary = Path(temp.name)
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, spec.path)
            finally:
                temporary.unlink(missing_ok=True)
            return True
        except (OSError, ValueError, EOFError, csv.Error, zlib.error):
            continue
    if spec.kind == "osm":
        for root in cache_roots(cfg):
            if reuse_legacy_tile(spec, dict(cfg, cache_dir=str(root))):
                return True
        if spec.path.parent.name == "Coast":
            target = cfg["bounds"]
            for root in cache_roots(cfg):
                for path in root.glob("input_manifest_*.json"):
                    try:
                        manifest = json.loads(path.read_text())
                        bounds = manifest["bounds"]
                        if manifest.get("osm_query_version") != cfg[
                            "osm_query_version"
                        ] or manifest.get("osm_data_snapshot") != cfg.get("osm_data_snapshot"):
                            continue
                        if not (
                            bounds[0] <= target[0]
                            and bounds[1] <= target[1]
                            and bounds[2] >= target[2]
                            and bounds[3] >= target[3]
                        ):
                            continue
                        from metrosim26.geography import inputs

                        sources = {
                            str(item.path.relative_to(root)): item
                            for item in inputs(
                                dict(
                                    cfg,
                                    cache_dir=str(root),
                                    bounds=bounds,
                                    include_terrain=False,
                                    include_ocean=True,
                                )
                            )
                        }
                        for entry in manifest["files"]:
                            source = sources.get(entry["file"])
                            if source is None:
                                continue
                            validate_input(source)
                            spec.path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_json(spec.path, json.loads(source.path.read_text()))
                            return True
                    except (OSError, ValueError, KeyError, TypeError, EOFError):
                        continue
    return False


def discover_cached_inputs(cfg):
    """Validate local coverage and import missing files without network requests."""
    ready = reused = 0
    specs = expected_inputs(cfg)
    for spec in specs:
        try:
            validate_input(spec)
        except (OSError, ValueError, EOFError, csv.Error, zlib.error):
            if not reuse_cached_input(spec, cfg):
                continue
            reused += 1
        ready += 1
    return {"ready": ready, "reused": reused, "missing": len(specs) - ready}


def reuse_legacy_tile(spec, cfg):
    """Reuse a validated old tile containing an entire fixed cell, never partial coverage."""
    if not spec.path.name.startswith("tile_"):
        return False
    _, y, x, _ = spec.path.stem.split("_", 3)
    y, x = int(y), int(x)
    target = [y * 0.025 - 90, x * 0.025 - 180, (y + 1) * 0.025 - 90, (x + 1) * 0.025 - 180]
    for manifest_path in Path(cfg["cache_dir"]).glob("input_manifest_*.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("osm_query_version") != cfg["osm_query_version"] or manifest.get(
                "osm_data_snapshot"
            ) != cfg.get("osm_data_snapshot"):
                continue
            south, west, north, east = manifest["bounds"]
            tx, ty = manifest["osm_tiles"]
            for entry in manifest["files"]:
                source = Path(cfg["cache_dir"]) / entry["file"]
                parts = source.stem.split("_")
                if len(parts) != 4 or parts[0] != "osm":
                    continue
                iy, ix = int(parts[1]), int(parts[2])
                bounds = [
                    south + (north - south) * iy / ty,
                    west + (east - west) * ix / tx,
                    south + (north - south) * (iy + 1) / ty,
                    west + (east - west) * (ix + 1) / tx,
                ]
                if not (
                    bounds[0] <= target[0] + 1e-8
                    and bounds[1] <= target[1] + 1e-8
                    and bounds[2] >= target[2] - 1e-8
                    and bounds[3] >= target[3] - 1e-8
                ):
                    continue
                validate_input(InputFile("Legacy tile", "osm", source, spec.url))
                spec.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(spec.path, json.loads(source.read_text()))
                print(f"  {spec.label}: reused containing legacy tile", flush=True)
                return True
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            continue
    return False


def inspect_cache(cfg, force=False):
    valid, problems = {}, []
    specs = expected_inputs(cfg)
    # Inventory all missing files before parsing/hash-reading any large input.
    for spec in specs:
        if not spec.path.is_file():
            problems.append((spec, "missing"))
    for spec in specs:
        if not spec.path.is_file():
            continue
        try:
            print(f"  validating {spec.label}", flush=True)
            valid[str(spec.path)] = validate_input(spec, force=force)
        except (OSError, ValueError, EOFError, csv.Error, zlib.error) as exc:
            problems.append((spec, str(exc)))
    return specs, valid, problems


def require_input_cache(cfg):
    """Fail fast on missing files; otherwise report all validation failures. No HTTP."""
    print("Checking local MetroSim26 input cache (no network)...", flush=True)
    report_progress("validating_cache", "Validating input cache")
    specs = expected_inputs(cfg)
    problems = [(spec, "missing") for spec in specs if not spec.path.is_file()]
    if not problems:
        _, _, problems = inspect_cache(cfg)
    if problems:
        details = "\n".join(f"- {spec.label}: {reason}\n  {spec.path}" for spec, reason in problems)
        mode = "--preview" if cfg["preview_mode"] else "--full"
        raise FileNotFoundError(
            "Required MetroSim26 input cache is incomplete.\n\n"
            + details
            + f"\n\nRun scripts/prefetch_data.py {mode} outside Blender first. "
            "Previously completed files will be reused."
        )
    print("Local input cache validated.", flush=True)
