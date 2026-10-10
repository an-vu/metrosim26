"""Data lodes: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import csv
import gzip

import numpy as np

from metrosim26.datasets.download import _download_cached
from metrosim26.datasets.inputs import lodes_input
from metrosim26.runtime import report_progress


def load_lodes(cfg, grid):
    arrays = {
        key: np.zeros(grid.shape, dtype=np.float64)
        for key in (
            "jobs",
            "residence_jobs",
            "industrial_jobs",
            "retail_jobs",
            "office_jobs",
            "institution_jobs",
        )
    }
    metadata = {
        "year": cfg["lodes_year"],
        "version": cfg["lodes_version"],
        "job_type": cfg.get("lodes_job_type", "JT00"),
        "files": [],
        "labels": {
            "jobs": "WAC: jobs at workplace census blocks",
            "residence_jobs": "RAC: jobs by workers’ home census blocks; not population or unique people",
        },
        "allocation": "Each census block is assigned by its published internal point, not evenly over its polygon.",
    }
    sectors = {
        "industrial_jobs": ("CNS04", "CNS05", "CNS06", "CNS08"),
        "retail_jobs": ("CNS07", "CNS18"),
        "office_jobs": ("CNS09", "CNS10", "CNS11", "CNS12", "CNS13", "CNS14"),
        "institution_jobs": ("CNS15", "CNS16", "CNS20"),
    }
    for state in sorted(cfg.get("lodes_states", [])):
        report_progress("loading_lodes", f"Streaming {state.upper()} LODES")

        def fetch(kind):
            spec = lodes_input(cfg, state, kind)
            record = _download_cached(spec, cfg)
            metadata["files"].append(
                {"url": spec.url, "cache_file": str(spec.path), "sha256": record["sha256"]}
            )
            return spec.path

        print(f"LODES: streaming {state.upper()} crosswalk and {cfg['lodes_year']} WAC/RAC.")
        crosswalk = fetch("xwalk")
        selected_blocks = {}
        invalid_coordinates = 0
        with gzip.open(crosswalk, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"tabblk2020", "blklatdd", "blklondd"}.issubset(reader.fieldnames or []):
                raise RuntimeError(f"Unexpected LODES8 crosswalk schema: {crosswalk}")
            for row in reader:
                try:
                    lat, lon = float(row["blklatdd"]), float(row["blklondd"])
                except (ValueError, TypeError):
                    invalid_coordinates += 1
                    continue
                if grid.south <= lat < grid.north and grid.west <= lon < grid.east:
                    x, y = grid.project(lon, lat)
                    radius = cfg.get("study_radius_km")
                    if radius and x * x + y * y > radius * radius:
                        continue
                    cell = grid.index(x, y)
                    if cell is not None:
                        selected_blocks[row["tabblk2020"]] = cell
        metadata.setdefault("selected_blocks", {})[state] = len(selected_blocks)
        metadata.setdefault("invalid_crosswalk_coordinates", {})[state] = invalid_coordinates
        for kind, geocode in (("wac", "w_geocode"), ("rac", "h_geocode")):
            path = fetch(kind)
            createdates = set()
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not {geocode, "C000"}.issubset(reader.fieldnames or []):
                    raise RuntimeError(f"Unexpected LODES {kind.upper()} schema: {path}")
                for row in reader:
                    cell = selected_blocks.get(row[geocode])
                    if cell is None:
                        continue
                    gx, gy = cell
                    arrays["jobs" if kind == "wac" else "residence_jobs"][gy, gx] += int(
                        row["C000"] or 0
                    )
                    if kind == "wac":
                        for name, columns in sectors.items():
                            arrays[name][gy, gx] += sum(
                                int(row.get(column, 0) or 0) for column in columns
                            )
                    if row.get("createdate"):
                        createdates.add(row["createdate"])
            metadata["files"][-1]["createdates"] = sorted(createdates)
    return {k: v.astype(np.float32) for k, v in arrays.items()}, metadata
