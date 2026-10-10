"""Data validation: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import csv
import gzip
import json
import time
from pathlib import Path

from metrosim26.datasets.files import _file_sha256


def validate_dataset(path, kind):
    if kind == "osm":
        return validate_osm(path)
    if kind == "elevation":
        from metrosim26.geography import elevation_tile

        tile = elevation_tile(path)
        return {"side": tile.shape[0], "source": "Mapzen Terrain Tiles / Skadi"}
    return validate_lodes(path, kind)


def validate_osm(path):
    with Path(path).open(encoding="utf-8") as source:
        payload = json.load(source)
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("elements"), list)
        or payload.get("remark")
    ):
        raise ValueError("Invalid/incomplete Overpass response (missing elements or server remark)")
    for element in payload["elements"]:
        if (
            not isinstance(element, dict)
            or element.get("type") not in {"node", "way", "relation"}
            or not isinstance(element.get("id"), int)
        ):
            raise ValueError("Invalid OSM element type/id")
    metadata = payload.get("osm3s", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid OSM timestamp metadata")
    return {"timestamp_osm_base": metadata.get("timestamp_osm_base")}


def validate_lodes(path, kind):
    required = (
        {"tabblk2020", "blklatdd", "blklondd"}
        if kind == "xwalk"
        else {"w_geocode" if kind == "wac" else "h_geocode", "C000"}
    )
    rows = 0
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Unexpected LODES {kind.upper()} CSV columns")
        for row in reader:  # Read to EOF: includes gzip trailer/CRC validation.
            if None in row or any(row.get(key) is None for key in required):
                raise ValueError("Truncated or malformed LODES CSV row")
            if kind == "xwalk":
                # Coordinate interpretation/invalid-coordinate handling stays in load_lodes.
                if not row["tabblk2020"]:
                    raise ValueError("Missing crosswalk block identifier")
            else:
                if not row["w_geocode" if kind == "wac" else "h_geocode"]:
                    raise ValueError("Missing employment block identifier")
                for key in ("C000", *(f"CNS{i:02d}" for i in range(1, 21))):
                    if key in row:
                        int(row[key] or 0)
            rows += 1
    if not rows:
        raise ValueError("Empty LODES dataset")
    return {"rows": rows}


VALIDATOR_VERSION = 1

_VALIDATED_INPUTS = {}


def _validation_path(path):
    return path.with_name(path.name + ".validation.json")


def _file_identity(path):
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def validate_input(spec, force=False):
    """Validate legacy files too. Receipts skip reparsing only after a hash match."""
    identity = _file_identity(spec.path)
    if not identity[1]:
        raise ValueError("Empty cache file")
    key = (identity, spec.kind, VALIDATOR_VERSION)
    if not force and key in _VALIDATED_INPUTS:
        return dict(_VALIDATED_INPUTS[key])
    receipt = {}
    try:
        receipt = json.loads(_validation_path(spec.path).read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            receipt = {}
    except (OSError, ValueError):
        pass
    digest = _file_sha256(spec.path)
    if (
        not force
        and receipt.get("validator_version") == VALIDATOR_VERSION
        and receipt.get("kind") == spec.kind
        and receipt.get("sha256") == digest
    ):
        record = receipt
    else:
        details = validate_dataset(spec.path, spec.kind)
        # For legacy files the actual downloading endpoint is unknown, not today's setting.
        record = {
            "validator_version": VALIDATOR_VERSION,
            "kind": spec.kind,
            "sha256": digest,
            "size": identity[1],
            "details": details,
            "source_url": receipt.get("source_url") if receipt.get("sha256") == digest else None,
            "validated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    if _file_identity(spec.path) != identity:
        raise ValueError("Cache file changed during validation; retry after acquisition finishes")
    _VALIDATED_INPUTS[key] = dict(record)
    return dict(record)
