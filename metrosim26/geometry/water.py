"""Water geometry policy, independent of land and presentation effects."""

from .polygons import _surface_geometry


def ocean_batch(make_batch, material, chunk_size):
    return make_batch("land", "Ocean", material, chunk_size, weld=True, radial_batches=False)


def inland_geometry(polygon, geography):
    """Keep mapped water outlines above the terrain without filling their holes."""
    vertices, faces = _surface_geometry(polygon, 0.0)
    if geography.tiles:
        return geography.drape_surface(vertices, faces, clearance=0.0003)
    return geography.lift(vertices, water=True), faces
