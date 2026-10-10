"""Coordinate parsing and field synchronization, separate from panel layout."""

import re

_UPDATING = False


def parse_coordinates(text, axis="latitude"):
    """Accept decimal coordinates, optional degrees and compass directions."""
    normalized = text.strip().upper().replace("−", "-").replace("°", "")
    matches = re.findall(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([NSEW]?)", normalized)
    remainder = re.sub(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*[NSEW]?", "", normalized)
    if len(matches) not in (1, 2) or remainder.strip(" ,;()\t\n"):
        raise ValueError("Use decimal latitude, longitude; for example 22.29660 N, 113.95100 E")
    result = {}
    for index, (number, direction) in enumerate(matches):
        key = (
            ("latitude" if direction in ("N", "S") else "longitude")
            if direction
            else (axis if len(matches) == 1 else ("latitude" if index == 0 else "longitude"))
        )
        value = float(number)
        if direction:
            value = abs(value) * (-1 if direction in ("S", "W") else 1)
        if key in result or abs(value) > (85 if key == "latitude" else 180):
            raise ValueError("Latitude must be within 85° N/S; longitude within 180° E/W")
        result[key] = value
    return result


def coordinate_text_changed(settings, axis):
    global _UPDATING
    if _UPDATING:
        return
    try:
        values = parse_coordinates(getattr(settings, "coordinate_" + axis), axis)
    except ValueError as error:
        settings.coordinate_error = str(error)
        return
    _UPDATING = True
    try:
        for key, value in values.items():
            setattr(settings, key, value)
            setattr(settings, "coordinate_" + key, f"{value:.5f}")
        settings.coordinate_error = ""
    finally:
        _UPDATING = False


def latitude_text_changed(settings, context):
    coordinate_text_changed(settings, "latitude")


def longitude_text_changed(settings, context):
    coordinate_text_changed(settings, "longitude")
