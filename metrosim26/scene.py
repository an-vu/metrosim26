"""Pure scene geometry and bounded mesh export. Never imports Blender."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from metrosim26.config import timeline_start
from metrosim26.geometry import land, water
from metrosim26.geometry.buildings import _building_geometry as _building_geometry
from metrosim26.geometry.lifetimes import _visual_lifetime_records as _visual_lifetime_records
from metrosim26.geometry.packets import PacketBatch as PacketBatch
from metrosim26.geometry.polygons import _oriented_ring as _oriented_ring
from metrosim26.geometry.polygons import _surface_geometry as _surface_geometry
from metrosim26.geometry.roads import _road_geometry as _road_geometry
from metrosim26.runtime import report_progress, write_json


def export_scene(cfg, grid, baseline, result, directory, *, append=False, complete=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    previous = json.loads((directory / "manifest.json").read_text()) if append else {}
    packets = list(previous.get("packets", []))
    previous_count = len(packets)
    palette = dict(previous.get("materials", {}))
    collections = {
        key: key
        for key in (
            "baseline",
            "land",
            "future",
            "stage",
            "ground",
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
                "preparing_scene",
                f"Preparing geometry: {processed:,} records processed",
                geometry_records=processed,
            )
            last_pulse = time.monotonic()

    def material(name, color, roughness=0.75, metallic=0.0):
        palette[name] = {"color": color, "roughness": roughness, "metallic": metallic}
        return name

    def make_batch(
        collection,
        name,
        material,
        max_vertices,
        lifetime=None,
        properties=None,
        *,
        inner_radius=None,
        weld=False,
        radial_batches=True,
    ):
        return PacketBatch(
            directory,
            packets,
            collection,
            name,
            material,
            max_vertices,
            lifetime,
            properties,
            cfg.get("study_radius_km"),
            cfg.get("expand_inner_radius_km", 0) if inner_radius is None else inner_radius,
            weld=weld,
            radial_batches=radial_batches,
        )

    def interior(record):
        inner = cfg.get("expand_inner_radius_km", 0)
        points = record.get("points") or [
            point for polygon in record.get("polygons", []) for point in polygon["outer"]
        ]
        return bool(
            inner
            and points
            and all(
                math.hypot(*point) + record.get("width_km", 0) < inner * math.cos(math.pi / 96)
                for point in points
            )
        )

    def observed_order(record):
        if not cfg.get("progressive_build"):
            return (0, str(record["id"]))
        points = record.get("points") or [
            point for polygon in record.get("polygons", []) for point in polygon["outer"]
        ]
        if not points:
            return (0, str(record["id"]))
        center = np.mean(points, axis=0)
        return (math.hypot(*center), str(record["id"]))

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
        key = (
            kind,
            life,
            provenance,
            material,
            feature["id"] if cfg.get("view_mode") == "MAP" else None,
        )
        if key not in observed_batches:
            observed_batches[key] = make_batch(
                collection,
                name,
                material,
                chunk_size,
                life,
                dict(
                    provenance=provenance,
                    feature_id=str(feature["id"]) if cfg.get("view_mode") == "MAP" else "",
                    feature_kind=kind,
                    input_provenance="observed",
                    historical_coverage=evidence["historical_coverage"]
                    if evidence
                    else "not_requested",
                ),
            )
        return observed_batches[key]

    from metrosim26.geography import Geography

    geography = Geography(cfg, grid)
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
    colors.update(cfg.get("palette", {}))
    materials = {
        key: material("MetroSim26_" + key, color, 0.28 if key == "water" else 0.8)
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
    if not append:
        ground = {
            "outer": [
                (grid.min_x, grid.min_y),
                (grid.max_x, grid.min_y),
                (grid.max_x, grid.max_y),
                (grid.min_x, grid.max_y),
            ],
            "holes": [],
        }
        if cfg.get("include_ground", True) or cfg.get("include_ocean"):
            ground_batch = land.ground_batch(make_batch, materials["ground"], chunk_size)
            ocean_batch = water.ocean_batch(make_batch, materials["water"], chunk_size)
            if cfg.get("include_terrain") or cfg.get("include_ocean"):
                # Resample the complete land surface so an old clipped boundary
                # cannot survive as a crack. Ocean keeps its existing ring path.
                geography.inner_radius = 0
                for sea, points in geography.surface():
                    pulse()
                    if sea and cfg.get("include_ocean"):
                        ocean_batch.add(points, [list(range(len(points)))])
                    elif cfg.get("include_ground", True):
                        ground_batch.add(*land.ground_geometry(points, geography))
            elif cfg.get("include_ground", True):
                ground_batch.add(*_surface_geometry(ground, -0.00002))
            ground_batch.flush()
            ocean_batch.flush()
        report_progress("preparing_scene", "Preparing OSM buildings")
        for building in sorted(
            baseline["buildings"] if cfg.get("include_buildings", True) else [], key=observed_order
        ):
            if interior(building):
                continue
            pulse()
            observed = observed_batch(
                "building",
                building,
                collections["baseline"],
                "OSM_Buildings",
                materials["existing"],
            )
            for polygon in building["polygons"]:
                vertices, faces = _building_geometry(polygon, building["height_m"] * vertical_scale)
                observed.add(geography.lift(vertices, building=True), faces)
        report_progress("preparing_scene", "Preparing OSM roads")
        for road in sorted(
            baseline["roads"] if cfg.get("include_roads", True) else [], key=observed_order
        ):
            if interior(road):
                continue
            pulse()
            if road.get("tunnel", False):
                continue  # The flat map cannot represent terrain above a tunnel.
            roads = observed_batch(
                "road", road, collections["observed_roads"], "OSM_Roads", materials["road"]
            )
            elevation = 0.007 if road.get("bridge", False) else 0.00002
            vertices, faces = _road_geometry(
                geography.road_points(road["points"]), road["width_km"], elevation
            )
            vertices, faces = geography.drape_road(
                vertices, faces, bridge=road.get("bridge", False)
            )
            roads.add(vertices, faces)
        report_progress("preparing_scene", "Preparing land features")
        for feature in sorted(baseline["land"], key=observed_order):
            if interior(feature):
                continue
            pulse()
            kind = feature["kind"]
            if kind in ("water", "park") and cfg.get(
                "include_water" if kind == "water" else "include_parks", True
            ):
                land_batch = observed_batch(
                    "land", feature, collections["land"], "OSM_" + kind, materials[kind]
                )
                for polygon in feature["polygons"]:
                    geometry = water.inland_geometry if kind == "water" else land.park_geometry
                    land_batch.add(*geometry(polygon, geography))
        for batch in observed_batches.values():
            batch.flush()
    batches = {}
    base_year = int(cfg["base_year"])
    base_decade = (base_year // 10) * 10
    versions = _visual_lifetime_records(result["versions"])
    project_names = {p["project_id"]: p["name"] for p in result.get("projects", [])}
    previous_lifetime = None
    report_progress("preparing_scene", "Preparing future development")
    for site in (
        versions if cfg.get("include_buildings", True) or cfg.get("include_roads", True) else []
    ):
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
        for building in sorted(
            site["buildings"] if cfg.get("include_buildings", True) else [],
            key=lambda record: record["id"],
        ):
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
        for surface in site.get("surfaces", []) if cfg.get("include_roads", True) else []:
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
    for record in result.get("infrastructure", []) if cfg.get("include_roads", True) else []:
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
    if cfg.get("progressive_build"):
        packets[previous_count:] = sorted(
            packets[previous_count:],
            key=lambda packet: (
                packet["lifetime"][0] if packet["lifetime"] else timeline_start(cfg),
                packet.get("distance", 0),
            ),
        )
    manifest = {
        "schema_version": 1,
        "complete": complete,
        "cfg": cfg,
        "result_directory": result["run_directory"],
        "grid": {
            key: getattr(grid, key)
            for key in ("min_x", "min_y", "max_x", "max_y", "width", "height")
        },
        "materials": palette,
        "packets": packets,
        "warnings": [
            "No coastline found in this area; ocean surface omitted. Inland water is still available."
        ]
        if cfg.get("include_ocean") and not geography.segments
        else [],
        "height_bounds": [
            min((p.get("min_z", 0) for p in packets), default=0),
            max((p.get("max_z", 0.3) for p in packets), default=0.3),
        ],
    }
    write_json(directory / "manifest.json", manifest)
    return manifest
