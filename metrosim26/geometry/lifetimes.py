"""Lifetimes geometry helpers; extracted without behavior changes."""


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
