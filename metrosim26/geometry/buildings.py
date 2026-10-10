"""Buildings geometry helpers; extracted without behavior changes."""

from .polygons import _oriented_ring, _surface_geometry


def _building_geometry(polygon, height_m, z0_m=0.0):
    """Extrude actual footprints, including courtyard walls and a holed roof."""
    # Centimeter-scale offsets separate flat presentation surfaces without
    # visibly lifting buildings off the ground.
    floor = 0.00008 + float(z0_m) / 1000.0
    roof = floor + max(0.1, float(height_m)) / 1000.0
    vertices, faces = _surface_geometry(polygon, roof)
    lower_vertices, lower_faces = _surface_geometry(polygon, floor)
    offset = len(vertices)
    vertices.extend(lower_vertices)
    faces.extend(tuple(offset + i for i in reversed(face)) for face in lower_faces)
    rings = [(polygon["outer"], True)]
    rings.extend((hole, False) for hole in polygon.get("holes", []))
    for ring, counterclockwise in rings:
        ring = _oriented_ring(ring, counterclockwise)
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if a == b:
                continue
            offset = len(vertices)
            vertices.extend(
                ((a[0], a[1], floor), (b[0], b[1], floor), (b[0], b[1], roof), (a[0], a[1], roof))
            )
            faces.append((offset, offset + 1, offset + 2, offset + 3))
    return vertices, faces
