"""Polygons geometry helpers; extracted without behavior changes."""

from metrosim26.workers import mesh_triangles


def _oriented_ring(ring, counterclockwise=True):
    points = [(float(p[0]), float(p[1])) for p in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    signed = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))
    if (signed > 0.0) != counterclockwise:
        points.reverse()
    return points


def _surface_geometry(polygon, elevation_km=0.0):
    """Pure geometry helper; triangulation preserves multipolygon courtyard holes."""
    vertices, faces = [], []
    for triangle in mesh_triangles(polygon):
        triangle = _oriented_ring(triangle)
        if len(triangle) != 3:
            continue
        start = len(vertices)
        vertices.extend((x, y, elevation_km) for x, y in triangle)
        faces.append((start, start + 1, start + 2))
    return vertices, faces
