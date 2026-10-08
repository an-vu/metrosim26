#!/usr/bin/env python3
"""Pure MetroSim26 geometry and process workers. Never imports Blender.

Keep this module inside the metrosim26 package. Worker startup uses an external Python broker
and an explicit spawn context, including on non-Windows test machines.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EMPTY, SUBURBAN, URBAN_RES, MIXED, COMMERCIAL, RETAIL, INDUSTRIAL, OFFICE, HIGHRISE = range(9)


@dataclass
class Grid:
    bounds: tuple
    cell_km: float

    def __post_init__(self):
        self.south, self.west, self.north, self.east = map(float, self.bounds)
        if not (self.south < self.north and self.west < self.east and self.cell_km > 0):
            raise ValueError("Invalid study bounds or CELL_KM.")
        self.center_lon = (self.west + self.east) / 2
        self.center_lat = (self.south + self.north) / 2
        self.cos_lat = math.cos(math.radians(self.center_lat))
        self.min_x, self.min_y = self.project(self.west, self.south)
        self.max_x, self.max_y = self.project(self.east, self.north)
        self.width, self.height = self.max_x - self.min_x, self.max_y - self.min_y
        self.nx = int(math.ceil(self.width / self.cell_km))
        self.ny = int(math.ceil(self.height / self.cell_km))
        self.shape = (self.ny, self.nx)

    def project(self, lon, lat):
        # Local equirectangular approximation, adequate for scenario-scale Omaha.
        return ((lon - self.center_lon) * 111.320 * self.cos_lat, (lat - self.center_lat) * 110.574)

    def index(self, x, y):
        if not (self.min_x <= x < self.max_x and self.min_y <= y < self.max_y):
            return None
        return (
            int(math.floor((x - self.min_x) / self.cell_km)),
            int(math.floor((y - self.min_y) / self.cell_km)),
        )

    def center(self, gx, gy):
        x0, y0, x1, y1 = self.cell_bounds(gx, gy)
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    def cell_bounds(self, gx, gy):
        x0 = self.min_x + gx * self.cell_km
        y0 = self.min_y + gy * self.cell_km
        return (x0, y0, min(x0 + self.cell_km, self.max_x), min(y0 + self.cell_km, self.max_y))


def stable_seed(seed, *parts):
    payload = json.dumps(
        [int(seed), *parts], sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little")


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def polygon_area(ring):
    """Unsigned planar area of an open or closed ring, in km²."""
    if len(ring) < 3:
        return 0.0
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) * 0.5


def _bbox(ring):
    xs, ys = zip(*ring)
    return min(xs), min(ys), max(xs), max(ys)


def _boxes_overlap(a, b, clearance=0.0):
    return not (
        a[2] + clearance < b[0]
        or b[2] + clearance < a[0]
        or a[3] + clearance < b[1]
        or b[3] + clearance < a[1]
    )


def _point_segment_distance2(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2)) if length2 else 0.0
    return (p[0] - a[0] - t * dx) ** 2 + (p[1] - a[1] - t * dy) ** 2


def _segments_touch(a, b, c, d, clearance=0.0):
    if not _boxes_overlap(
        (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])),
        (min(c[0], d[0]), min(c[1], d[1]), max(c[0], d[0]), max(c[1], d[1])),
        clearance,
    ):
        return False

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)):
        return True
    tolerance2 = max(1e-20, clearance * clearance)
    return (
        min(
            _point_segment_distance2(a, c, d),
            _point_segment_distance2(b, c, d),
            _point_segment_distance2(c, a, b),
            _point_segment_distance2(d, a, b),
        )
        <= tolerance2
    )


def point_in_ring(x, y, ring):
    """Boundary-inclusive ray test. A ring must have at least three vertices."""
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if _point_segment_distance2((x, y), a, b) < 1e-20:
            return True
        if (a[1] > y) != (b[1] > y):
            if x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                inside = not inside
    return inside


def _point_in_polygon(x, y, polygon):
    return point_in_ring(x, y, polygon["outer"]) and not any(
        point_in_ring(x, y, h) for h in polygon.get("holes", [])
    )


def rings_intersect(a, b, clearance=0.0):
    """True for overlap, containment, touching, or insufficient edge clearance."""
    if len(a) < 3 or len(b) < 3 or not _boxes_overlap(_bbox(a), _bbox(b), clearance):
        return False
    for p, q in zip(a, a[1:] + a[:1]):
        for r, s in zip(b, b[1:] + b[:1]):
            if _segments_touch(p, q, r, s, clearance):
                return True
    return point_in_ring(a[0][0], a[0][1], b) or point_in_ring(b[0][0], b[0][1], a)


def _ring_hits_polygon(ring, polygon, clearance=0.0):
    outer = polygon["outer"]
    if not _boxes_overlap(_bbox(ring), _bbox(outer), clearance):
        return False
    # Test material boundaries independently: a ring wholly inside a sufficiently
    # large courtyard/island is allowed, while crossing a hole boundary is not.
    for boundary in [outer] + polygon.get("holes", []):
        for a, b in zip(ring, ring[1:] + ring[:1]):
            for c, d in zip(boundary, boundary[1:] + boundary[:1]):
                if _segments_touch(a, b, c, d, clearance):
                    return True
    return _point_in_polygon(ring[0][0], ring[0][1], polygon) or point_in_ring(
        outer[0][0], outer[0][1], ring
    )


def mesh_triangles(polygon):
    """Exact horizontal-slab tessellation of a valid simple polygon with holes.

    Uses the even/odd fill rule across every boundary. Added vertices on slab
    boundaries are intentional. No convex-hull shortcut can fill a courtyard.
    Invalid/self-intersecting OSM polygons are outside this tessellator's scope.
    """
    rings = [polygon["outer"]] + polygon.get("holes", [])
    edges, starts, ends = [], {}, {}
    for ring in rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if abs(a[1] - b[1]) < 1e-12:
                continue
            lo, hi = (a, b) if a[1] < b[1] else (b, a)
            edge_id = len(edges)
            edges.append((lo, hi))
            starts.setdefault(lo[1], []).append(edge_id)
            ends.setdefault(hi[1], []).append(edge_id)
    levels = sorted(set(starts) | set(ends))
    active, triangles = set(), []

    def at_y(edge_id, y):
        a, b = edges[edge_id]
        return a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])

    for y0, y1 in zip(levels, levels[1:]):
        active.difference_update(ends.get(y0, []))
        active.update(starts.get(y0, []))
        ordered = sorted(active, key=lambda i: (at_y(i, (y0 + y1) * 0.5), i))
        if len(ordered) % 2:
            continue
        for left, right in zip(ordered[::2], ordered[1::2]):
            a, b = (at_y(left, y0), y0), (at_y(right, y0), y0)
            c, d = (at_y(right, y1), y1), (at_y(left, y1), y1)
            for tri in ([a, b, c], [a, c, d]):
                if polygon_area(tri) > 1e-14:
                    triangles.append(tri)
    return triangles


def _bucket_cells(box, grid, padding=0.0):
    minx, miny, maxx, maxy = box
    if not _boxes_overlap(box, (grid.min_x, grid.min_y, grid.max_x, grid.max_y), padding):
        return
    x0 = max(0, math.floor((minx - padding - grid.min_x) / grid.cell_km))
    y0 = max(0, math.floor((miny - padding - grid.min_y) / grid.cell_km))
    x1 = min(grid.nx - 1, math.floor((maxx + padding - grid.min_x) / grid.cell_km))
    y1 = min(grid.ny - 1, math.floor((maxy + padding - grid.min_y) / grid.cell_km))
    for gy in range(y0, y1 + 1):
        for gx in range(x0, x1 + 1):
            yield gx, gy


def prepare_spatial_index(b, grid):
    """Cell buckets contain each exact protected polygon/road segment once."""
    index = {"polygons": [], "segments": [], "cells": {}}

    def cell(gx, gy):
        return index["cells"].setdefault((gx, gy), {"polygons": [], "segments": []})

    for feature in b.get("buildings", []) + [
        f for f in b.get("land", []) if f["kind"] in ("water", "park")
    ]:
        for polygon in feature["polygons"]:
            pid = len(index["polygons"])
            index["polygons"].append(polygon)
            for gx, gy in _bucket_cells(_bbox(polygon["outer"]), grid):
                cell(gx, gy)["polygons"].append(pid)
    for road in b.get("roads", []):
        if road.get("tunnel"):
            continue  # Buried infrastructure is not a surface road exclusion.
        for p, q in zip(road["points"], road["points"][1:]):
            sid = len(index["segments"])
            width = road["width_km"] * 0.5
            index["segments"].append((p, q, width))
            for gx, gy in _bucket_cells(_bbox([p, q]), grid, width):
                cell(gx, gy)["segments"].append(sid)
    index["polygon_bounds"] = [_bbox(p["outer"]) for p in index["polygons"]]
    b["spatial_index"] = index
    return index


def footprint_allowed(poly, b, grid, gx, gy, clearance_km=0.0):
    """Exact narrow-phase check after bucket lookup; partial cells stay usable."""
    if len(poly) < 3 or polygon_area(poly) < 1e-12:
        return False
    allowed = grid.cell_bounds(gx, gy)
    box = _bbox(poly)
    if (
        box[0] < allowed[0] - 1e-10
        or box[1] < allowed[1] - 1e-10
        or box[2] > allowed[2] + 1e-10
        or box[3] > allowed[3] + 1e-10
    ):
        return False
    index = b.get("spatial_index") or prepare_spatial_index(b, grid)
    polygons, segments = set(), set()
    for key in _bucket_cells(box, grid, clearance_km):
        bucket = index["cells"].get(key)
        if bucket:
            polygons.update(bucket["polygons"])
            segments.update(bucket["segments"])
    bounds = index.get("polygon_bounds")
    for pid in sorted(polygons):
        if bounds is not None and not _boxes_overlap(box, bounds[pid], clearance_km):
            continue
        if _ring_hits_polygon(poly, index["polygons"][pid], clearance_km):
            return False
    for sid in sorted(segments):
        p, q, radius = index["segments"][sid]
        clearance = radius + clearance_km
        if not _boxes_overlap(box, _bbox([p, q]), clearance):
            continue
        if point_in_ring(p[0], p[1], poly) or point_in_ring(q[0], q[1], poly):
            return False
        if any(_segments_touch(a, c, p, q, clearance) for a, c in zip(poly, poly[1:] + poly[:1])):
            return False
    return True


def building_capacity(poly, floors, arch, cfg, housing_share=None):
    """Approximate usable capacity from generated floor area (all values assumed)."""
    area_m2 = abs(polygon_area(poly)) * 1_000_000.0
    if arch == SUBURBAN:
        return 1.0, 0.0
    if housing_share is None:
        housing_share = cfg["housing_share_by_archetype"].get(str(arch), 0.0)
    floor_area = area_m2 * floors * cfg.get("usable_floor_area_ratio", 0.80)
    housing = floor_area * housing_share / cfg.get("floor_area_per_home_m2", 95.0)
    job_area = {
        COMMERCIAL: cfg.get("commercial_area_per_job_m2", 38.0),
        RETAIL: cfg.get("retail_area_per_job_m2", 45.0),
        INDUSTRIAL: cfg.get("industrial_area_per_job_m2", 105.0),
    }.get(arch, cfg.get("office_area_per_job_m2", 24.0))
    jobs = floor_area * (1.0 - housing_share) / job_area
    return float(housing), float(jobs)


def site_id(gx, gy):
    return f"site_{gy:05d}_{gx:05d}"


def make_site_plan(cfg, grid, baseline, gx, gy, arch, epoch, year):
    """Road-aligned, setback parcels. Every footprint is checked against real data.

    Coordinates are km; heights are meters. Rotating an inscribed square keeps
    its parcels inside the study cell. A plan reserves rights of way even where
    real geometry prevents a generated street segment from being drawn.
    """
    rng = np.random.default_rng(stable_seed(cfg["seed"], gx, gy, arch, epoch, "parcels"))
    xmin, ymin, xmax, ymax = grid.cell_bounds(gx, gy)
    cx, cy = (xmin + xmax) * 0.5, (ymin + ymax) * 0.5
    angle = float(baseline["road_angle"][gy, gx])
    cosine, sine = math.cos(angle), math.sin(angle)
    span = (min(xmax - xmin, ymax - ymin) - 0.012) / (abs(cosine) + abs(sine))
    if span < 0.065:
        return {"buildings": [], "roads": [], "parking": {}}
    half = span * 0.5
    identity = site_id(gx, gy)

    def rect(x, y, width, depth):
        return [
            (cx + cosine * u - sine * v, cy + sine * u + cosine * v)
            for u, v in [
                (x - width / 2, y - depth / 2),
                (x + width / 2, y - depth / 2),
                (x + width / 2, y + depth / 2),
                (x - width / 2, y + depth / 2),
            ]
        ]

    raw = []
    road_rects = []
    if arch in (SUBURBAN, URBAN_RES, MIXED, OFFICE, COMMERCIAL, HIGHRISE):
        road_width = 0.020 if arch == COMMERCIAL else (0.014 if arch == HIGHRISE else 0.010)
        road_rects.append(rect(0, 0, span, road_width))
    if arch in (SUBURBAN, URBAN_RES, HIGHRISE):
        road_rects.append(rect(0, 0, 0.008 if arch != HIGHRISE else 0.014, span))
    if arch == RETAIL:
        road_rects.append(rect(0, -half + 0.007, span, 0.012))
    if arch == INDUSTRIAL:
        road_rects.append(rect(0, 0, 0.014, span))

    def add(x, y, width, depth, floors, parking=None, tower=False):
        raw.append(
            {
                "poly": rect(x, y, width, depth),
                "floors": floors,
                "parking": rect(*parking) if parking is not None else None,
                "tower": (rect(x, y, width * 0.55, depth * 0.60) if tower else None),
            }
        )

    if arch == SUBURBAN:
        # Detached houses, generous yards, a local cross street and reserved lots.
        for side_x in (-1, 1):
            for side_y in (-1, 1):
                for u in np.arange(0.026, half - 0.010, 0.037):
                    for v in np.arange(0.027, half - 0.013, 0.045):
                        add(side_x * float(u), side_y * float(v), 0.014, 0.019, 2)
    elif arch == URBAN_RES:
        for side_x in (-1, 1):
            for side_y in (-1, 1):
                for u in np.arange(0.030, half - 0.017, 0.047):
                    for v in np.arange(0.030, half - 0.018, 0.052):
                        add(
                            side_x * float(u),
                            side_y * float(v),
                            0.031,
                            0.034,
                            int(rng.integers(3, 6)),
                        )
    elif arch == MIXED:
        # Perimeter wings around two open courtyards, with an east/west street.
        for side in (-1, 1):
            add(0, side * (half - 0.021), span * 0.75, 0.028, int(rng.integers(5, 8)))
            for side_x in (-1, 1):
                add(
                    side_x * (half - 0.020),
                    side * (half * 0.5 - 0.0145),
                    0.027,
                    max(0.020, half - 0.055),
                    int(rng.integers(4, 7)),
                )
    elif arch == COMMERCIAL:
        for side in (-1, 1):
            add(
                0,
                side * (half - 0.024),
                span * 0.64,
                0.032,
                1,
                parking=(0, side * (half - 0.065), span * 0.65, 0.043),
            )
            for side_x in (-1, 1):
                add(side_x * (half - 0.023), side * 0.040, 0.025, 0.024, 1)
    elif arch == RETAIL:
        add(
            -span * 0.13,
            span * 0.20,
            span * 0.63,
            span * 0.34,
            1,
            parking=(-span * 0.13, -span * 0.11, span * 0.63, span * 0.24),
        )
        for v in (-0.15, 0.10, 0.32):
            add(span * 0.38, span * v, span * 0.17, span * 0.15, 1)
    elif arch == INDUSTRIAL:
        for side in (-1, 1):
            add(
                side * span * 0.26,
                span * 0.17,
                span * 0.34,
                span * 0.49,
                1,
                parking=(side * span * 0.26, -span * 0.20, span * 0.35, span * 0.19),
            )
    elif arch == OFFICE:
        urban_office = baseline["centrality"][gy, gx] >= 0.60
        for side_x in (-1, 1):
            for side_y in (-1, 1):
                if urban_office:
                    add(
                        side_x * span * 0.26,
                        side_y * span * 0.26,
                        span * 0.32,
                        span * 0.31,
                        int(rng.integers(5, 9)),
                    )
                else:
                    add(
                        side_x * span * 0.25,
                        side_y * span * 0.31,
                        span * 0.25,
                        span * 0.25,
                        3,
                        parking=(
                            side_x * span * 0.25,
                            side_y * span * 0.10,
                            span * 0.26,
                            span * 0.12,
                        ),
                    )
    elif arch == HIGHRISE:
        for side_x in (-1, 1):
            for side_y in (-1, 1):
                add(
                    side_x * span * 0.27,
                    side_y * span * 0.27,
                    span * 0.32,
                    span * 0.32,
                    int(rng.integers(14, 25)),
                    tower=True,
                )

    clearance = cfg.get("building_clearance_km", 0.003)
    minimum_spacing = cfg["minimum_spacing_by_archetype_km"][str(arch)]
    accepted = []
    parking_by_id = {}
    occupied = []
    for ordinal, candidate in enumerate(raw):
        poly = candidate["poly"]
        parking = candidate["parking"]
        if not footprint_allowed(poly, baseline, grid, gx, gy, clearance):
            continue
        if any(rings_intersect(poly, road, clearance) for road in road_rects):
            continue
        if any(rings_intersect(poly, previous, minimum_spacing) for previous in occupied):
            continue
        if parking is not None:
            if not footprint_allowed(parking, baseline, grid, gx, gy, 0.001):
                continue
            if rings_intersect(poly, parking, 0.001):
                continue
            if any(rings_intersect(parking, other, 0.001) for other in road_rects + occupied):
                continue
        identifier = f"{identity}_e{epoch}_b{ordinal:03d}"
        floors = candidate["floors"]
        housing, jobs = building_capacity(poly, floors, arch, cfg)
        height = (
            9.0
            if arch == INDUSTRIAL
            else 5.0
            if arch in (COMMERCIAL, RETAIL)
            else floors * (3.6 if arch in (OFFICE, HIGHRISE) else 3.1)
        )
        item = {
            "id": identifier,
            "poly": poly,
            "height_m": height,
            "z0_m": 0.0,
            "arch": int(arch),
            "housing": housing,
            "jobs": jobs,
            "construction_year": int(year),
        }
        if candidate["tower"] is not None:
            podium_height = 3 * 3.6
            tower_height = floors * 3.6
            item["volumes"] = [
                {"poly": poly, "height_m": podium_height, "z0_m": 0.0},
                {"poly": candidate["tower"], "height_m": tower_height, "z0_m": podium_height},
            ]
            ph, pj = building_capacity(poly, 3, arch, cfg)
            th, tj = building_capacity(candidate["tower"], floors, arch, cfg)
            item.update(housing=ph + th, jobs=pj + tj, height_m=podium_height + tower_height)
        accepted.append(item)
        occupied.append(poly)
        if parking is not None:
            occupied.append(parking)
            parking_by_id[identifier] = {
                "id": identifier + "_parking",
                "poly": parking,
                "kind": "parking",
                "construction_year": int(year),
            }
    roads = []
    for ordinal, poly in enumerate(road_rects):
        if footprint_allowed(poly, baseline, grid, gx, gy, 0.0):
            roads.append(
                {
                    "id": f"{identity}_e{epoch}_road{ordinal}",
                    "poly": poly,
                    "kind": "road",
                    "construction_year": int(year),
                }
            )
    # A seed-controlled ordering leaves deterministic vacant parcels for infill.
    accepted.sort(key=lambda item: (stable_seed(cfg["seed"], item["id"], "lot-order"), item["id"]))
    return {"buildings": accepted, "roads": roads, "parking": parking_by_id}


# =============================================================================
# READ-ONLY WORKER INPUT — memory-mapped arrays, not 16 baseline object copies
# =============================================================================


def write_worker_state(directory, cfg, grid, baseline):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    index = baseline.get("spatial_index") or prepare_spatial_index(baseline, grid)
    vertices, ring_offsets, polygon_offsets = [], [0], [0]
    for polygon in index["polygons"]:
        for ring in [polygon["outer"]] + polygon.get("holes", []):
            vertices.extend(ring)
            ring_offsets.append(len(vertices))
        polygon_offsets.append(len(ring_offsets) - 1)
    arrays = {
        "vertices": np.asarray(vertices, dtype=np.float64).reshape(-1, 2),
        "ring_offsets": np.asarray(ring_offsets, dtype=np.int64),
        "polygon_offsets": np.asarray(polygon_offsets, dtype=np.int64),
        "segments": np.asarray(
            [(*p, *q, radius) for p, q, radius in index["segments"]], dtype=np.float64
        ).reshape(-1, 5),
        "polygon_bounds": np.asarray(
            [_bbox(p["outer"]) for p in index["polygons"]], dtype=np.float64
        ).reshape(-1, 4),
        "road_angle": baseline["road_angle"],
        "centrality": baseline["centrality"],
    }
    for category in ("polygons", "segments"):
        offsets, ids = [0], []
        for gy in range(grid.ny):
            for gx in range(grid.nx):
                ids.extend(index["cells"].get((gx, gy), {}).get(category, []))
                offsets.append(len(ids))
        arrays["cell_" + category] = np.asarray(ids, dtype=np.int64)
        arrays["cell_" + category + "_offsets"] = np.asarray(offsets, dtype=np.int64)
    for name, array in arrays.items():
        np.save(directory / (name + ".npy"), array, allow_pickle=False)
    (directory / "inputs.json").write_text(
        json.dumps(
            {"cfg": cfg, "bounds": grid.bounds, "cell_km": grid.cell_km}, default=_json_default
        ),
        encoding="utf8",
    )


class _MappedPolygons:
    def __init__(self, arrays):
        self.arrays = arrays
        # Only local geometry touched by this process is materialized as Python lists.
        self._cache = {}

    def _read(self, polygon_id):
        a = self.arrays
        start, end = map(int, a["polygon_offsets"][polygon_id : polygon_id + 2])
        rings = []
        for i in range(start, end):
            lo, hi = map(int, a["ring_offsets"][i : i + 2])
            rings.append(a["vertices"][lo:hi].tolist())
        return {"outer": rings[0], "holes": rings[1:]}

    def __getitem__(self, polygon_id):
        polygon_id = int(polygon_id)
        if polygon_id not in self._cache:
            if len(self._cache) >= 2048:
                self._cache.pop(next(iter(self._cache)))
            self._cache[polygon_id] = self._read(polygon_id)
        return self._cache[polygon_id]


class _MappedSegments:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, segment_id):
        x, y, u, v, radius = self.rows[int(segment_id)].tolist()
        return (x, y), (u, v), radius


class _MappedCells:
    def __init__(self, arrays, nx):
        self.arrays, self.nx = arrays, nx
        self.cache = {}

    def get(self, key, default=None):
        if key in self.cache:
            return self.cache[key]
        flat = key[1] * self.nx + key[0]
        result = {}
        for name in ("polygons", "segments"):
            lo, hi = self.arrays["cell_" + name + "_offsets"][flat : flat + 2]
            result[name] = self.arrays["cell_" + name][int(lo) : int(hi)].tolist()
        if len(self.cache) >= 512:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = result
        return result


def load_worker_state(directory):
    directory = Path(directory)
    inputs = json.loads((directory / "inputs.json").read_text(encoding="utf8"))
    grid = Grid(tuple(inputs["bounds"]), inputs["cell_km"])
    arrays = {
        p.stem: np.load(p, mmap_mode="r", allow_pickle=False)
        for p in sorted(directory.glob("*.npy"))
    }
    baseline = {
        "road_angle": arrays["road_angle"],
        "centrality": arrays["centrality"],
        "spatial_index": {
            "polygons": _MappedPolygons(arrays),
            # Compact bounds are hot in every rejection test;
            # only these are materialized, not full geometry.
            "polygon_bounds": arrays["polygon_bounds"].tolist(),
            "segments": _MappedSegments(arrays["segments"]),
            "cells": _MappedCells(arrays, grid.nx),
        },
    }
    return inputs["cfg"], grid, baseline


_WORKER_STATE = None


def _initialize_worker(directory):
    global _WORKER_STATE
    if "bpy" in sys.modules:
        raise RuntimeError("Blender must never be imported in a computation worker.")
    _WORKER_STATE = load_worker_state(directory)


def _evaluate_task(task):
    assert _WORKER_STATE is not None, "Worker must be initialized before evaluating tasks."
    cfg, grid, baseline = _WORKER_STATE
    gx, gy, arch, epoch, year = task
    return make_site_plan(cfg, grid, baseline, gx, gy, arch, epoch, year)


def _worker_health():
    return os.getpid(), "bpy" not in sys.modules


def broker_main(directory, workers):
    """External interpreter owns the spawn pool; Blender is never the spawn main."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    pool = None
    try:
        pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(directory,),
        )
        pid, clean = pool.submit(_worker_health).result()
        if not clean:
            raise RuntimeError("Worker imported Blender")
        print(
            json.dumps({"ready": True, "pid": pid, "workers": workers, "numpy": np.__version__}),
            flush=True,
        )
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("stop"):
                break
            try:
                results = list(pool.map(_evaluate_task, request["tasks"], chunksize=1))
                print(
                    json.dumps({"results": results}, separators=(",", ":"), allow_nan=False),
                    flush=True,
                )
            except Exception:
                print(json.dumps({"error": traceback.format_exc()}), flush=True)
                break
    except Exception:
        print(json.dumps({"error": traceback.format_exc()}), flush=True)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--broker":
        broker_main(sys.argv[2], int(sys.argv[3]))
    else:
        raise SystemExit("This helper is launched automatically by blender.py.")
