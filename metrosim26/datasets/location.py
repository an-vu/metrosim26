"""Data location: isolated acquisition and preparation responsibilities."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from metrosim26.datasets.files import _data_signature, atomic_json


def lookup_location(latitude, longitude, cache_dir):
    """One user-triggered reverse lookup, cached by signed coordinates."""
    key = f"{float(latitude):.5f},{float(longitude):.5f}"
    cache_key = "detailed-v1:" + key
    directory = Path(cache_dir) / "Locations"
    path = directory / (_data_signature(cache_key) + ".json")
    if path.is_file():
        return json.loads(path.read_text())
    endpoint = os.environ.get(
        "METROSIM26_GEOCODER_URL", "https://nominatim.openstreetmap.org/reverse"
    )
    query = urllib.parse.urlencode(
        {"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 14, "accept-language": "en"}
    )
    request = urllib.request.Request(
        endpoint + "?" + query,
        headers={"User-Agent": "MetroSim26/3 (https://github.com/an-vu/metrosim26)"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    address = result.get("address", {})
    city = next(
        (
            address.get(k)
            for k in ("town", "city", "village", "municipality", "county")
            if address.get(k)
        ),
        "",
    )
    locality = next(
        (address.get(k) for k in ("neighbourhood", "quarter", "hamlet") if address.get(k)), ""
    )
    suburb = address.get("suburb")
    if suburb and city and suburb.lower().startswith(city.lower() + " "):
        suburb = None
    parts = list(
        dict.fromkeys(
            v
            for v in (
                locality,
                suburb,
                city,
                address.get("city") if address.get("city") != city else None,
                address.get("state") or address.get("region"),
                address.get("country"),
            )
            if v
        )
    )
    if not parts:
        raise ValueError("No place name found for these coordinates")
    record = {
        "coordinates": key,
        "label": ", ".join(parts),
    }
    atomic_json(path, record)
    return record
