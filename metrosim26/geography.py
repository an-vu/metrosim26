"""Optional elevation tiles and approximate coastline surfaces; no Blender imports."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import numpy as np

from metrosim26.geometry import coastline
from metrosim26.geometry.terrain import Terrain


def elevation_tile(path):
    with gzip.open(path, "rb") as source:
        raw = source.read(3601 * 3601 * 2 + 1)
    count = len(raw) // 2
    side = math.isqrt(count)
    if len(raw) % 2 or side not in (1201, 3601) or side * side != count:
        raise ValueError("Invalid elevation tile; expected a 1201 or 3601 square HGT grid")
    return np.frombuffer(raw, dtype=">i2").reshape(side, side)


def inputs(cfg):
    from metrosim26.data import InputFile, _data_signature

    result = []
    south, west, north, east = cfg["bounds"]
    if cfg.get("include_terrain"):
        if cfg.get("view_mode", "GROWTH") != "MAP":
            raise ValueError(
                "Terrain currently applies to 3D maps; growth still uses flat planning geometry."
            )
        for lat in range(math.floor(south), math.ceil(north)):
            for lon in range(math.floor(west), math.ceil(east)):
                row = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
                name = f"{row}{'E' if lon >= 0 else 'W'}{abs(lon):03d}.hgt.gz"
                result.append(
                    InputFile(
                        f"Elevation {name}",
                        "elevation",
                        Path(cfg["cache_dir"]) / "Terrain" / name,
                        f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/{row}/{name}",
                    )
                )
    if cfg.get("include_ocean"):
        bbox = f"({south:.8f},{west:.8f},{north:.8f},{east:.8f})"
        date = (
            "[date:" + json.dumps(str(cfg["osm_data_snapshot"])) + "]"
            if cfg.get("osm_data_snapshot")
            else ""
        )
        query = (
            "[out:json][timeout:210]"
            + date
            + ';way["natural"="coastline"]'
            + bbox
            + ";(._;>;);out body;"
        )
        result.append(
            InputFile(
                "Ocean coastline",
                "osm",
                Path(cfg["cache_dir"]) / "Coast" / (_data_signature(query) + ".json"),
                cfg["overpass_url"],
                query=query,
            )
        )
    return result


class Geography(Terrain):
    def __init__(self, cfg, grid):
        from metrosim26.data import validate_input

        self.grid = grid
        self.spacing = float(
            cfg.get("terrain_spacing_km", max(0.03, max(grid.width, grid.height) / 200))
        )
        self.inner_radius = cfg.get("expand_inner_radius_km", 0)
        self.tiles = {}
        self.segments = []
        for spec in inputs(cfg):
            validate_input(spec)
            if spec.kind == "elevation":
                name = spec.path.name
                lat = int(name[1:3]) * (1 if name[0] == "N" else -1)
                lon = int(name[4:7]) * (1 if name[3] == "E" else -1)
                self.tiles[lat, lon] = elevation_tile(spec.path)
            else:
                elements = json.loads(spec.path.read_text())["elements"]
                nodes = {
                    e["id"]: grid.project(e["lon"], e["lat"])
                    for e in elements
                    if e["type"] == "node"
                }
                for way in elements:
                    if way["type"] != "way" or way.get("tags", {}).get("natural") != "coastline":
                        continue
                    refs = way.get("nodes", [])
                    for a, b in zip(refs, refs[1:]):
                        if a in nodes and b in nodes and nodes[a] != nodes[b]:
                            self.segments.append((nodes[a], nodes[b]))

    def surface(self):
        return coastline.surface(self)
