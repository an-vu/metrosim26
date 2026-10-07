"""Conservative future collector/access corridors; no bridges or highway junctions."""

from __future__ import annotations

import heapq
import math
from copy import deepcopy

import numpy as np

from omaha_projects import add_event, fictional_name
from omaha_workers import (
    _bbox,
    _bucket_cells,
    _ring_hits_polygon,
    prepare_spatial_index,
    stable_seed,
)


def corridor_polygon(a, b, width):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-10:
        return []
    ux, uy = dx / length * width / 2, dy / length * width / 2
    return [
        (a[0] - ux - uy, a[1] - uy + ux),
        (a[0] - ux + uy, a[1] - uy - ux),
        (b[0] + ux + uy, b[1] + uy - ux),
        (b[0] + ux - uy, b[1] + uy + ux),
    ]


def clear_corridor(poly, grid, indexes):
    if not poly:
        return False
    if any(not (grid.min_x <= x <= grid.max_x and grid.min_y <= y <= grid.max_y) for x, y in poly):
        return False
    for index in indexes:
        ids = set()
        for key in _bucket_cells(_bbox(poly), grid, 0.003):
            ids.update(index["cells"].get(key, {}).get("polygons", []))
        if any(_ring_hits_polygon(poly, index["polygons"][pid], 0.003) for pid in sorted(ids)):
            return False
    return True


def generated_index(grid, active, pending):
    polygons = []
    seen = set()
    for site in list(active.values()) + list(pending.values()):
        for building in site["buildings"]:
            if building["id"] in seen:
                continue
            seen.add(building["id"])
            polygons.extend(
                {"outer": v["poly"], "holes": v.get("holes", [])}
                for v in building.get("volumes") or [building]
            )
        for surface in site["surfaces"]:
            if surface["kind"] != "road" and surface["id"] not in seen:
                seen.add(surface["id"])
                polygons.append({"outer": surface["poly"], "holes": surface.get("holes", [])})
    return prepare_spatial_index(
        {"buildings": [{"polygons": polygons}], "land": [], "roads": []}, grid
    )


class Infrastructure:
    def __init__(self, cfg, grid, baseline, events):
        self.cfg, self.grid, self.baseline, self.events = cfg, grid, baseline, events
        self.records = []
        self.pressure = np.zeros(grid.shape, dtype=np.int32)
        self.static_index = baseline.get("spatial_index") or prepare_spatial_index(baseline, grid)
        self.connections = []
        barriers = []
        for road in sorted(baseline["roads"], key=lambda r: r["id"]):
            if not road.get("tunnel") and (
                road.get("bridge")
                or road.get("class") in {"motorway", "motorway_link", "trunk", "trunk_link"}
            ):
                for a, b in zip(road["points"], road["points"][1:]):
                    poly = corridor_polygon(a, b, road["width_km"])
                    if poly:
                        barriers.append({"outer": poly, "holes": []})
            if (
                road.get("tunnel")
                or road.get("bridge")
                or road.get("class") in {"motorway", "motorway_link", "trunk", "trunk_link"}
            ):
                continue
            points = road["points"]
            if len(points) >= 2:
                self.connections += [(road["id"], points[0]), (road["id"], points[-1])]
        self.barrier_index = prepare_spatial_index(
            {"buildings": [{"polygons": barriers}], "land": [], "roads": []}, grid
        )

    def reserved(self):
        mask = np.zeros(self.grid.shape, dtype=bool)
        for record in self.records:
            for x, y in record["affected_cells"]:
                mask[y, x] = True
        return mask

    def begin_year(self, year):
        for record in self.records:
            if year == record["start_year"] + 1:
                record.update(status="construction", phase="road construction")
                add_event(
                    self.events,
                    year,
                    record["infrastructure_id"],
                    record["name"],
                    "construction started",
                )
            for i, segment in enumerate(record["segments"]):
                if segment["opening_year"] == year:
                    record.update(status="partially_open", phase=f"segment {i + 1} open")
                    add_event(
                        self.events,
                        year,
                        record["infrastructure_id"],
                        record["name"],
                        "segment opened",
                        segment=i,
                    )
            if year == record["completion_year"]:
                record.update(status="complete", phase="complete")
                add_event(
                    self.events,
                    year,
                    record["infrastructure_id"],
                    record["name"],
                    "corridor complete",
                )

    def accessibility(self, year):
        boost = np.zeros(self.grid.shape, dtype=np.float32)
        gain = self.cfg.get("infrastructure_access_gain", 0.20)
        for record in self.records:
            for segment in record["segments"]:
                if segment["opening_year"] > year:
                    continue
                for x, y in segment["affected_cells"]:
                    boost[max(0, y - 1) : y + 2, max(0, x - 1) : x + 2] = gain
        return np.minimum(1, self.baseline["road_access"] + boost)

    def route(self, target, connection, indexes):
        """Bounded grid A*: turns/alignment cost extra; each swept segment is checked exactly."""
        grid = self.grid
        indexes = [*indexes, self.barrier_index]
        source = grid.index(*connection)
        if source is None or source == target:
            return None
        width = self.cfg.get("infrastructure_width_km", 0.018)
        limit = self.cfg.get("infrastructure_max_route_cells", 8)
        center = grid.center(*source)
        initial = [tuple(connection)]
        if math.dist(connection, center) > 1e-10:
            if not clear_corridor(corridor_polygon(connection, center, width), grid, indexes):
                return None
            initial.append(center)
        queue = [(0.0, 0.0, source, (0, 0), 0, initial)]
        best = {}
        checked = {}
        while queue:
            _, cost, cell, direction, steps, points = heapq.heappop(queue)
            key = (cell, direction, steps)
            if cost >= best.get(key, float("inf")):
                continue
            best[key] = cost
            if cell == target:
                return points
            if steps >= limit:
                continue
            for dx, dy in ((-1, 0), (0, -1), (0, 1), (1, 0)):
                nxt = cell[0] + dx, cell[1] + dy
                if not (0 <= nxt[0] < grid.nx and 0 <= nxt[1] < grid.ny):
                    continue
                if abs(nxt[0] - target[0]) + abs(nxt[1] - target[1]) > limit - steps - 1:
                    continue
                point = grid.center(*nxt)
                edge = cell, nxt
                if edge not in checked:
                    checked[edge] = clear_corridor(
                        corridor_polygon(points[-1], point, width), grid, indexes
                    )
                if not checked[edge]:
                    continue
                angle = float(self.baseline["road_angle"][nxt[1], nxt[0]])
                alignment = abs(dx * math.cos(angle) + dy * math.sin(angle))
                new_cost = (
                    cost
                    + 1
                    + 0.2 * (1 - alignment)
                    + (0.25 if direction not in ((0, 0), (dx, dy)) else 0)
                )
                distance = abs(nxt[0] - target[0]) + abs(nxt[1] - target[1])
                heapq.heappush(
                    queue,
                    (new_cost + distance, new_cost, nxt, (dx, dy), steps + 1, points + [point]),
                )
        return None

    def propose(self, year, urban, neighbors, housing_need, job_need, active, projects):
        if year <= self.cfg["base_year"]:
            return
        access = self.accessibility(year)
        near_projects = np.zeros(self.grid.shape, dtype=bool)
        for record in projects.records.values():
            if record["status"] != "retired":
                x, y = record["location"]
                near_projects[max(0, y - 1) : y + 2, max(0, x - 1) : x + 2] = True
        pressure = ((neighbors >= 0.125) | near_projects) & (
            access < self.cfg.get("infrastructure_access_threshold", 0.40)
        )
        pressure &= (
            self.baseline["water_fraction"] < self.cfg.get("substantial_water_fraction", 0.45)
        ) & (self.baseline["park_fraction"] < self.cfg.get("substantial_park_fraction", 0.75))
        pressure &= ~self.reserved()
        if housing_need <= 1e-8 and job_need <= 1e-8:
            pressure &= near_projects  # A committed large project can justify access independently.
        self.pressure = np.where(pressure, self.pressure + 1, 0)
        if len(self.records) >= self.cfg.get("infrastructure_max_projects", 24):
            return
        candidates = np.flatnonzero(
            self.pressure >= self.cfg.get("infrastructure_pressure_years", 3)
        )
        if not len(candidates):
            return
        # Bounded candidate/endpoint search; equal scores use stable flat cell order.
        candidates = sorted(
            candidates.tolist(),
            key=lambda i: (-int(self.pressure.flat[i]), -float(neighbors.flat[i]), i),
        )[:12]
        connections = list(self.connections)
        for record in self.records:
            for segment in record["segments"]:
                if segment["opening_year"] <= year:
                    connections += [(record["infrastructure_id"], p) for p in segment["points"]]
        for site in sorted(active.values(), key=lambda s: s["id"]):
            for surface in site["surfaces"]:
                if surface["kind"] == "road":
                    ring = surface["poly"]
                    # A connection to the midpoint of the local road surface is a junction.
                    connections.append(
                        (
                            surface["id"],
                            [
                                sum(p[0] for p in ring) / len(ring),
                                sum(p[1] for p in ring) / len(ring),
                            ],
                        )
                    )
        indexes = [self.static_index, generated_index(self.grid, active, projects.pending)]
        starts = 0
        for flat in candidates:
            y, x = divmod(flat, self.grid.nx)
            if self.reserved()[y, x]:
                continue
            target = self.grid.center(x, y)
            for road_id, point in sorted(
                connections, key=lambda c: (math.dist(c[1], target), c[0], tuple(c[1]))
            )[:12]:
                if math.dist(point, target) > self.grid.cell_km * self.cfg.get(
                    "infrastructure_max_route_cells", 8
                ):
                    continue
                points = self.route((x, y), point, indexes)
                if points is None:
                    continue
                duration = min(
                    len(points) - 1, self.cfg.get("infrastructure_construction_years", 3)
                )
                segments = []
                for i, (a, b) in enumerate(zip(points, points[1:])):
                    poly = corridor_polygon(a, b, self.cfg.get("infrastructure_width_km", 0.018))
                    cells = [list(c) for c in _bucket_cells(_bbox(poly), self.grid)]
                    segments.append(
                        dict(
                            points=[a, b],
                            poly=poly,
                            affected_cells=cells,
                            opening_year=year
                            + 2
                            + min(duration - 1, i * duration // (len(points) - 1)),
                        )
                    )
                related = sorted(
                    r["project_id"]
                    for r in projects.records.values()
                    if r["status"] != "retired"
                    and abs(r["location"][0] - x) + abs(r["location"][1] - y) <= 2
                )
                identity = (
                    f"infrastructure_{stable_seed(self.cfg['seed'], year, x, y, road_id):016x}"
                )
                kind = "major_access" if related else "collector_extension"
                name = fictional_name(
                    self.cfg["seed"], identity, "Access Road" if related else "Connector"
                )
                record = dict(
                    infrastructure_id=identity,
                    name=name,
                    type=kind,
                    start_year=year,
                    completion_year=max(s["opening_year"] for s in segments),
                    status="proposed",
                    phase="approved corridor",
                    geometry=points,
                    segments=segments,
                    connected_road_ids=[road_id],
                    affected_cells=[
                        list(c)
                        for c in sorted({tuple(c) for s in segments for c in s["affected_cells"]})
                    ],
                    accessibility_effect=self.cfg.get("infrastructure_access_gain", 0.20),
                    reason=dict(
                        sustained_pressure_years=int(self.pressure[y, x]),
                        housing_need=housing_need,
                        job_need=job_need,
                        road_access=float(access[y, x]),
                        urban_neighbors=float(neighbors[y, x]),
                    ),
                    related_project_ids=related,
                    fictional=True,
                    provenance="future_generated",
                )
                self.records.append(record)
                add_event(
                    self.events,
                    year,
                    identity,
                    name,
                    "corridor proposed and approved",
                    related_project_ids=related,
                )
                starts += 1
                break
            if starts >= self.cfg.get("infrastructure_max_starts_per_year", 1) or len(
                self.records
            ) >= self.cfg.get("infrastructure_max_projects", 24):
                break

    def snapshot(self):
        return deepcopy(dict(infrastructure=self.records, pressure_years=self.pressure.tolist()))
