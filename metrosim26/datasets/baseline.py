"""Data baseline: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import json
import math

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
from metrosim26.datasets.cache import require_input_cache
from metrosim26.datasets.files import atomic_npz
from metrosim26.datasets.lodes import load_lodes
from metrosim26.datasets.osm import load_osm, parse_osm
from metrosim26.runtime import report_progress
from metrosim26.workers import (
    _bbox,
    _boxes_overlap,
    _json_default,
    polygon_area,
    prepare_spatial_index,
)


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
    downtown_x, downtown_y = (
        grid.project(*cfg["downtown_anchor"])
        if cfg.get("downtown_anchor")
        else ((grid.min_x + grid.max_x) / 2, (grid.min_y + grid.max_y) / 2)
    )
    # DERIVED centrality combines distance to the configured downtown anchor
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
    if cfg.get("view_mode") == "MAP":
        lodes = {
            key: np.zeros(grid.shape, dtype=np.float32)
            for key in (
                "jobs",
                "residence_jobs",
                "industrial_jobs",
                "retail_jobs",
                "office_jobs",
                "institution_jobs",
            )
        }
        lodes_metadata = {"source": "Not used: OSM-only 3D map"}
        if (
            not (
                b["buildings"]
                or b["roads"]
                or any(f["kind"] in {"water", "park"} for f in b["land"])
            )
            and not cfg.get("include_terrain")
            and not (
                cfg.get("include_buildings") is False
                and cfg.get("include_roads") is False
                and cfg.get("include_water") is False
                and cfg.get("include_parks") is False
            )
        ):
            raise ValueError(
                "No usable 3D features returned by OpenStreetMap for this area. Coverage may be sparse, or this location may have no mapped buildings/roads. Try a larger radius or another center."
            )
    else:
        lodes, lodes_metadata = load_lodes(cfg, grid)
    if cfg.get("study_radius_km"):
        radius = cfg["study_radius_km"]
        # Outside geometry remains a collision/context input; no outside demand.
        gx, gy = np.meshgrid(np.arange(grid.nx), np.arange(grid.ny))
        xs = (
            grid.min_x
            + gx * grid.cell_km
            + np.minimum(grid.min_x + (gx + 1) * grid.cell_km, grid.max_x)
        ) / 2
        ys = (
            grid.min_y
            + gy * grid.cell_km
            + np.minimum(grid.min_y + (gy + 1) * grid.cell_km, grid.max_y)
        ) / 2
        inside = xs * xs + ys * ys <= radius * radius
        for values in lodes.values():
            if isinstance(values, np.ndarray) and values.shape == grid.shape:
                values[~inside] = 0
        b["study_radius_km"] = radius
        b["study_mask"] = inside
        # Retain surrounding observed geometry for context/collision checks.
    report_progress("deriving_baseline", "Deriving baseline signals and raster masks")
    b = derive_baseline(cfg, grid, b, lodes)
    if cfg.get("study_radius_km"):
        b["urban"][~inside] = False
        b["arch"][~inside] = 0
        b["road_access"][~inside] = 0
    b["metadata"] = {
        "osm": osm_metadata,
        "lodes": lodes_metadata,
        "diagnostics": b.pop("diagnostics"),
        "fraction_masks": f"{cfg.get('subcell_samples', 8)}×{cfg.get('subcell_samples', 8)} samples per cell; exact future-footprint exclusions follow",
        "projection": "Local equirectangular kilometer projection; suitable for metro visualization, not survey coordinates",
        "baseline_year_label": "2026 scenario start; OSM cache date and LODES year can differ",
        "limits": [
            "OSM coverage and tags are incomplete; missing building heights are assumed.",
            "Terrain is optional visualization only; no elevation-aware growth, floodplain, ownership, zoning, utility capacity, or land-market model.",
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
