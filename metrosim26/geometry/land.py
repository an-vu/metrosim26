"""Ground and park geometry policy; water is handled separately."""

from .polygons import _surface_geometry


def ground_batch(make_batch, material, chunk_size):
    return make_batch("ground", "Study_Ground", material, chunk_size, inner_radius=0, weld=True)


def ground_geometry(points, geography):
    vertices = geography.lift([(x, y, z - 0.00002) for x, y, z in points])
    return vertices, [list(range(len(points)))]


def park_geometry(polygon, geography):
    vertices, faces = _surface_geometry(polygon, -0.000005)
    return geography.lift(vertices), faces
