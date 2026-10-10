"""Data osm: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import hashlib
import json
import math

from metrosim26.config import (
    COMMERCIAL,
    EMPTY,
    HIGHRISE,
    INDUSTRIAL,
    OFFICE,
    RETAIL,
    SUBURBAN,
    URBAN_RES,
)
from metrosim26.datasets.download import _download_cached
from metrosim26.datasets.inputs import osm_inputs
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    _bbox,
    _boxes_overlap,
    point_in_ring,
    polygon_area,
)


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
