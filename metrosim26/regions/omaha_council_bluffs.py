"""Omaha–Council Bluffs growth coverage and employment inputs."""

FULL_BOUNDS = (40.99031149984831, -96.43156075030879, 41.453886499728576, -95.61498024942590)
PREVIEW_BOUNDS = (41.22, -96.06, 41.31, -95.89)
DOWNTOWN_ANCHOR = [-95.9345, 41.2565]
LODES_STATES = ["ne", "ia"]
STATE_NAMES = {"ne": "Nebraska", "ia": "Iowa"}


def validate_growth_bounds(bounds):
    south, west, north, east = bounds
    s, w, n, e = FULL_BOUNDS
    if not (s <= south < north <= n and w <= west < east <= e):
        raise ValueError(
            "Growth is not supported here yet. Employment data/model currently cover Omaha / Council Bluffs. Choose 3D map to view this location."
        )
