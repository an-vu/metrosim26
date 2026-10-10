"""Packets geometry helpers; extracted without behavior changes."""

import math

import numpy as np


class PacketBatch:
    """Preserve face geometry while bounding each Blender mesh import."""

    def __init__(
        self,
        directory,
        packets,
        collection,
        name,
        material,
        max_vertices,
        lifetime=None,
        properties=None,
        radius=None,
        inner_radius=0,
        *,
        weld=False,
        radial_batches=True,
    ):
        self.directory, self.packets = directory, packets
        self.collection, self.name, self.material = collection, name, material
        self.limit = max(1000, max_vertices)
        self.lifetime = lifetime
        self.properties = properties or {}
        self.vertices, self.faces = [], []
        self.weld = weld
        self.radial_batches = radial_batches
        self.vertex_lookup = {}
        self.chunk = 0
        self.radius = radius
        self.inner_radius = inner_radius

    def add(self, vertices, faces):
        if self.radius:
            clipped_vertices, clipped_faces = [], []
            for face in faces:
                polygon = [np.asarray(vertices[i], dtype=float) for i in face]
                if not all(
                    np.hypot(p[0], p[1]) <= self.radius * math.cos(math.pi / 96) for p in polygon
                ):
                    for angle in np.arange(96) * (2 * math.pi / 96):
                        normal = np.array([math.cos(angle), math.sin(angle)])
                        limit = self.radius * math.cos(math.pi / 96)
                        output = []
                        for a, b in zip(polygon, polygon[1:] + polygon[:1]):
                            da, db = np.dot(a[:2], normal) - limit, np.dot(b[:2], normal) - limit
                            if da <= 0:
                                output.append(a)
                            if (da <= 0) != (db <= 0):
                                output.append(a + (b - a) * da / (da - db))
                        polygon = output
                        if len(polygon) < 3:
                            break
                if len(polygon) >= 3:
                    offset = len(clipped_vertices)
                    clipped_vertices.extend(polygon)
                    clipped_faces.append(list(range(offset, offset + len(polygon))))
            vertices, faces = clipped_vertices, clipped_faces
        if self.inner_radius and vertices:
            from metrosim26.geography import Geography

            output_vertices, output_faces = [], []
            limit = self.inner_radius * math.cos(math.pi / 96)
            for face in faces:
                polygon = [tuple(vertices[index]) for index in face]
                if all(math.hypot(p[0], p[1]) <= limit for p in polygon):
                    continue
                x0, x1 = min(p[0] for p in polygon), max(p[0] for p in polygon)
                y0, y1 = min(p[1] for p in polygon), max(p[1] for p in polygon)
                distance = math.hypot(max(x0, min(0, x1)), max(y0, min(0, y1)))
                pieces = []
                if distance >= self.inner_radius:
                    pieces = [polygon]
                else:
                    remaining = polygon
                    for angle in np.arange(96) * (2 * math.pi / 96):
                        normal = (math.cos(angle), math.sin(angle))
                        outside = Geography.clip_polygon(
                            remaining, (-normal[0], -normal[1]), -limit
                        )
                        if len(outside) >= 3:
                            pieces.append(outside)
                        remaining = Geography.clip_polygon(remaining, normal, limit)
                        if len(remaining) < 3:
                            break
                for piece in pieces:
                    offset = len(output_vertices)
                    output_vertices.extend(piece)
                    output_faces.append(list(range(offset, offset + len(piece))))
            vertices, faces = output_vertices, output_faces
        if self.radius and vertices and self.radial_batches:
            band = int(
                np.mean([np.hypot(v[0], v[1]) for v in vertices]) / max(0.25, self.radius / 20)
            )
            if getattr(self, "band", None) is None or band > self.band:
                self.flush()
                self.band = band
        remap = {}
        for face in faces:
            if len(self.vertices) + len(face) > self.limit:
                self.flush()
                remap = {}
            indices = []
            for index in face:
                if index not in remap:
                    key = tuple(round(float(value), 9) for value in vertices[index])
                    shared = self.vertex_lookup.get(key) if self.weld else None
                    if shared is None:
                        shared = len(self.vertices)
                        self.vertices.append(vertices[index])
                        if self.weld:
                            self.vertex_lookup[key] = shared
                    remap[index] = shared
                indices.append(remap[index])
            self.faces.append(indices)

    def flush(self):
        if not self.faces:
            return
        filename = f"mesh_{len(self.packets):06d}.npz"
        offsets = np.cumsum([0] + [len(face) for face in self.faces], dtype=np.int64)
        np.savez_compressed(
            self.directory / filename,
            vertices=np.asarray(self.vertices, dtype=np.float64),
            indices=np.asarray([v for face in self.faces for v in face], dtype=np.int32),
            offsets=offsets,
        )
        self.packets.append(
            dict(
                file=filename,
                name=f"{self.name}_{self.chunk:03d}",
                collection=self.collection,
                material=self.material,
                lifetime=self.lifetime,
                properties=self.properties,
                min_z=float(np.min(np.asarray(self.vertices)[:, 2])),
                max_z=float(np.max(np.asarray(self.vertices)[:, 2])),
                distance=float(
                    np.min(
                        np.hypot(np.asarray(self.vertices)[:, 0], np.asarray(self.vertices)[:, 1])
                    )
                ),
            )
        )
        self.chunk += 1
        self.vertices, self.faces = [], []
        self.vertex_lookup.clear()
