"""Optional fictional projects built from authoritative, demand-selected site plans."""

from __future__ import annotations

from copy import deepcopy

import numpy as np

from omaha_config import HIGHRISE, INDUSTRIAL, MIXED, OFFICE, RETAIL, SUBURBAN, URBAN_RES
from omaha_workers import stable_seed


def fictional_name(seed, identity, suffix):
    value = stable_seed(seed, identity, "fictional-name-v1")
    first = (
        "Prairie",
        "Two Pines",
        "Lone Hill",
        "Cottonwood",
        "Westhaven",
        "Riverbend",
        "Meadow",
        "Oak Ridge",
    )
    second = ("Crossing", "Heights", "Grove", "Valley", "Commons", "Ridge", "Point", "Fields")
    return f"{first[value % len(first)]} {second[(value // len(first)) % len(second)]} {suffix}"


def add_event(events, year, identity, name, action, **details):
    events.append(
        dict(
            event_id=f"event_{len(events):08d}",
            year=int(year),
            entity_id=identity,
            name=name,
            action=action,
            provenance="future_generated",
            fictional=True,
            **details,
        )
    )


def capacity(buildings):
    return sum(b["housing"] for b in buildings), sum(b["jobs"] for b in buildings)


class Projects:
    def __init__(self, cfg, grid, events):
        self.cfg, self.grid, self.events = cfg, grid, events
        self.records = {}
        self.pending = {}
        self.starts = {}

    def project_type(self, site, baseline):
        kind = site["arch"]
        if kind in (SUBURBAN, URBAN_RES):
            return "residential_neighborhood", "Neighborhood"
        if kind == RETAIL:
            return "regional_retail", "Retail Center"
        if kind == INDUSTRIAL:
            return "industrial_park", "Logistics Park"
        if kind == OFFICE:
            y, x = site["gy"], site["gx"]
            if baseline["institution_signal"][y, x] > baseline["office_signal"][y, x]:
                return "institutional_expansion", "Campus"
            return "office_campus", "Office Campus"
        if kind == HIGHRISE:
            return "downtown_complex", "Towers"
        return (
            ("mixed_use_district", "District")
            if kind == MIXED
            else ("regional_retail", "Commerce Center")
        )

    def stage(self, site, previous, phase, year, baseline, housing_need, job_need):
        """Reserve selected geometry now; credit capacity only as phases actually open."""
        if not self.cfg.get("enable_projects"):
            return site
        prior_id = previous.get("project_id") if previous else None
        if phase == "redevelopment":
            if prior_id:
                record = self.records[prior_id]
                record.update(status="retired", phase="replaced", retired_year=year)
                add_event(self.events, year, prior_id, record["name"], "redeveloped / retired")
            return site
        if prior_id:
            site["project_id"] = prior_id
        old_ids = {b["id"] for b in previous["buildings"]} if previous else set()
        additions = [b for b in site["buildings"] if b["id"] not in old_ids]
        homes, jobs = capacity(additions)
        qualifies = len(additions) >= self.cfg.get("project_min_buildings", 2) and (
            homes >= self.cfg.get("project_min_housing", 40)
            or jobs >= self.cfg.get("project_min_jobs", 150)
        )
        if not qualifies or (
            not prior_id
            and self.starts.get(year, 0) >= self.cfg.get("project_max_starts_per_year", 3)
        ):
            if prior_id and additions:
                record = self.records[prior_id]
                add_event(self.events, year, prior_id, record["name"], "ordinary infill expansion")
            return site
        project_id = (
            prior_id
            or f"project_{stable_seed(self.cfg['seed'], site['id'], site['layout_epoch'], year, 'project-v1'):016x}"
        )
        if not prior_id:
            kind, suffix = self.project_type(site, baseline)
            name = fictional_name(self.cfg["seed"], project_id, suffix)
            if any(r["name"] == name for r in self.records.values()):
                name += " " + project_id[-4:].upper()
            y, x = site["gy"], site["gx"]
            nearby = sorted(
                r["project_id"]
                for r in self.records.values()
                if r["status"] != "retired"
                and abs(r["location"][0] - x) + abs(r["location"][1] - y) <= 2
            )
            self.records[project_id] = dict(
                project_id=project_id,
                name=name,
                project_type=kind,
                origin_year=year,
                start_year=year,
                completion_year=year,
                status="proposed",
                phase="proposal",
                location=[x, y],
                affected_cells=[[x, y]],
                generated_site_ids=[site["id"]],
                housing_capacity=0.0,
                job_capacity=0.0,
                fictional=True,
                provenance="future_generated",
                reason={
                    "selection": "Existing demand ranking and validated site plan",
                    "housing_need": housing_need,
                    "job_need": job_need,
                    "nearby_project_ids": nearby,
                    "signals": {
                        k: float(baseline[k][y, x])
                        for k in (
                            "road_access",
                            "centrality",
                            "housing_signal",
                            "job_signal",
                            "industrial_signal",
                            "retail_signal",
                            "office_signal",
                            "institution_signal",
                        )
                    },
                },
            )
            self.starts[year] = self.starts.get(year, 0) + 1
            add_event(self.events, year, project_id, name, "proposed and approved")
        record = self.records[project_id]
        duration = min(len(additions), self.cfg.get("project_construction_years", 3))
        completion = year + duration  # Never truncate construction at END_YEAR.
        record.update(
            completion_year=completion,
            status="expanding" if prior_id else "construction",
            phase="access roads / site preparation",
        )
        add_event(
            self.events,
            year,
            project_id,
            record["name"],
            "expansion started" if prior_id else "construction started",
        )
        site = deepcopy(site)
        site["project_id"] = project_id
        opening = {
            b["id"]: year + 1 + min(duration - 1, i * duration // len(additions))
            for i, b in enumerate(additions)
        }
        for b in site["buildings"]:
            if b["id"] in opening:
                b["construction_year"] = opening[b["id"]]
        for surface in site["surfaces"]:
            if surface["id"].endswith("_parking") and surface["id"][:-8] in opening:
                surface["construction_year"] = opening[surface["id"][:-8]]
        self.pending[site["id"]] = deepcopy(site)
        site["buildings"] = [b for b in site["buildings"] if b["construction_year"] <= year]
        site["surfaces"] = [s for s in site["surfaces"] if s["construction_year"] <= year]
        return site

    def due(self, year, active):
        result = []
        for site_id in sorted(self.pending):
            planned = self.pending[site_id]
            old = active[site_id]
            additions = [b for b in planned["buildings"] if b["construction_year"] == year]
            if not additions:
                continue
            site = deepcopy(old)
            site.update(version=old["version"] + 1, start_year=year, end_year=None)
            site["buildings"] += deepcopy(additions)
            site["surfaces"] += deepcopy(
                [s for s in planned["surfaces"] if s["construction_year"] == year]
            )
            record = self.records[planned["project_id"]]
            complete = year == record["completion_year"]
            record.update(
                status="complete" if complete else "partially_open",
                phase="complete" if complete else f"opening phase {year - record['start_year']}",
            )
            add_event(
                self.events,
                year,
                record["project_id"],
                record["name"],
                "opened / complete" if complete else "phase opened",
            )
            if complete:
                del self.pending[site_id]
            result.append((site, old))
        return result

    def refresh(self, active):
        for record in self.records.values():
            buildings = [
                b
                for sid in record["generated_site_ids"]
                if sid in active and active[sid].get("project_id") == record["project_id"]
                for b in active[sid]["buildings"]
            ]
            record["housing_capacity"], record["job_capacity"] = capacity(buildings)

    def feedback(self, baseline):
        result = dict(baseline)
        keys = (
            "housing_signal",
            "job_signal",
            "industrial_signal",
            "retail_signal",
            "office_signal",
            "institution_signal",
        )
        boosts = {key: np.zeros(self.grid.shape, dtype=np.float32) for key in keys}
        cap = self.cfg.get("project_feedback_cap", 0.15)
        for record in self.records.values():
            if record["status"] != "complete":
                continue
            x, y = record["location"]
            targets = []
            if record["housing_capacity"] > 0:
                targets.append("housing_signal")
            if record["job_capacity"] > 0:
                targets.append("job_signal")
            sector = {
                "industrial_park": "industrial_signal",
                "regional_retail": "retail_signal",
                "office_campus": "office_signal",
                "institutional_expansion": "institution_signal",
            }.get(record["project_type"])
            if sector:
                targets.append(sector)
            for key in targets:
                boosts[key][max(0, y - 1) : y + 2, max(0, x - 1) : x + 2] += cap / 2
        for key in keys:
            result[key] = np.minimum(1, baseline[key] + np.minimum(cap, boosts[key]))
        return result

    def snapshot(self):
        return deepcopy(
            dict(
                projects=[self.records[k] for k in sorted(self.records)],
                pending_sites=self.pending,
                starts_by_year=self.starts,
            )
        )
