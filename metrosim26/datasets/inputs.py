"""Data inputs: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import json
import math
from pathlib import Path

from metrosim26.datasets.files import InputFile, _data_signature


def osm_inputs(cfg):
    if cfg.get("study_radius_km"):
        return fixed_osm_inputs(cfg)
    return legacy_osm_inputs(cfg)


def fixed_osm_inputs(cfg):
    """Stable geographic cells: overlapping circles share download identities."""
    south, west, north, east = cfg["bounds"]
    step = 0.025  # About 2.8 km north–south; narrower toward the poles.
    result = []
    lon, lat = cfg["study_center"]
    scale = 111.320 * math.cos(math.radians(lat))
    radius = cfg["study_radius_km"] + cfg.get("study_buffer_km", 0)
    for iy in range(math.floor((south + 90) / step), math.ceil((north + 90) / step)):
        for ix in range(math.floor((west + 180) / step), math.ceil((east + 180) / step)):
            bounds = [
                round(iy * step - 90, 8),
                round(ix * step - 180, 8),
                round((iy + 1) * step - 90, 8),
                round((ix + 1) * step - 180, 8),
            ]
            x0, x1 = (bounds[1] - lon) * scale, (bounds[3] - lon) * scale
            y0, y1 = (bounds[0] - lat) * 110.574, (bounds[2] - lat) * 110.574
            x, y = max(x0, min(0, x1)), max(y0, min(0, y1))
            if x * x + y * y > radius * radius:
                continue
            query = _osm_query(bounds, cfg.get("osm_data_snapshot"))
            signature = _data_signature(
                {"tile": bounds, "query": query, "version": cfg["osm_query_version"]}
            )
            path = Path(cfg["cache_dir"]) / "osm" / f"tile_{iy}_{ix}_{signature}.json"
            result.append(
                InputFile(
                    f"OSM cell {iy}:{ix}",
                    "osm",
                    path,
                    cfg["overpass_url"],
                    query=query,
                    tile_id=len(result),
                )
            )
    return result


def legacy_osm_inputs(cfg):
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
            radius = cfg.get("study_radius_km")
            if radius:
                lon, lat = cfg["study_center"]
                scale = 111.320 * math.cos(math.radians(lat))
                x0, x1 = (bounds[1] - lon) * scale, (bounds[3] - lon) * scale
                y0, y1 = (bounds[0] - lat) * 110.574, (bounds[2] - lat) * 110.574
                x, y = max(x0, min(0, x1)), max(y0, min(0, y1))
                if x * x + y * y > (radius + cfg.get("study_buffer_km", 0)) ** 2:
                    continue
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
    from metrosim26.geography import inputs

    if cfg.get("view_mode") == "MAP":
        return osm_inputs(cfg) + inputs(cfg)
    return osm_inputs(cfg) + [
        lodes_input(cfg, state, kind)
        for state in sorted(cfg.get("lodes_states", []))
        for kind in ("xwalk", "wac", "rac")
    ]


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
