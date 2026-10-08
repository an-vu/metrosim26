"""Shared data acquisition, geography parsing, and baseline preparation. No Blender imports."""

from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import json
import math
import multiprocessing
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np

from metrosim26.config import (
    ARCH_NAMES,
    COMMERCIAL,
    EMPTY,
    HIGHRISE,
    INDUSTRIAL,
    MIXED,
    OFFICE,
    RETAIL,
    SUBURBAN,
    URBAN_RES,
)
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    _bbox,
    _boxes_overlap,
    _json_default,
    point_in_ring,
    polygon_area,
    prepare_spatial_index,
)


def replace_checkpoint(temporary, destination):
    """Allow brief Windows reader/scanner locks without losing a completed checkpoint."""
    for attempt in range(5):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * 2**attempt)


def atomic_json(path, data, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf8") as out:
        if compact:
            # dumps uses the C encoder for unindented JSON; schema/content unchanged.
            out.write(
                json.dumps(
                    data,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=_json_default,
                    allow_nan=False,
                )
            )
        else:
            json.dump(data, out, sort_keys=True, indent=2, default=_json_default, allow_nan=False)
        out.write("\n")
    replace_checkpoint(tmp, path)


def atomic_npz(path, **arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as out:
        np.savez_compressed(out, **arrays)
    replace_checkpoint(tmp, path)


def _data_signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class InputFile:
    """One canonical expected input; transport URL is not its cache identity."""

    label: str
    kind: str
    path: Path
    url: str
    state: str = ""
    query: str = ""
    tile_id: int = -1


def osm_inputs(cfg):
    south, west, north, east = map(float, cfg["bounds"])
    tx, ty = cfg["osm_tiles"]
    result = []
    for iy in range(ty):
        for ix in range(tx):
            bounds = [
                south + (north - south) * iy / ty,
                west + (east - west) * ix / tx,
                south + (north - south) * (iy + 1) / ty,
                west + (east - west) * (ix + 1) / tx,
            ]
            query = _osm_query(bounds, cfg.get("osm_data_snapshot"))
            signature = _data_signature(
                {
                    "study_bounds": cfg["bounds"],
                    "tile": bounds,
                    "query": query,
                    "version": cfg["osm_query_version"],
                }
            )
            path = Path(cfg["cache_dir"]) / "osm" / f"osm_{iy:02d}_{ix:02d}_{signature}.json"
            result.append(
                InputFile(
                    f"OSM tile {iy * tx + ix + 1} ({iy + 1},{ix + 1})",
                    "osm",
                    path,
                    cfg["overpass_url"],
                    query=query,
                    tile_id=iy * tx + ix,
                )
            )
    return result


def lodes_input(cfg, state, kind):
    if kind not in {"xwalk", "wac", "rac"}:
        raise ValueError(f"Unknown LODES file kind: {kind}")
    base = f"https://lehd.ces.census.gov/data/lodes/{cfg['lodes_version']}/{state}"
    filename = (
        f"{state}_xwalk.csv.gz"
        if kind == "xwalk"
        else f"{state}_{kind}_S000_{cfg.get('lodes_job_type', 'JT00')}_{cfg['lodes_year']}.csv.gz"
    )
    url = base + "/" + (kind + "/" if kind != "xwalk" else "") + filename
    signature = _data_signature(
        {
            "url": url,
            "version": cfg["lodes_version"],
            "year": cfg["lodes_year"],
            "vintage": cfg.get("lodes_vintage", "cached-release"),
        }
    )
    path = Path(cfg["cache_dir"]) / "lodes" / f"{signature}_{filename}"
    return InputFile(
        f"{state.upper()} {kind.upper()} {cfg['lodes_year']}", kind, path, url, state=state
    )


def expected_inputs(cfg):
    """The same ordered input inventory is used by prefetch and baseline loading."""
    return osm_inputs(cfg) + [
        lodes_input(cfg, state, kind)
        for state in sorted(cfg.get("lodes_states", ["ne", "ia"]))
        for kind in ("xwalk", "wac", "rac")
    ]


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
# Reuse validation within one process only when the file's filesystem identity is unchanged.
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
        details = (
            validate_osm(spec.path) if spec.kind == "osm" else validate_lodes(spec.path, spec.kind)
        )
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


def _retry_delay(error, attempt, cfg):
    value = getattr(error, "retry_after", None)
    if value is None and isinstance(error, urllib.error.HTTPError) and error.headers:
        value = error.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(
        float(cfg.get("download_backoff_max", 300)),
        float(cfg.get("download_backoff_initial", 30)) * 2**attempt,
    )


class DownloadAttemptError(OSError):
    """A serializable HTTP-attempt failure reported by a disposable process."""

    def __init__(self, message, *, error_type="OSError", http_code=None, retry_after=None):
        super().__init__(message)
        self.error_type = error_type
        self.http_code = http_code
        self.retry_after = retry_after


def _download_once(endpoint, post_data, temp_path, socket_timeout):
    request = urllib.request.Request(
        endpoint,
        data=post_data,
        headers={"User-Agent": "MetroSim26/3 (local research visualization)"},
    )
    with urllib.request.urlopen(request, timeout=socket_timeout) as response:
        source_url = response.geturl()
        with open(temp_path, "wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
    return source_url


def _download_attempt_worker(endpoint, post_data, temp_path, socket_timeout, sender):
    """Child target: its process can be terminated even during a blocked socket read."""
    try:
        source_url = _download_once(endpoint, post_data, temp_path, socket_timeout)
        sender.send({"ok": True, "source_url": source_url})
    except BaseException as exc:
        headers = getattr(exc, "headers", None)
        sender.send(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "http_code": getattr(exc, "code", None),
                "retry_after": headers.get("Retry-After") if headers else None,
            }
        )
    finally:
        sender.close()


def _download_attempt_with_deadline(endpoint, post_data, temp_path, cfg):
    socket_timeout = float(cfg.get("download_timeout", 240))
    hard_timeout = float(cfg.get("download_attempt_timeout", 300))
    if hard_timeout <= 0:  # Used only by isolated unit tests with mocked HTTP responses.
        return _download_once(endpoint, post_data, temp_path, socket_timeout)

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_download_attempt_worker,
        args=(endpoint, post_data, str(temp_path), socket_timeout, sender),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        process.join(hard_timeout)
    except BaseException:
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join()
        receiver.close()
        process.close()
        raise
    if process.is_alive():
        process.terminate()
        process.join(10)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join()
        receiver.close()
        process.close()
        raise DownloadAttemptError(
            f"hard wall-clock timeout after {hard_timeout:g} seconds",
            error_type="HardTimeout",
        )
    exit_code = process.exitcode
    try:
        result = receiver.recv() if receiver.poll() else None
    finally:
        receiver.close()
        process.close()
    if not result:
        raise DownloadAttemptError(
            f"download process exited without a result (exit code {exit_code})",
            error_type="ChildProcessError",
        )
    if not result["ok"]:
        raise DownloadAttemptError(
            result["message"],
            error_type=result["error_type"],
            http_code=result["http_code"],
            retry_after=result["retry_after"],
        )
    return result["source_url"]


def _endpoint_name(url):
    return urllib.parse.urlparse(url).netloc or url


def _attempt_error_text(error):
    code = getattr(error, "http_code", None) or getattr(error, "code", None)
    return (
        f"HTTP {code}" if code else f"{getattr(error, 'error_type', type(error).__name__)}: {error}"
    )


def _download_cached(spec, cfg):
    """Sequential, bounded retries with atomic per-file publication and receipts."""
    try:
        record = validate_input(spec)
        # Register valid old caches without redownloading; endpoint remains unknown.
        if cfg.get("data_mode", "OFFLINE").upper() == "ONLINE":
            atomic_json(_validation_path(spec.path), record)
        return record
    except (OSError, ValueError, EOFError, csv.Error, zlib.error):
        pass
    if cfg.get("data_mode", "OFFLINE").upper() != "ONLINE":
        raise FileNotFoundError(
            f"{spec.label} is missing/invalid: {spec.path}. "
            "Run scripts/prefetch_data.py outside Blender first."
        )
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    attempts = int(cfg.get("download_attempts", 3))
    endpoints = (
        [spec.url] + list(cfg.get("overpass_fallback_urls", []))
        if spec.kind == "osm"
        else [spec.url]
    )
    last_error = None
    for attempt in range(attempts):
        temp_path = None
        endpoint = endpoints[attempt % len(endpoints)]
        endpoint_name = _endpoint_name(endpoint)
        started = time.monotonic()
        try:
            print(f"  attempt {attempt + 1}/{attempts} -> {endpoint_name}", flush=True)
            post = urllib.parse.urlencode({"data": spec.query}).encode() if spec.query else None
            with tempfile.NamedTemporaryFile(
                prefix=spec.path.name + ".", suffix=".part", dir=spec.path.parent, delete=False
            ) as handle:
                temp_path = Path(handle.name)
            source_url = _download_attempt_with_deadline(endpoint, post, temp_path, cfg)
            details = (
                validate_osm(temp_path)
                if spec.kind == "osm"
                else validate_lodes(temp_path, spec.kind)
            )
            record = {
                "validator_version": VALIDATOR_VERSION,
                "kind": spec.kind,
                "sha256": _file_sha256(temp_path),
                "size": temp_path.stat().st_size,
                "details": details,
                "source_url": source_url,
                "requested_url": endpoint,
                "validated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            os.replace(temp_path, spec.path)
            atomic_json(_validation_path(spec.path), record)
            _VALIDATED_INPUTS[(_file_identity(spec.path), spec.kind, VALIDATOR_VERSION)] = dict(
                record
            )
            elapsed = time.monotonic() - started
            size_mb = record["size"] / (1024 * 1024)
            print(
                f"  {endpoint_name} -> SUCCESS — {size_mb:.1f} MB — {elapsed:.1f} s",
                flush=True,
            )
            return record
        except (
            OSError,
            ValueError,
            EOFError,
            csv.Error,
            zlib.error,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
            elapsed = time.monotonic() - started
            print(f"  {endpoint_name} -> {_attempt_error_text(exc)} — {elapsed:.1f} s", flush=True)
            http_code = getattr(exc, "http_code", None) or getattr(exc, "code", None)
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if http_code is not None and http_code not in {408, 429, 500, 502, 503, 504}:
                break
            if attempt + 1 < attempts:
                delay = _retry_delay(exc, attempt, cfg)
                print(f"  retrying in {delay:g} seconds (prefetch only)", flush=True)
                time.sleep(delay)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    raise RuntimeError(f"Data acquisition failed for {spec.label}: {last_error}") from last_error


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


def print_cache_report(cfg, specs, valid, problems):
    print("\n================================================\nMETROSIM26 DATA CACHE")
    print("Study area: " + ("Omaha preview" if cfg["preview_mode"] else "Omaha / Council Bluffs"))
    osm = [spec for spec in specs if spec.kind == "osm"]
    print(f"OSM tiles: {sum(str(spec.path) in valid for spec in osm)} / {len(osm)} valid")
    for state in sorted(cfg.get("lodes_states", ["ne", "ia"])):
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
    return not problems


def _osm_query(bounds, snapshot=None):
    south, west, north, east = bounds
    bbox = f"({south:.8f},{west:.8f},{north:.8f},{east:.8f})"
    # Recursive down (>>) also includes nested relation members and way nodes.
    # Relations intersect the bbox through their members; no out-center geometry.
    selectors = [
        '["building"]',
        '["highway"]',
        '["landuse"]',
        '["natural"="water"]',
        '["water"]',
        '["waterway"="riverbank"]',
        '["leisure"]',
        '["boundary"="protected_area"]',
        '["amenity"]',
        '["shop"]',
        '["office"]',
    ]
    body = "".join(f"nwr{selector}{bbox};" for selector in selectors)
    body += f'relation["type"="building"]{bbox};'
    date_setting = "[date:" + json.dumps(str(snapshot)) + "]" if snapshot else ""
    return "[out:json][timeout:210]" + date_setting + ";(" + body + ");(._;>>;);out body;"


def load_osm(cfg, grid):
    specs = osm_inputs(cfg)
    tasks = [(spec.tile_id, spec.path, spec.query) for spec in specs]
    records = {spec.tile_id: _download_cached(spec, cfg) for spec in specs}
    print(f"OSM: loading {len(tasks)} deterministically ordered local tiles.")
    merged, provenance, conflicts = {}, [], 0
    for tile_id, path, query in sorted(tasks):
        report_progress("loading_osm", f"Loading OSM tile {tile_id + 1} / {len(tasks)}")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("remark") or "elements" not in payload:
            raise RuntimeError(f"Invalid/incomplete cached OSM tile: {path}")
        provenance.append(
            {
                "tile": tile_id,
                "cache_file": str(path),
                "sha256": records[tile_id]["sha256"],
                "timestamp_osm_base": payload.get("osm3s", {}).get("timestamp_osm_base"),
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "source_url": records[tile_id].get("source_url"),
            }
        )
        for element in payload["elements"]:
            key = (element["type"], int(element["id"]))
            previous = merged.get(key)
            if previous is None:
                merged[key] = element
            elif previous != element:
                conflicts += 1

                # Overlapping live tiles can differ. Prefer higher version if given,
                # otherwise choose by canonical content, not response arrival time.
                def rank(e):
                    return (
                        int(e.get("version", 0)),
                        json.dumps(e, sort_keys=True, separators=(",", ":")),
                    )

                if rank(element) > rank(previous):
                    merged[key] = element
    elements = [merged[key] for key in sorted(merged)]
    return elements, {
        "tiles": provenance,
        "query_version": cfg["osm_query_version"],
        "requested_snapshot": cfg.get("osm_data_snapshot"),
        "overlapping_element_conflicts": conflicts,
        "attribution": "© OpenStreetMap contributors, ODbL",
        "license_url": "https://www.openstreetmap.org/copyright",
    }


def _land_kind(tags):
    landuse, amenity = tags.get("landuse", ""), tags.get("amenity", "")
    if (
        tags.get("natural") == "water"
        or "water" in tags
        or tags.get("waterway") == "riverbank"
        or landuse in ("reservoir", "basin")
    ):
        return "water"
    if (
        tags.get("leisure")
        in ("park", "nature_reserve", "golf_course", "recreation_ground", "garden", "pitch")
        or tags.get("boundary") == "protected_area"
        or landuse in ("cemetery", "recreation_ground", "village_green", "conservation")
    ):
        return "park"
    if landuse == "residential":
        return "residential"
    if landuse == "industrial":
        return "industrial"
    if landuse == "retail":
        return "retail"
    if landuse == "commercial":
        return "commercial"
    if landuse in ("institutional", "education", "religious", "military") or amenity in (
        "university",
        "college",
        "school",
        "hospital",
        "research_institute",
        "place_of_worship",
    ):
        return "institutional"
    return None


def _building_arch(tags, height_m=0.0):
    building = tags.get("building", "")
    if building in ("industrial", "warehouse", "manufacture", "storage_tank", "hangar"):
        return INDUSTRIAL
    if building in ("retail", "supermarket", "kiosk") or tags.get("shop"):
        return RETAIL
    if building in (
        "office",
        "school",
        "university",
        "hospital",
        "civic",
        "public",
        "college",
        "church",
    ) or tags.get("office"):
        return OFFICE
    if building in ("commercial", "hotel"):
        return COMMERCIAL
    if height_m >= 45:
        return HIGHRISE
    if building in ("apartments", "dormitory"):
        return URBAN_RES
    if building in (
        "house",
        "detached",
        "semidetached_house",
        "terrace",
        "residential",
        "bungalow",
    ):
        return SUBURBAN
    return EMPTY  # Infer untagged generic buildings from district signals later.


def _tag_number(value, default=0.0):
    if value is None:
        return default
    text = str(value).strip().lower().replace(",", ".")
    number = ""
    for char in text:
        if char in "+-.0123456789":
            number += char
        elif number:
            break
    try:
        result = float(number)
        if not math.isfinite(result):
            return default
        return result * 0.3048 if ("ft" in text or "'" in text) else result
    except ValueError:
        return default


def _building_height(tags):
    # Measured/tagged heights are real input. Missing heights are explicitly
    # estimated masses, deterministic by building use (not observed measurements).
    if tags.get("height"):
        return max(2.5, min(400.0, _tag_number(tags["height"], 9.0))), "OSM height"
    if tags.get("building:levels"):
        return max(
            2.5, min(400.0, _tag_number(tags["building:levels"], 3.0) * 3.2)
        ), "OSM levels × assumed 3.2m"
    defaults = {
        "house": 6.5,
        "detached": 6.5,
        "garage": 3.0,
        "garages": 3.0,
        "apartments": 13.0,
        "industrial": 9.0,
        "warehouse": 10.0,
        "retail": 6.0,
        "office": 15.0,
        "church": 12.0,
    }
    return defaults.get(tags.get("building"), 8.0), "assumed missing height"


def _join_member_rings(members, ways):
    """Join unordered/reversed way chains by endpoint node IDs deterministically."""
    remaining = {
        wid: list(ways[wid].get("nodes", []))
        for wid in sorted(set(members))
        if wid in ways and len(ways[wid].get("nodes", [])) >= 2
    }
    rings, consumed = [], set()
    incomplete = sum(wid not in ways for wid in set(members))
    while remaining:
        first = min(remaining)
        chain, used = remaining.pop(first), {first}
        while chain[0] != chain[-1]:
            matches = [wid for wid, seq in remaining.items() if chain[-1] in (seq[0], seq[-1])]
            if not matches:
                break
            wid = min(matches)
            segment = remaining.pop(wid)
            if segment[-1] == chain[-1]:
                segment.reverse()
            chain.extend(segment[1:])
            used.add(wid)
        if chain[0] == chain[-1] and len(chain) >= 4:
            rings.append(chain[:-1])
            consumed.update(used)
        else:
            incomplete += 1
    return rings, consumed, incomplete


def parse_osm(elements, grid):
    nodes = {int(e["id"]): e for e in elements if e["type"] == "node"}
    ways = {int(e["id"]): e for e in elements if e["type"] == "way"}
    relations = {int(e["id"]): e for e in elements if e["type"] == "relation"}
    xy = {
        nid: grid.project(n["lon"], n["lat"])
        for nid, n in nodes.items()
        if "lon" in n and "lat" in n
    }
    buildings, land, roads, pois = [], [], [], []
    diagnostics = {
        "incomplete_relation_chains": 0,
        "missing_ring_nodes": 0,
        "orphan_relation_holes": 0,
        "relation_member_duplicates_avoided": 0,
        "estimated_building_heights": 0,
        "sites_without_area_boundary": 0,
        "building_relations_without_outline": 0,
    }
    study_box = (grid.min_x, grid.min_y, grid.max_x, grid.max_y)
    suppressed = {"building": set(), "land": set(), "poi": set()}
    suppressed_relations = {"building": set(), "land": set(), "poi": set()}

    def ring_coordinates(ids):
        if not all(nid in xy for nid in ids):
            diagnostics["missing_ring_nodes"] += 1
            return None
        points = [xy[nid] for nid in ids]
        points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        return points if len(points) >= 3 and polygon_area(points) > 1e-12 else None

    def members_of(rid, parity=False, seen=None):
        seen = set() if seen is None else seen
        if rid in seen:
            return []
        seen = seen | {rid}
        result = []
        for m in sorted(
            relations[rid].get("members", []),
            key=lambda m: (m["type"], m["ref"], m.get("role", "")),
        ):
            role = m.get("role", "")
            relation_type = relations[rid].get("tags", {}).get("type")
            if relation_type == "building" and role != "outline":
                continue  # Preserve overall footprint; do not double-extrude 3D parts.
            if relation_type == "site" and role != "perimeter":
                continue  # Disconnected members do not imply a filled campus polygon.
            if role not in ("", "outer", "inner", "outline", "perimeter"):
                continue
            inner = parity != (role == "inner")
            if m["type"] == "way":
                result.append((int(m["ref"]), inner))
            elif m["type"] == "relation" and int(m["ref"]) in relations:
                result.extend(members_of(int(m["ref"]), inner, seen))
        return result

    def descendant_relations(rid, seen=None):
        seen = {rid} if seen is None else seen | {rid}
        result = set()
        for member in relations[rid].get("members", []):
            child = int(member["ref"])
            if member["type"] == "relation" and child in relations and child not in seen:
                result.add(child)
                result.update(descendant_relations(child, seen))
        return result

    def record(fid, tags, polygons):
        polygons = [p for p in polygons if _boxes_overlap(_bbox(p["outer"]), study_box)]
        if not polygons:
            return
        if tags.get("building") not in (None, "no"):
            height, source = _building_height(tags)
            diagnostics["estimated_building_heights"] += int(source == "assumed missing height")
            buildings.append(
                {
                    "id": fid,
                    "polygons": polygons,
                    "height_m": height,
                    "height_source": source,
                    "arch": _building_arch(tags, height),
                    "tags": tags,
                }
            )
        kind = _land_kind(tags)
        if kind:
            land.append({"id": fid, "kind": kind, "polygons": polygons, "tags": tags})
        if any(tags.get(k) for k in ("shop", "office", "amenity")):
            box = _bbox(polygons[0]["outer"])
            pois.append(
                {
                    "id": fid,
                    "point": [(box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5],
                    "tags": tags,
                }
            )

    for rid in sorted(relations):
        relation = relations[rid]
        tags = dict(relation.get("tags", {}))
        relation_type = tags.get("type")
        if relation_type not in ("multipolygon", "boundary", "building", "site"):
            continue
        members = members_of(rid)
        if relation_type == "site" and not members:
            # A site can still supply a POI even when it has no mapped perimeter.
            diagnostics["sites_without_area_boundary"] += 1
            points = []
            for member in sorted(relation.get("members", []), key=lambda m: (m["type"], m["ref"])):
                if member["type"] == "node" and member["ref"] in xy:
                    points.append(xy[member["ref"]])
                elif member["type"] == "way" and member["ref"] in ways:
                    points.extend(xy[n] for n in ways[member["ref"]].get("nodes", []) if n in xy)
            if points and any(tags.get(k) for k in ("shop", "office", "amenity")):
                point = [sum(p[i] for p in points) / len(points) for i in (0, 1)]
                if grid.index(*point) is not None:
                    pois.append({"id": f"relation/{rid}", "point": point, "tags": tags})
                    for member in relation.get("members", []):
                        if member["type"] == "way" and member["ref"] in ways:
                            member_tags = ways[member["ref"]].get("tags", {})
                            if any(
                                tags.get(k) and tags[k] == member_tags.get(k)
                                for k in ("shop", "office", "amenity")
                            ):
                                suppressed["poi"].add(member["ref"])
            continue
        if relation_type == "building" and not members:
            diagnostics["building_relations_without_outline"] += 1
            continue
        outer_ids = [wid for wid, inner in members if not inner]
        inner_ids = [wid for wid, inner in members if inner]
        # Legacy multipolygons may put feature tags on their outer member.
        if not (_land_kind(tags) or tags.get("building")):
            candidate_tags = [ways[wid].get("tags", {}) for wid in outer_ids if wid in ways]
            candidate_tags.extend(
                relations[m["ref"]].get("tags", {})
                for m in relation.get("members", [])
                if m["type"] == "relation" and m.get("role") == "outline" and m["ref"] in relations
            )
            for inherited in candidate_tags:
                if _land_kind(inherited) or inherited.get("building"):
                    tags = {**inherited, **tags}
                    break
        if relation_type == "building":
            tags.setdefault("building", "yes")
        outer_rings, outer_used, broken1 = _join_member_rings(outer_ids, ways)
        hole_rings, inner_used, broken2 = _join_member_rings(inner_ids, ways)
        diagnostics["incomplete_relation_chains"] += broken1 + broken2
        outers = [ring_coordinates(ring) for ring in outer_rings]
        holes = [ring_coordinates(ring) for ring in hole_rings]
        polygons = [{"outer": ring, "holes": []} for ring in outers if ring]
        for hole in (h for h in holes if h):
            owners = [p for p in polygons if point_in_ring(*hole[0], p["outer"])]
            if owners:
                min(owners, key=lambda p: polygon_area(p["outer"]))["holes"].append(hole)
            else:
                diagnostics["orphan_relation_holes"] += 1
        if not polygons:
            continue
        record(f"relation/{rid}", tags, polygons)
        # Suppress only matching feature categories. A landuse member carrying
        # independent building tags must still survive as a protected footprint.
        used = outer_used | inner_used
        child_ids = descendant_relations(rid)
        if tags.get("building") not in (None, "no"):
            suppressed["building"].update(used)
            suppressed_relations["building"].update(f"relation/{child}" for child in child_ids)
        if _land_kind(tags):
            kind = _land_kind(tags)
            suppressed_relations["land"].update((f"relation/{child}", kind) for child in child_ids)
            suppressed["land"].update(
                wid for wid in used if _land_kind(ways[wid].get("tags", {})) in (None, kind)
            )
        if any(tags.get(k) for k in ("shop", "office", "amenity")):
            suppressed["poi"].update(used)
            suppressed_relations["poi"].update(f"relation/{child}" for child in child_ids)
    road_widths = {
        "motorway": 0.026,
        "motorway_link": 0.014,
        "trunk": 0.022,
        "trunk_link": 0.012,
        "primary": 0.020,
        "primary_link": 0.012,
        "secondary": 0.016,
        "tertiary": 0.012,
        "residential": 0.009,
        "unclassified": 0.009,
        "service": 0.006,
        "living_street": 0.006,
    }
    for wid in sorted(ways):
        way = ways[wid]
        tags = dict(way.get("tags", {}))
        ids = way.get("nodes", [])
        highway = tags.get("highway")
        if highway in road_widths and len(ids) >= 2:
            # Never connect across a missing node: that invents a road segment.
            parts, current = [], []
            for nid in ids:
                if nid in xy:
                    current.append(xy[nid])
                elif current:
                    parts.append(current)
                    current = []
            parts.append(current)
            for part_num, points in enumerate(parts):
                if len(points) > 1 and _boxes_overlap(_bbox(points), study_box):
                    width = _tag_number(tags.get("width"), road_widths[highway] * 1000) / 1000
                    roads.append(
                        {
                            "id": f"way/{wid}/{part_num}",
                            "points": points,
                            "width_km": max(0.003, min(0.06, width)),
                            "class": highway,
                            "bridge": tags.get("bridge", "no") not in ("no", "0", ""),
                            "tunnel": tags.get("tunnel", "no") not in ("no", "0", ""),
                        }
                    )
        if len(ids) < 4 or ids[0] != ids[-1]:
            continue
        ring = ring_coordinates(ids[:-1])
        if not ring:
            continue
        if wid in suppressed["building"]:
            if tags.pop("building", None):
                diagnostics["relation_member_duplicates_avoided"] += 1
        if wid in suppressed["land"]:
            for key in ("landuse", "natural", "water", "waterway", "leisure", "boundary"):
                tags.pop(key, None)
            # Campus amenities also define an area; remove them only when their
            # relation already supplies that feature.
            if _land_kind(tags) == "institutional":
                tags.pop("amenity", None)
        if wid in suppressed["poi"]:
            for key in ("shop", "office", "amenity"):
                tags.pop(key, None)
        record(f"way/{wid}", tags, [{"outer": ring, "holes": []}])
    for nid in sorted(nodes):
        tags = nodes[nid].get("tags", {})
        if (
            nid in xy
            and grid.index(*xy[nid]) is not None
            and any(tags.get(k) for k in ("shop", "office", "amenity"))
        ):
            pois.append({"id": f"node/{nid}", "point": xy[nid], "tags": tags})
    buildings = [f for f in buildings if f["id"] not in suppressed_relations["building"]]
    land = [f for f in land if (f["id"], f["kind"]) not in suppressed_relations["land"]]
    pois = [f for f in pois if f["id"] not in suppressed_relations["poi"]]
    return {
        "buildings": sorted(buildings, key=lambda f: f["id"]),
        "land": sorted(land, key=lambda f: (f["kind"], f["id"])),
        "roads": sorted(roads, key=lambda f: f["id"]),
        "pois": sorted(pois, key=lambda f: f["id"]),
        "diagnostics": diagnostics,
    }


def parse_osm_elements(elements, cfg, grid):
    """Pure parsing entry point for saved/synthetic Overpass elements."""
    return parse_osm(elements, grid)


def load_lodes(cfg, grid):
    arrays = {
        key: np.zeros(grid.shape, dtype=np.float64)
        for key in (
            "jobs",
            "residence_jobs",
            "industrial_jobs",
            "retail_jobs",
            "office_jobs",
            "institution_jobs",
        )
    }
    metadata = {
        "year": cfg["lodes_year"],
        "version": cfg["lodes_version"],
        "job_type": cfg.get("lodes_job_type", "JT00"),
        "files": [],
        "labels": {
            "jobs": "WAC: jobs at workplace census blocks",
            "residence_jobs": "RAC: jobs by workers’ home census blocks; not population or unique people",
        },
        "allocation": "Each census block is assigned by its published internal point, not evenly over its polygon.",
    }
    sectors = {
        "industrial_jobs": ("CNS04", "CNS05", "CNS06", "CNS08"),
        "retail_jobs": ("CNS07", "CNS18"),
        "office_jobs": ("CNS09", "CNS10", "CNS11", "CNS12", "CNS13", "CNS14"),
        "institution_jobs": ("CNS15", "CNS16", "CNS20"),
    }
    for state in sorted(cfg.get("lodes_states", ["ne", "ia"])):
        report_progress(
            "loading_lodes", f"Streaming {'Iowa' if state == 'ia' else 'Nebraska'} LODES"
        )

        def fetch(kind):
            spec = lodes_input(cfg, state, kind)
            record = _download_cached(spec, cfg)
            metadata["files"].append(
                {"url": spec.url, "cache_file": str(spec.path), "sha256": record["sha256"]}
            )
            return spec.path

        print(f"LODES: streaming {state.upper()} crosswalk and {cfg['lodes_year']} WAC/RAC.")
        crosswalk = fetch("xwalk")
        selected_blocks = {}
        invalid_coordinates = 0
        with gzip.open(crosswalk, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"tabblk2020", "blklatdd", "blklondd"}.issubset(reader.fieldnames or []):
                raise RuntimeError(f"Unexpected LODES8 crosswalk schema: {crosswalk}")
            for row in reader:
                try:
                    lat, lon = float(row["blklatdd"]), float(row["blklondd"])
                except (ValueError, TypeError):
                    invalid_coordinates += 1
                    continue
                if grid.south <= lat < grid.north and grid.west <= lon < grid.east:
                    cell = grid.index(*grid.project(lon, lat))
                    if cell is not None:
                        selected_blocks[row["tabblk2020"]] = cell
        metadata.setdefault("selected_blocks", {})[state] = len(selected_blocks)
        metadata.setdefault("invalid_crosswalk_coordinates", {})[state] = invalid_coordinates
        for kind, geocode in (("wac", "w_geocode"), ("rac", "h_geocode")):
            path = fetch(kind)
            createdates = set()
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not {geocode, "C000"}.issubset(reader.fieldnames or []):
                    raise RuntimeError(f"Unexpected LODES {kind.upper()} schema: {path}")
                for row in reader:
                    cell = selected_blocks.get(row[geocode])
                    if cell is None:
                        continue
                    gx, gy = cell
                    arrays["jobs" if kind == "wac" else "residence_jobs"][gy, gx] += int(
                        row["C000"] or 0
                    )
                    if kind == "wac":
                        for name, columns in sectors.items():
                            arrays[name][gy, gx] += sum(
                                int(row.get(column, 0) or 0) for column in columns
                            )
                    if row.get("createdate"):
                        createdates.add(row["createdate"])
            metadata["files"][-1]["createdates"] = sorted(createdates)
    return {k: v.astype(np.float32) for k, v in arrays.items()}, metadata


def _points_in_ring_array(xs, ys, ring):
    inside = np.zeros(xs.shape, dtype=bool)
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if abs(b[1] - a[1]) < 1e-14:
            continue
        cross = a[0] + (ys - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        inside ^= ((a[1] > ys) != (b[1] > ys)) & (xs < cross)
    return inside


def _rasterize_polygon(mask, polygon, grid, samples):
    box = _bbox(polygon["outer"])
    # The subcell axes account for the clipped final row/column of the study.
    x0 = max(0, math.floor((box[0] - grid.min_x) / grid.cell_km))
    y0 = max(0, math.floor((box[1] - grid.min_y) / grid.cell_km))
    x1 = min(grid.nx - 1, math.floor((box[2] - grid.min_x) / grid.cell_km))
    y1 = min(grid.ny - 1, math.floor((box[3] - grid.min_y) / grid.cell_km))
    if x1 < x0 or y1 < y0:
        return
    cell_x = np.arange(x0, x1 + 1, dtype=np.float64)
    cell_y = np.arange(y0, y1 + 1, dtype=np.float64)
    xbase = grid.min_x + cell_x * grid.cell_km
    ybase = grid.min_y + cell_y * grid.cell_km
    offsets = (np.arange(samples) + 0.5) / samples
    xs = (xbase[:, None] + np.minimum(grid.cell_km, grid.max_x - xbase)[:, None] * offsets).ravel()
    ys = (ybase[:, None] + np.minimum(grid.cell_km, grid.max_y - ybase)[:, None] * offsets).ravel()
    xx, yy = np.meshgrid(xs, ys)
    inside = _points_in_ring_array(xx, yy, polygon["outer"])
    for hole in polygon.get("holes", []):
        inside &= ~_points_in_ring_array(xx, yy, hole)
    mask[y0 * samples : (y1 + 1) * samples, x0 * samples : (x1 + 1) * samples] |= inside


def _smooth_grid(array, passes=2):
    result = np.asarray(array, dtype=np.float64)
    for _ in range(passes):
        p = np.pad(result, 1, mode="edge")
        result = (p[1:-1, 1:-1] * 4 + p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) / 8.0
    return result


def _normalized_signal(array):
    values = np.maximum(0, np.asarray(array, dtype=np.float64))
    positive = values[values > 0]
    if positive.size == 0:
        return np.zeros(values.shape, dtype=np.float32)
    scale = max(1e-9, float(np.percentile(positive, 95)))
    return np.clip(np.log1p(values) / math.log1p(scale), 0, 1).astype(np.float32)


def _road_signals(roads, grid, cfg):
    weight = np.zeros(grid.nx * grid.ny, dtype=np.float64)
    cosine, sine = np.zeros_like(weight), np.zeros_like(weight)
    weights = {
        "motorway": 1.0,
        "motorway_link": 0.7,
        "trunk": 1.0,
        "primary": 1.0,
        "secondary": 0.8,
        "tertiary": 0.6,
        "residential": 0.4,
        "service": 0.2,
    }
    for road in roads:
        if road.get("tunnel"):
            continue
        for a, b in zip(road["points"], road["points"][1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length < 1e-10 or not _boxes_overlap(
                _bbox([a, b]), (grid.min_x, grid.min_y, grid.max_x, grid.max_y)
            ):
                continue
            count = max(1, int(math.ceil(length / (grid.cell_km * 0.45))))
            t = (np.arange(count) + 0.5) / count
            xs, ys = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            gx = np.floor((xs - grid.min_x) / grid.cell_km).astype(int)
            gy = np.floor((ys - grid.min_y) / grid.cell_km).astype(int)
            valid = (gx >= 0) & (gx < grid.nx) & (gy >= 0) & (gy < grid.ny)
            valid &= (xs < grid.max_x) & (ys < grid.max_y)
            ids = gy[valid] * grid.nx + gx[valid]
            w = weights.get(road["class"], 0.5) * length / count
            angle = math.atan2(b[1] - a[1], b[0] - a[0])
            np.add.at(weight, ids, w)
            np.add.at(cosine, ids, w * math.cos(2 * angle))
            np.add.at(sine, ids, w * math.sin(2 * angle))
    passes = max(1, int(round(cfg.get("road_influence_km", 0.8) / grid.cell_km)))
    road_access = _normalized_signal(_smooth_grid(weight.reshape(grid.shape), passes))
    c = _smooth_grid(cosine.reshape(grid.shape), passes)
    s = _smooth_grid(sine.reshape(grid.shape), passes)
    return road_access, (0.5 * np.arctan2(s, c)).astype(np.float32)


def derive_baseline(cfg, grid, b, lodes):
    samples = max(2, int(cfg.get("subcell_samples", 8)))
    shape = (grid.ny * samples, grid.nx * samples)
    fractions = {}
    developed = np.zeros(shape, dtype=bool)
    protected = np.zeros(shape, dtype=bool)
    for kind in (
        "water",
        "park",
        "residential",
        "commercial",
        "retail",
        "industrial",
        "institutional",
    ):
        mask = np.zeros(shape, dtype=bool)
        for feature in b["land"]:
            if feature["kind"] == kind:
                for polygon in feature["polygons"]:
                    _rasterize_polygon(mask, polygon, grid, samples)
        fractions[kind] = (
            mask.reshape(grid.ny, samples, grid.nx, samples).mean(axis=(1, 3)).astype(np.float32)
        )
        if kind not in ("water", "park"):
            developed |= mask
        else:
            protected |= mask
    building_count = np.zeros(grid.shape, dtype=np.float32)
    footprint_area = np.zeros(grid.shape, dtype=np.float32)
    height = np.zeros(grid.shape, dtype=np.float32)
    building_votes = np.zeros((len(ARCH_NAMES), *grid.shape), dtype=np.float32)
    for building in b["buildings"]:
        for polygon in building["polygons"]:
            _rasterize_polygon(developed, polygon, grid, samples)
            box = _bbox(polygon["outer"])
            cell = grid.index((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)
            if cell is not None:
                gx, gy = cell
                area = max(
                    0,
                    polygon_area(polygon["outer"])
                    - sum(polygon_area(h) for h in polygon.get("holes", [])),
                )
                building_count[gy, gx] += 1
                footprint_area[gy, gx] += area
                height[gy, gx] = max(height[gy, gx], building["height_m"])
                if building["arch"]:
                    building_votes[building["arch"], gy, gx] += area
    developed_fraction = (
        developed.reshape(grid.ny, samples, grid.nx, samples).mean(axis=(1, 3)).astype(np.float32)
    )
    # The mask is a sampled union, never a sum that double-counts overlapping land.
    del developed
    road_access, road_angle = _road_signals(b["roads"], grid, cfg)
    poi_counts = {
        key: np.zeros(grid.shape, dtype=np.float32)
        for key in ("housing", "retail", "office", "institution")
    }
    for poi in b["pois"]:
        cell = grid.index(*poi["point"])
        if cell is None:
            continue
        gx, gy = cell
        tags = poi["tags"]
        if tags.get("shop") or tags.get("amenity") in (
            "restaurant",
            "cafe",
            "fast_food",
            "fuel",
            "bar",
        ):
            poi_counts["retail"][gy, gx] += 1
        if tags.get("office"):
            poi_counts["office"][gy, gx] += 1
        if tags.get("amenity") in (
            "school",
            "university",
            "college",
            "hospital",
            "clinic",
            "place_of_worship",
        ):
            poi_counts["institution"][gy, gx] += 1
    housing = np.maximum(
        _normalized_signal(_smooth_grid(lodes["residence_jobs"])), fractions["residential"]
    )
    jobs = _normalized_signal(_smooth_grid(lodes["jobs"]))
    industrial = np.maximum(
        _normalized_signal(_smooth_grid(lodes["industrial_jobs"])), fractions["industrial"]
    )
    retail = np.maximum.reduce(
        [
            _normalized_signal(_smooth_grid(lodes["retail_jobs"])),
            fractions["retail"],
            _normalized_signal(_smooth_grid(poi_counts["retail"])),
        ]
    )
    office = np.maximum.reduce(
        [
            _normalized_signal(_smooth_grid(lodes["office_jobs"])),
            fractions["commercial"],
            _normalized_signal(_smooth_grid(poi_counts["office"])),
        ]
    )
    institution = np.maximum.reduce(
        [
            _normalized_signal(_smooth_grid(lodes["institution_jobs"])),
            fractions["institutional"],
            _normalized_signal(_smooth_grid(poi_counts["institution"])),
        ]
    )
    gx, gy = np.meshgrid(np.arange(grid.nx), np.arange(grid.ny))
    xs = grid.min_x + (gx + 0.5) * grid.cell_km
    ys = grid.min_y + (gy + 0.5) * grid.cell_km
    downtown_x, downtown_y = grid.project(*cfg.get("downtown_anchor", [-95.9345, 41.2565]))
    # DERIVED centrality combines distance to the chosen Omaha downtown anchor
    # and observed workplace concentration; it is not an observed district label.
    centrality = np.clip(
        0.55
        * np.exp(-np.hypot(xs - downtown_x, ys - downtown_y) / cfg.get("centrality_decay_km", 5.0))
        + 0.45 * _normalized_signal(_smooth_grid(lodes["jobs"], 5)),
        0,
        1,
    ).astype(np.float32)
    urban = (
        (building_count > 0)
        | (developed_fraction > 0.12)
        | (lodes["jobs"] > 12)
        | (lodes["residence_jobs"] > 12)
    )
    arch = np.zeros(grid.shape, dtype=np.uint8)
    arch[urban] = SUBURBAN
    arch[urban & ((building_count > 32) | (housing > 0.7)) & (centrality > 0.25)] = URBAN_RES
    arch[urban & (jobs > 0.45) & (housing > 0.35)] = MIXED
    arch[urban & (office > 0.5) & (jobs > housing)] = COMMERCIAL
    arch[urban & (retail > 0.55) & (retail > office) & (retail > industrial)] = RETAIL
    arch[urban & (industrial > 0.55) & (industrial > retail)] = INDUSTRIAL
    arch[urban & ((institution > 0.65) | ((office > 0.7) & (housing < 0.4)))] = OFFICE
    arch[urban & (height >= 45) & (centrality > 0.55) & (jobs > 0.6)] = HIGHRISE
    # Explicit OSM land use overrides noisy employment-sector heuristics.
    for kind, code in (
        ("residential", SUBURBAN),
        ("commercial", COMMERCIAL),
        ("retail", RETAIL),
        ("industrial", INDUSTRIAL),
        ("institutional", OFFICE),
    ):
        arch[urban & (fractions[kind] > 0.5)] = code
    dominant = building_votes.argmax(axis=0).astype(np.uint8)
    explicit_area = building_votes.sum(axis=0)
    arch[urban & (explicit_area > 0.015)] = dominant[urban & (explicit_area > 0.015)]
    # Morphology is distinct from use: an explicitly tagged tall office remains
    # nonresidential for baseline capacity, while its district contains a tower.
    arch[urban & (height >= 45)] = HIGHRISE
    for building in b["buildings"]:
        if building["arch"] == EMPTY:
            box = _bbox(building["polygons"][0]["outer"])
            cell = grid.index((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)
            building["arch"] = int(arch[cell[1], cell[0]]) if cell is not None else SUBURBAN
            if not building["arch"]:
                building["arch"] = SUBURBAN
    b.update(
        {
            "urban": urban,
            "arch": arch,
            "water_fraction": fractions["water"],
            "park_fraction": fractions["park"],
            "developed_fraction": developed_fraction,
            "protected_fraction": protected.reshape(grid.ny, samples, grid.nx, samples)
            .mean(axis=(1, 3))
            .astype(np.float32),
            "road_access": road_access,
            "road_angle": road_angle,
            "centrality": centrality,
            "housing_signal": housing.astype(np.float32),
            "job_signal": jobs,
            "industrial_signal": industrial.astype(np.float32),
            "retail_signal": retail.astype(np.float32),
            "office_signal": office.astype(np.float32),
            "institution_signal": institution.astype(np.float32),
            "jobs": lodes["jobs"],
            "residence_jobs": lodes["residence_jobs"],
        }
    )
    return b


def build_baseline(cfg, grid):
    if cfg.get("data_mode", "OFFLINE").upper() != "ONLINE":
        require_input_cache(cfg)
    elements, osm_metadata = load_osm(cfg, grid)
    report_progress("parsing_osm", "Parsing OSM geometry and relations")
    b = parse_osm(elements, grid)
    del elements
    lodes, lodes_metadata = load_lodes(cfg, grid)
    report_progress("deriving_baseline", "Deriving baseline signals and raster masks")
    b = derive_baseline(cfg, grid, b, lodes)
    b["metadata"] = {
        "osm": osm_metadata,
        "lodes": lodes_metadata,
        "diagnostics": b.pop("diagnostics"),
        "fraction_masks": f"{cfg.get('subcell_samples', 8)}×{cfg.get('subcell_samples', 8)} samples per cell; exact future-footprint exclusions follow",
        "projection": "Local equirectangular kilometer projection; suitable for metro visualization, not survey coordinates",
        "baseline_year_label": "2026 scenario start; OSM cache date and LODES year can differ",
        "limits": [
            "OSM coverage and tags are incomplete; missing building heights are assumed.",
            "No elevation, floodplain, ownership, zoning, utility capacity, or land-market model.",
            "RAC is a residential employment distribution signal, never a population count.",
            "Classification and road accessibility are derived model inputs.",
        ],
    }
    report_progress("building_spatial_index", "Building baseline spatial index")
    prepare_spatial_index(b, grid)
    print(
        f"Baseline: {len(b['buildings']):,} OSM buildings, {len(b['roads']):,} roads, "
        f"{len(b['land']):,} land features; {int(b['urban'].sum()):,} developed cells."
    )
    return b


def save_baseline(path, baseline):
    """Save real input geometry plus derived arrays; never pickle executable data."""
    arrays = {key: value for key, value in baseline.items() if isinstance(value, np.ndarray)}
    geometry = {
        key: value
        for key, value in baseline.items()
        if key not in arrays and key != "spatial_index"
    }
    arrays["baseline_json"] = np.asarray(
        json.dumps(geometry, sort_keys=True, default=_json_default)
    )
    atomic_npz(path, **arrays)


def load_baseline(path, grid):
    with np.load(path, allow_pickle=False) as saved:
        baseline = json.loads(str(saved["baseline_json"].item()))
        for key in saved.files:
            if key != "baseline_json":
                baseline[key] = saved[key].copy()
    for key in ("urban", "arch", "water_fraction", "park_fraction", "developed_fraction"):
        if key not in baseline or baseline[key].shape != grid.shape:
            raise ValueError(f"Invalid cached baseline array: {key}")
    baseline["spatial_index"] = prepare_spatial_index(baseline, grid)
    return baseline
