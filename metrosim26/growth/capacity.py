"""Growth capacity. No Blender dependencies."""

from __future__ import annotations

import math

import numpy as np

from metrosim26.config import (
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
from metrosim26.workers import (
    make_site_plan,
    polygon_area,
    site_id,
)


def estimate_baseline_capacity(cfg, grid, baseline):
    """OSM floor-space is a MODEL estimate of homes; RAC is never population."""
    housing_capacity = 0.0
    floor_jobs = 0.0
    for building in sorted(baseline["buildings"], key=lambda item: str(item["id"])):
        radius = baseline.get("study_radius_km")
        if radius and any(
            x * x + y * y > radius * radius
            for polygon in building["polygons"]
            for x, y in polygon["outer"]
        ):
            continue
        arch = int(building.get("arch", EMPTY))
        floors = max(1.0, building.get("height_m", 6.0) / 3.2)
        for polygon in building["polygons"]:
            area = max(
                0.0,
                abs(polygon_area(polygon["outer"]))
                - sum(abs(polygon_area(hole)) for hole in polygon.get("holes", [])),
            )
            share = (
                1.0 if arch == SUBURBAN else cfg["housing_share_by_archetype"].get(str(arch), 0.0)
            )
            tags = building.get("tags", {})
            if tags.get("building") in {
                "house",
                "detached",
                "semidetached_house",
                "terrace",
                "residential",
                "bungalow",
                "apartments",
                "dormitory",
            }:
                share = 1.0
            elif (
                tags.get("office")
                or tags.get("shop")
                or tags.get("building")
                in {
                    "office",
                    "commercial",
                    "retail",
                    "industrial",
                    "warehouse",
                    "school",
                    "university",
                    "hospital",
                    "church",
                    "garage",
                    "garages",
                    "shed",
                }
            ):
                share = 0.0
            floor_area = area * 1_000_000.0 * floors * cfg.get("usable_floor_area_ratio", 0.80)
            housing_capacity += floor_area * share / cfg.get("floor_area_per_home_m2", 95.0)
            floor_jobs += floor_area * (1.0 - share) / cfg.get("office_area_per_job_m2", 24.0)
    h_vacancy = float(cfg.get("initial_housing_vacancy", 0.04))
    j_vacancy = float(cfg.get("initial_job_vacancy", 0.04))
    if not (0.0 <= h_vacancy < 1.0 and 0.0 <= j_vacancy < 1.0):
        raise ValueError("Initial vacancy assumptions must be in [0, 1).")
    requested_housing = cfg.get("initial_households")
    requested_jobs = cfg.get("initial_jobs")
    households = (
        float(requested_housing)
        if requested_housing is not None
        else housing_capacity * (1.0 - h_vacancy)
    )
    wac_jobs = float(np.sum(baseline["jobs"], dtype=np.float64))
    jobs = float(requested_jobs) if requested_jobs is not None else wac_jobs
    # Missing workplace records do not silently become a population estimate.
    # The explicit fallback is employment floor space, labeled in metadata.
    job_source = (
        "configured scenario baseline"
        if requested_jobs is not None
        else "LODES WAC workplace employment"
    )
    if requested_jobs is None and jobs <= 0.0:
        jobs = floor_jobs * (1.0 - j_vacancy)
        job_source = "MODEL fallback: estimated OSM nonresidential floor-space capacity"
    if households < 0.0 or jobs < 0.0:
        raise ValueError("Scenario household/job baselines cannot be negative.")
    return {
        "households": households,
        "jobs": jobs,
        "housing_capacity": households / (1.0 - h_vacancy),
        "job_capacity": jobs / (1.0 - j_vacancy),
        "housing_baseline_source": (
            "configured scenario baseline"
            if requested_housing is not None
            else "MODEL: OSM residential floor space / assumed area per home, less assumed vacancy"
        ),
        "job_baseline_source": job_source,
        "rac_use": "Spatial distribution signal only: jobs associated with workers' residence locations; not population.",
    }


def neighbor_fraction(urban):
    """Eight-neighbor occupancy; vectorized, with outside-study cells undeveloped."""
    padded = np.pad(urban.astype(np.float32), 1)
    result = np.zeros(urban.shape, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            if dx != 1 or dy != 1:
                result += padded[dy : dy + urban.shape[0], dx : dx + urban.shape[1]]
    return result / 8.0


def highrise_allowed(cfg, baseline, gx, gy, neighbors):
    return bool(
        baseline["centrality"][gy, gx] >= cfg.get("highrise_min_centrality", 0.70)
        and baseline["job_signal"][gy, gx] >= cfg.get("highrise_min_job_signal", 0.55)
        and neighbors[gy, gx] >= cfg.get("highrise_min_neighbor_fraction", 0.45)
    )


def choose_archetype(cfg, baseline, gx, gy, neighbors, housing_need, job_need):
    """Future land-use is an explicit scenario decision informed by real signals."""
    centrality = float(baseline["centrality"][gy, gx])
    housing_weight = housing_need / max(housing_need + job_need * 0.5, 1e-9)
    if highrise_allowed(cfg, baseline, gx, gy, neighbors):
        if (
            housing_need > cfg["highrise_min_housing_demand"]
            and job_need > cfg["highrise_min_job_demand"]
        ):
            return HIGHRISE
    if housing_weight > 0.65:
        return URBAN_RES if centrality > 0.45 or neighbors[gy, gx] > 0.60 else SUBURBAN
    if housing_weight > 0.22 and centrality > 0.35:
        return MIXED
    preferences = [
        (float(baseline["industrial_signal"][gy, gx]) + (1.0 - centrality) * 0.20, INDUSTRIAL),
        (float(baseline["retail_signal"][gy, gx]) + 0.05, RETAIL),
        (
            max(
                float(baseline["office_signal"][gy, gx]),
                float(baseline["institution_signal"][gy, gx]),
            )
            + centrality * 0.15,
            OFFICE,
        ),
        (float(baseline["road_access"][gy, gx]) * 0.45, COMMERCIAL),
    ]
    # Stable numeric tie-breaking avoids dependence on dictionary iteration.
    return max(preferences, key=lambda pair: (pair[0], -pair[1]))[1]


def site_capacity(site):
    return (
        sum(item["housing"] for item in site["buildings"]),
        sum(item["jobs"] for item in site["buildings"]),
    )


def select_plan_buildings(plan, housing_need, job_need, limit):
    selected = []
    housing = jobs = 0.0
    for building in plan["buildings"]:
        if len(selected) >= limit:
            break
        helps_housing = housing_need > housing + 1e-8 and building["housing"] > 0.0
        helps_jobs = job_need > jobs + 1e-8 and building["jobs"] > 0.0
        if not (helps_housing or helps_jobs):
            continue
        selected.append(building)
        housing += building["housing"]
        jobs += building["jobs"]
    return selected


def create_site(
    cfg,
    grid,
    baseline,
    gx,
    gy,
    arch,
    year,
    housing_need,
    job_need,
    origin_type,
    old_site=None,
    redevelopment=False,
    plan_cache=None,
):
    """Construct a full active version. Infill preserves structures; replacement retires them."""
    from metrosim26.growth.planning import _PlanningCache

    if redevelopment:
        assert old_site is not None, "Redevelopment requires an existing site."
        epoch = old_site["layout_epoch"] + 1
    else:
        epoch = old_site["layout_epoch"] if old_site is not None else 0
    cache_key = (int(gx), int(gy), int(arch), int(epoch))
    if isinstance(plan_cache, _PlanningCache):
        template = plan_cache.template(gx, gy, arch, epoch, year)
    elif plan_cache is not None and cache_key in plan_cache:
        template = plan_cache[cache_key]
    else:
        template = make_site_plan(cfg, grid, baseline, gx, gy, arch, epoch, year)
        if plan_cache is not None:
            plan_cache[cache_key] = template
    # Cached templates describe fixed parcels. Only their construction year changes.
    plan = {
        "buildings": [dict(item, construction_year=int(year)) for item in template["buildings"]],
        "roads": [dict(item, construction_year=int(year)) for item in template["roads"]],
        "parking": {
            key: dict(item, construction_year=int(year))
            for key, item in template["parking"].items()
        },
    }
    plan_building_count = len(plan["buildings"])
    previous_buildings = [] if old_site is None or redevelopment else old_site["buildings"]
    previous_ids = {item["id"] for item in previous_buildings}
    plan["buildings"] = [item for item in plan["buildings"] if item["id"] not in previous_ids]
    if previous_buildings:
        limit = max(1, int(math.ceil(len(plan["buildings"]) * cfg.get("infill_fraction", 0.35))))
    elif redevelopment:
        limit = len(plan["buildings"])
    else:
        limit = max(
            1, int(math.ceil(len(plan["buildings"]) * cfg.get("initial_parcel_fill", 0.65)))
        )
    old_housing, old_jobs = site_capacity(old_site) if old_site else (0.0, 0.0)
    selected = select_plan_buildings(
        plan,
        housing_need + (old_housing if redevelopment else 0.0),
        job_need + (old_jobs if redevelopment else 0.0),
        limit,
    )
    if not selected:
        return None
    buildings = list(previous_buildings) + selected
    if redevelopment:
        housing = sum(item["housing"] for item in buildings)
        jobs = sum(item["jobs"] for item in buildings)
        # Conservative rule: replacement preserves all old capacity, even vacant space.
        if housing + 1e-8 < old_housing or jobs + 1e-8 < old_jobs:
            return None
        if not (
            (housing_need > 1e-8 and housing > old_housing + 1e-8)
            or (job_need > 1e-8 and jobs > old_jobs + 1e-8)
        ):
            return None
    if old_site is not None and not redevelopment:
        surfaces = list(old_site["surfaces"])
    else:
        surfaces = list(plan["roads"])
    surfaces.extend(
        plan["parking"][item["id"]] for item in selected if item["id"] in plan["parking"]
    )
    return {
        "id": site_id(gx, gy),
        "version": 0 if old_site is None else old_site["version"] + 1,
        "layout_epoch": epoch,
        "plan_building_count": plan_building_count,
        "gx": int(gx),
        "gy": int(gy),
        "arch": int(arch),
        "start_year": int(year),
        "end_year": None,
        "established_year": int(year) if old_site is None else old_site["established_year"],
        "last_redevelopment_year": int(year)
        if old_site is None or redevelopment
        else old_site["last_redevelopment_year"],
        "origin_type": origin_type,
        "lifecycle_type": "redevelopment"
        if redevelopment
        else "infill"
        if old_site or origin_type == "baseline_infill"
        else "greenfield",
        "buildings": buildings,
        "surfaces": surfaces,
    }


def ranked_cells(mask, score, limit):
    flat = np.flatnonzero(mask)
    if flat.size == 0:
        return []
    # lexsort's last key is primary; linear cell index is the deterministic tie-break.
    order = np.lexsort((flat, -score.ravel()[flat]))
    return flat[order[:limit]].tolist()


def redevelopment_archetypes(old_arch, can_highrise):
    options = {
        SUBURBAN: [URBAN_RES, MIXED],
        URBAN_RES: [MIXED],
        COMMERCIAL: [MIXED, OFFICE],
        RETAIL: [MIXED, OFFICE],
        OFFICE: [],
        MIXED: [],
        INDUSTRIAL: [],
        HIGHRISE: [],
    }.get(old_arch, [])
    if can_highrise and old_arch != INDUSTRIAL and old_arch != HIGHRISE:
        options = list(options) + [HIGHRISE]
    return options


def simulation_metrics(year, grid, baseline, urban, arch, actions, ledger):
    protected = baseline.get("protected_fraction")
    if protected is None:
        protected = np.clip(baseline["water_fraction"] + baseline["park_fraction"], 0.0, 1.0)
    widths = np.array(
        [grid.cell_bounds(gx, 0)[2] - grid.cell_bounds(gx, 0)[0] for gx in range(grid.nx)]
    )
    heights = np.array(
        [grid.cell_bounds(0, gy)[3] - grid.cell_bounds(0, gy)[1] for gy in range(grid.ny)]
    )
    metrics = {
        "year": int(year),
        "developed_area": float(
            np.sum(urban * (1.0 - protected) * heights[:, None] * widths[None, :])
        ),
        "new_greenfield_cells": int(actions["greenfield"]),
        "infill_sites": int(actions["infill"]),
        "redevelopment_sites": int(actions["redevelopment"]),
        "housing_capacity_added": float(actions["housing_net"]),
        "job_capacity_added": float(actions["jobs_net"]),
        "housing_capacity_built_gross": float(actions["housing_gross"]),
        "job_capacity_built_gross": float(actions["jobs_gross"]),
        "housing_capacity_demolished": float(actions["housing_demolished"]),
        "job_capacity_demolished": float(actions["jobs_demolished"]),
    }
    columns = {
        SUBURBAN: "suburban",
        URBAN_RES: "urban_residential",
        MIXED: "mixed",
        COMMERCIAL: "commercial",
        RETAIL: "retail",
        INDUSTRIAL: "industrial",
        OFFICE: "office",
        HIGHRISE: "highrise",
    }
    for code, name in columns.items():
        metrics[name + "_cells"] = int(np.count_nonzero((arch == code) & urban))
    metrics.update({key: float(value) for key, value in ledger.items()})
    return metrics
