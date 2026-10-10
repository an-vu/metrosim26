"""Consolidate shared downloads and provide a readable saved-area catalogue."""

import csv
import json
import os
import re
import shutil
import tempfile
import zlib
from pathlib import Path
from urllib.parse import quote


def consolidate(cfg):
    from metrosim26.data import InputFile, atomic_json, cache_roots, validate_input

    destination = Path(cfg["cache_dir"]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    roots = cache_roots(cfg)
    copied = invalid = 0
    groups = {}

    def remember(record, source):
        center = record.get("center") or record.get("study_center")
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            return
        try:
            key = tuple(round(float(value), 5) for value in center)
        except (ValueError, TypeError):
            return
        group = groups.setdefault(key, {"names": set(), "paths": set(), "radii": set()})
        name = record.get("location_name") or record.get("name") or record.get("location_label")
        if name:
            group["names"].add(str(name))
        group["paths"].add(str(source.resolve()))
        radius = record.get("radius_km") or record.get("study_radius_km")
        if radius:
            group["radii"].add(str(radius))

    remember(cfg, destination)
    for cache in roots:
        for folder, pattern, kind in (
            ("osm", "*.json", "osm"),
            ("Coast", "*.json", "osm"),
            ("Terrain", "*.hgt.gz", "elevation"),
        ):
            for source in (cache / folder).glob(pattern):
                if source.is_symlink() or source.name.endswith(".validation.json"):
                    continue
                if folder == "osm" and not source.name.startswith(("tile_", "osm_")):
                    continue
                target = destination / folder / source.name
                if source.resolve() == target.resolve():
                    continue
                try:
                    validate_input(InputFile(source.name, kind, source, ""))
                    try:
                        validate_input(InputFile(target.name, kind, target, ""))
                        continue
                    except (OSError, ValueError, EOFError, csv.Error, zlib.error):
                        pass
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                        temporary = Path(temp.name)
                    try:
                        shutil.copyfile(source, temporary)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    copied += 1
                except (OSError, ValueError, EOFError, csv.Error, zlib.error):
                    invalid += 1
        for area in (cache / "Locations").glob("*/area.json"):
            try:
                remember(json.loads(area.read_text()), area.parent)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    output = Path(cfg["output_dir"])
    for config in output.glob("*/jobs/*/config.json"):
        try:
            record = json.loads(config.read_text())
            if str(record.get("model_version", "")).startswith("metrosim26-"):
                remember(record, config.parent)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    # Retain prior catalogue entries when an old source folder is disconnected.
    catalogue = destination / "Saved Areas" / "catalogue.json"
    try:
        previous = json.loads(catalogue.read_text())
        for entry in previous:
            for name in entry["names"]:
                remember(dict(center=entry["center"], name=name), Path(entry["paths"][0]))
            group = groups[tuple(entry["center"])]
            group["paths"].update(entry["paths"])
            group["radii"].update(entry["radii"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass
    entries = []
    index = [
        "# Saved areas",
        "",
        "- Downloads are shared by coordinates, regardless of names.",
        "- Consolidation copies valid downloads; original folders stay in place.",
        "- Different centers remain separate entries but reuse overlapping tiles.",
        "",
    ]
    for center, group in sorted(groups.items()):
        names = sorted(group["names"]) or ["Unnamed area"]
        label = names[0]
        slug = re.sub(r"[^\w -]", "", label).strip()[:60] or "Unnamed area"
        folder = f"{slug} ({center[1]:.5f}, {center[0]:.5f})"
        note = destination / "Saved Areas" / folder / "README.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {label}",
            "",
            f"- Latitude: {center[1]:.5f}",
            f"- Longitude: {center[0]:.5f}",
            f"- Also named: {', '.join(names)}",
            f"- Saved radii (km): {', '.join(sorted(group['radii'])) or 'Unknown'}",
            "- Shared downloads: ../../osm, ../../Terrain, ../../Coast",
            "",
            "Saved folders:",
            "",
        ]
        for path in sorted(group["paths"]):
            lines.append(f"- [{Path(path).name}]({quote(Path(path).as_uri(), safe=':/')})")
        note.write_text("\n".join(lines) + "\n")
        index.append(f"- [{label}]({quote(folder)}/README.md) — {center[1]:.5f}, {center[0]:.5f}")
        entries.append(
            dict(
                center=list(center),
                names=names,
                paths=sorted(group["paths"]),
                radii=sorted(group["radii"]),
            )
        )
    atomic_json(catalogue, entries)
    (catalogue.parent / "README.md").write_text("\n".join(index) + "\n")
    return {"copied": copied, "invalid": invalid, "areas": len(entries)}
