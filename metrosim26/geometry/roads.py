"""Roads geometry helpers; extracted without behavior changes."""

import math


def _road_geometry(points, width_km, elevation_km=0.00002):
    """Flat road strips with bounded miter joins; no cylindrical road tubes."""
    clean = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not clean or math.dist(clean[-1], xy) > 1e-9:
            clean.append(xy)
    if len(clean) < 2:
        return [], []
    normals = []
    for a, b in zip(clean, clean[1:]):
        length = math.dist(a, b)
        normals.append((-(b[1] - a[1]) / length, (b[0] - a[0]) / length))
    half = max(float(width_km), 0.001) * 0.5
    vertices, faces = [], []
    for i, (x, y) in enumerate(clean):
        before = normals[max(0, i - 1)]
        after = normals[min(i, len(normals) - 1)]
        mx, my = before[0] + after[0], before[1] + after[1]
        length = math.hypot(mx, my)
        if length < 1e-8:
            mx, my = after
            extent = half
        else:
            mx, my = mx / length, my / length
            dot = max(0.25, mx * after[0] + my * after[1])
            extent = min(half / dot, half * 2.0)
        vertices.extend(
            (
                (x + mx * extent, y + my * extent, elevation_km),
                (x - mx * extent, y - my * extent, elevation_km),
            )
        )
        if i:
            start = 2 * i
            faces.append((start - 2, start - 1, start + 1, start))
    return vertices, faces
