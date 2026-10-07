"""Pure scene geometry and bounded mesh export. Never imports Blender."""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from omaha_config import timeline_start
from omaha_runtime import report_progress, write_json
from omaha_workers import mesh_triangles


class PacketBatch:
    """Preserve face geometry while bounding each Blender mesh import."""

    def __init__(
        self,
        directory,
        packets,
        collection,
        name,
        material,
        max_vertices,
        lifetime=None,
        properties=None,
    ):
        self.directory, self.packets = directory, packets
        self.collection, self.name, self.material = collection, name, material
        self.limit = max(1000, max_vertices)
        self.lifetime = lifetime
        self.properties = properties or {}
        self.vertices, self.faces = [], []
        self.chunk = 0

    def add(self, vertices, faces):
        remap = {}
        for face in faces:
            if len(self.vertices) + len(face) > self.limit:
                self.flush()
                remap = {}
            indices = []
            for index in face:
                if index not in remap:
                    remap[index] = len(self.vertices)
                    self.vertices.append(vertices[index])
                indices.append(remap[index])
            self.faces.append(indices)

    def flush(self):
        if not self.faces:
            return
        filename = f"mesh_{len(self.packets):06d}.npz"
        offsets = np.cumsum([0] + [len(face) for face in self.faces], dtype=np.int64)
        np.savez_compressed(
            self.directory / filename,
            vertices=np.asarray(self.vertices, dtype=np.float64),
            indices=np.asarray([v for face in self.faces for v in face], dtype=np.int32),
            offsets=offsets,
        )
        self.packets.append(
            dict(
                file=filename,
                name=f"{self.name}_{self.chunk:03d}",
                collection=self.collection,
                material=self.material,
                lifetime=self.lifetime,
                properties=self.properties,
            )
        )
        self.chunk += 1
        self.vertices, self.faces = [], []


def _oriented_ring(ring, counterclockwise=True):
    points = [(float(p[0]), float(p[1])) for p in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    signed = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
    if (signed > 0.0) != counterclockwise:
        points.reverse()
    return points


def _surface_geometry(polygon, elevation_km=0.0):
    """Pure geometry helper; triangulation preserves multipolygon courtyard holes."""
    vertices, faces = [], []
    for triangle in mesh_triangles(polygon):
        triangle = _oriented_ring(triangle)
        if len(triangle) != 3:
            continue
        start = len(vertices)
        vertices.extend((x, y, elevation_km) for x, y in triangle)
        faces.append((start, start + 1, start + 2))
    return vertices, faces


def _building_geometry(polygon, height_m, z0_m=0.0):
    """Extrude actual footprints, including courtyard walls and a holed roof."""
    # Centimeter-scale offsets separate flat presentation surfaces without
    # visibly lifting buildings off the ground.
    floor = 0.00008 + float(z0_m) / 1000.0
    roof = floor + max(0.1, float(height_m)) / 1000.0
    vertices, faces = _surface_geometry(polygon, roof)
    lower_vertices, lower_faces = _surface_geometry(polygon, floor)
    offset = len(vertices)
    vertices.extend(lower_vertices)
    faces.extend(tuple(offset + i for i in reversed(face)) for face in lower_faces)
    rings = [(polygon["outer"], True)]
    rings.extend((hole, False) for hole in polygon.get("holes", []))
    for ring, counterclockwise in rings:
        ring = _oriented_ring(ring, counterclockwise)
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if a == b:
                continue
            offset = len(vertices)
            vertices.extend(
                ((a[0], a[1], floor), (b[0], b[1], floor), (b[0], b[1], roof), (a[0], a[1], roof))
            )
            faces.append((offset, offset + 1, offset + 2, offset + 3))
    return vertices, faces


def _road_geometry(points, width_km, elevation_km=0.00002):
    """Flat road strips with bounded miter joins; no cylindrical road tubes."""
    clean = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not clean or math.dist(clean[-1], xy) > 1e-9:
            clean.append(xy)
    if len(clean) < 2:
        return [], []
    normals = []
    for a, b in zip(clean, clean[1:]):
        length = math.dist(a, b)
        normals.append((-(b[1] - a[1]) / length, (b[0] - a[0]) / length))
    half = max(float(width_km), 0.001) * 0.5
    vertices, faces = [], []
    for i, (x, y) in enumerate(clean):
        before = normals[max(0, i - 1)]
        after = normals[min(i, len(normals) - 1)]
        mx, my = before[0] + after[0], before[1] + after[1]
        length = math.hypot(mx, my)
        if length < 1e-8:
            mx, my = after
            extent = half
        else:
            mx, my = mx / length, my / length
            dot = max(0.25, mx * after[0] + my * after[1])
            extent = min(half / dot, half * 2.0)
        vertices.extend(
            (
                (x + mx * extent, y + my * extent, elevation_km),
                (x - mx * extent, y - my * extent, elevation_km),
            )
        )
        if i:
            start = 2 * i
            faces.append((start - 2, start - 1, start + 1, start))
    return vertices, faces


def _visual_lifetime_records(versions):
    """Coalesce persistent buildings across infill versions before meshing.

    An unchanged house belongs to one construction/demolition cohort, even if
    its site received infill many times. This avoids duplicating its mesh once
    per snapshot while retaining exclusive replacement lifetimes.
    """
    records = {}
    for site in sorted(versions, key=lambda s: (s["start_year"], s["id"], s["version"])):
        for category in ("buildings", "surfaces"):
            for item in site.get(category, []):
                key = (category, item["id"])
                if key in records:
                    existing = records[key]
                    if existing["end_year"] != site["start_year"]:
                        raise ValueError(f"Noncontiguous or overlapping lifetime for {item['id']}")
                    if existing[category][0] != item:
                        raise ValueError(
                            f"Persistent geometry changed without a new ID: {item['id']}"
                        )
                    existing["end_year"] = site.get("end_year")
                else:
                    records[key] = {
                        "id": item["id"],
                        "version": 0,
                        "start_year": int(site["start_year"]),
                        "end_year": site.get("end_year"),
                        "buildings": [item] if category == "buildings" else [],
                        "surfaces": [item] if category == "surfaces" else [],
                        **({"project_id": site["project_id"]} if "project_id" in site else {}),
                    }
    return sorted(
        records.values(),
        key=lambda record: (
            record["start_year"],
            record["end_year"] if record["end_year"] is not None else 99999,
            record["id"],
        ),
    )


def export_scene(cfg, grid, baseline, result, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    packets = []
    palette = {}
    collections = {
        key: key
        for key in (
            "baseline",
            "land",
            "future",
            "stage",
            "observed_roads",
            "local_roads",
            "infrastructure",
        )
    }
    last_pulse = time.monotonic()
    processed = 0

    def pulse():
        nonlocal last_pulse, processed
        processed += 1
        if time.monotonic() - last_pulse > 1:
            report_progress(
                "preparing_scene", f"Preparing geometry: {processed:,} records processed"
            )
            last_pulse = time.monotonic()

    def material(name, color, roughness=0.75, metallic=0.0):
        palette[name] = {"color": color, "roughness": roughness, "metallic": metallic}
        return name

    def make_batch(collection, name, material, max_vertices, lifetime=None, properties=None):
        return PacketBatch(
            directory, packets, collection, name, material, max_vertices, lifetime, properties
        )

    first_year = timeline_start(cfg)
    historical = result.get("history")
    observed_batches = {}

    def observed_batch(kind, feature, collection, name, material):
        evidence = historical["lifetimes"][kind][feature["id"]] if historical else None
        life = (evidence["start_year"], evidence["end_year"], first_year) if evidence else None
        provenance = (
            "historically_reconstructed"
            if evidence and evidence["historical_coverage"] == "evidence_supported"
            else "observed"
        )
        key = kind, life, provenance, material
        if key not in observed_batches:
            observed_batches[key] = make_batch(
                collection,
                name,
                material,
                chunk_size,
                life,
                dict(
                    provenance=provenance,
                    input_provenance="observed",
                    historical_coverage=evidence["historical_coverage"]
                    if evidence
                    else "not_requested",
                ),
            )
        return observed_batches[key]

    colors = {
        "existing": (0.56, 0.55, 0.52),
        "road": (0.19, 0.21, 0.22),
        "future_road": (0.23, 0.26, 0.27),
        "parking": (0.36, 0.37, 0.36),
        "water": (0.11, 0.27, 0.31),
        "park": (0.28, 0.40, 0.29),
        "ground": (0.43, 0.46, 0.40),
        "infrastructure": (0.16, 0.31, 0.40),
    }
    materials = {
        key: material("Omaha_" + key, color, 0.28 if key == "water" else 0.8)
        for key, color in colors.items()
    }
    # Muted warm construction cohorts; visual choices, not observed colors.
    decade_colors = (
        (0.62, 0.48, 0.34),
        (0.65, 0.52, 0.38),
        (0.68, 0.56, 0.43),
        (0.71, 0.60, 0.48),
        (0.74, 0.65, 0.54),
        (0.77, 0.69, 0.60),
    )
    chunk_size = min(12000, int(cfg.get("mesh_chunk_vertices", 120000)))
    vertical_scale = float(cfg.get("vertical_exaggeration", 1.25))
    report_progress("preparing_scene", "Preparing OSM buildings")
    for building in sorted(baseline["buildings"], key=lambda record: record["id"]):
        pulse()
        observed = observed_batch(
            "building", building, collections["baseline"], "OSM_Buildings", materials["existing"]
        )
        for polygon in building["polygons"]:
            observed.add(*_building_geometry(polygon, building["height_m"] * vertical_scale))
    report_progress("preparing_scene", "Preparing OSM roads")
    for road in sorted(baseline["roads"], key=lambda record: record["id"]):
        pulse()
        if road.get("tunnel", False):
            continue  # The flat map cannot represent terrain above a tunnel.
        roads = observed_batch(
            "road", road, collections["observed_roads"], "OSM_Roads", materials["road"]
        )
        elevation = 0.007 if road.get("bridge", False) else 0.00002
        roads.add(*_road_geometry(road["points"], road["width_km"], elevation))
    report_progress("preparing_scene", "Preparing land features")
    for feature in sorted(baseline["land"], key=lambda record: record["id"]):
        pulse()
        kind = feature["kind"]
        if kind in ("water", "park"):
            land_batch = observed_batch(
                "land", feature, collections["land"], "OSM_" + kind, materials[kind]
            )
            elevation = 0.0 if kind == "water" else -0.000005
            for polygon in feature["polygons"]:
                land_batch.add(*_surface_geometry(polygon, elevation))
    for batch in observed_batches.values():
        batch.flush()
    ground = {
        "outer": [
            (grid.min_x, grid.min_y),
            (grid.max_x, grid.min_y),
            (grid.max_x, grid.max_y),
            (grid.min_x, grid.max_y),
        ],
        "holes": [],
    }
    ground_batch = make_batch(
        collections["stage"], "Flat_Study_Ground", materials["ground"], chunk_size
    )
    ground_batch.add(*_surface_geometry(ground, -0.00002))
    ground_batch.flush()
    batches = {}
    base_year = int(cfg["base_year"])
    base_decade = (base_year // 10) * 10
    versions = _visual_lifetime_records(result["versions"])
    project_names = {p["project_id"]: p["name"] for p in result.get("projects", [])}
    previous_lifetime = None
    report_progress("preparing_scene", "Preparing future development")
    for site in versions:
        pulse()
        start, end = int(site["start_year"]), site.get("end_year")
        if end is not None:
            end = int(end)
        if end is not None and end <= start:
            raise ValueError(f"Invalid lifetime for site {site['id']}, version {site['version']}")
        lifetime = (start, end)
        properties = dict(provenance="future_generated", fictional=True)
        if site.get("project_id"):
            properties.update(
                project_id=site["project_id"],
                fictional_project_name=project_names[site["project_id"]],
            )
        if lifetime != previous_lifetime:
            for batch in batches.values():
                batch.flush()
            batches = {}
            previous_lifetime = lifetime
        for building in sorted(site["buildings"], key=lambda record: record["id"]):
            year = int(building.get("construction_year", start))
            decade = (year // 10) * 10
            material_key = "future_" + str(decade)
            if material_key not in materials:
                index = min(len(decade_colors) - 1, max(0, (decade - base_decade) // 10))
                materials[material_key] = material(
                    "Future_" + str(decade) + "s", decade_colors[index]
                )
            key = (start, end, material_key, site.get("project_id"))
            if key not in batches:
                name = f"Scenario_{start}_{end or 'onward'}_{material_key}"
                batches[key] = make_batch(
                    collections["future"],
                    name,
                    materials[material_key],
                    chunk_size,
                    (start, end, first_year),
                    properties,
                )
            for volume in building.get("volumes") or [building]:
                polygon = {"outer": volume["poly"], "holes": volume.get("holes", [])}
                batches[key].add(
                    *_building_geometry(
                        polygon,
                        volume["height_m"] * vertical_scale,
                        volume.get("z0_m", 0.0) * vertical_scale,
                    )
                )
        for surface in site.get("surfaces", []):
            material_key = "future_road" if surface["kind"] == "road" else "parking"
            key = (start, end, material_key, site.get("project_id"))
            if key not in batches:
                name = f"Scenario_{start}_{end or 'onward'}_{material_key}"
                batches[key] = make_batch(
                    collections["local_roads"]
                    if surface["kind"] == "road"
                    else collections["future"],
                    name,
                    materials[material_key],
                    chunk_size,
                    (start, end, first_year),
                    properties,
                )
            polygon = {"outer": surface["poly"], "holes": surface.get("holes", [])}
            batches[key].add(*_surface_geometry(polygon, 0.00007))
    for batch in batches.values():
        batch.flush()
    for record in result.get("infrastructure", []):
        for i, segment in enumerate(record["segments"]):
            batch = make_batch(
                collections["infrastructure"],
                record["name"] + f"_{i}",
                materials["infrastructure"],
                chunk_size,
                (segment["opening_year"], None, first_year),
                dict(
                    provenance="future_generated",
                    fictional=True,
                    infrastructure_id=record["infrastructure_id"],
                    fictional_project_name=record["name"],
                ),
            )
            batch.add(*_surface_geometry({"outer": segment["poly"], "holes": []}, 0.00009))
            batch.flush()
    manifest = {
        "schema_version": 1,
        "cfg": cfg,
        "result_directory": result["run_directory"],
        "grid": {
            key: getattr(grid, key)
            for key in ("min_x", "min_y", "max_x", "max_y", "width", "height")
        },
        "materials": palette,
        "packets": packets,
    }
    write_json(directory / "manifest.json", manifest)
    return manifest
