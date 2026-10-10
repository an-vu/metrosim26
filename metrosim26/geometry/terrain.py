"""Elevation sampling and alignment; no ocean generation or Blender code."""

import math

import numpy as np


class Terrain:
    def height(self, x, y):
        if not self.tiles:
            return 0.0
        x = min(self.grid.max_x - 1e-8, max(self.grid.min_x + 1e-8, x))
        y = min(self.grid.max_y - 1e-8, max(self.grid.min_y + 1e-8, y))
        lon = self.grid.center_lon + x / (111.320 * self.grid.cos_lat)
        lat = self.grid.center_lat + y / 110.574
        south, west = math.floor(lat), math.floor(lon)
        tile = self.tiles.get((south, west))
        if tile is None:
            raise ValueError("Elevation tile missing at the study boundary. Download Area again.")
        size = tile.shape[0] - 1
        fx, fy = (lon - west) * size, (south + 1 - lat) * size
        ix, iy = min(size - 1, max(0, int(fx))), min(size - 1, max(0, int(fy)))
        dx, dy = fx - ix, fy - iy
        samples = tile[iy : iy + 2, ix : ix + 2].astype(float)
        if np.any(samples == -32768):
            raise ValueError(
                "Elevation data has missing samples here. Disable Terrain or choose another area."
            )
        return (
            max(
                0.0,
                samples[0, 0] * (1 - dx) * (1 - dy)
                + samples[0, 1] * dx * (1 - dy)
                + samples[1, 0] * (1 - dx) * dy
                + samples[1, 1] * dx * dy,
            )
            / 1000
        )

    def surface_height(self, x, y):
        """Height on the same fixed triangle lattice used by the terrain mesh."""
        step = getattr(self, "spacing", 0.03)
        ix, iy = math.floor(x / step), math.floor(y / step)
        x0, y0 = ix * step, iy * step
        u, v = (x - x0) / step, (y - y0) / step
        h00, h10 = self.height(x0, y0), self.height(x0 + step, y0)
        h01, h11 = self.height(x0, y0 + step), self.height(x0 + step, y0 + step)
        return (
            h00 + u * (h10 - h00) + v * (h11 - h10)
            if u >= v
            else h00 + v * (h01 - h00) + u * (h11 - h01)
        )

    @staticmethod
    def clip_polygon(points, normal, limit):
        output = []
        for a, b in zip(points, points[1:] + points[:1]):
            da = a[0] * normal[0] + a[1] * normal[1] - limit
            db = b[0] * normal[0] + b[1] * normal[1] - limit
            if da <= 1e-10:
                output.append(a)
            if (da <= 0) != (db <= 0):
                t = da / (da - db)
                output.append(tuple(a[k] + t * (b[k] - a[k]) for k in range(3)))
        return output

    def terrain_pieces(self, points):
        step = self.spacing
        for iy in range(
            math.floor(min(p[1] for p in points) / step),
            math.floor(max(p[1] for p in points) / step) + 1,
        ):
            for ix in range(
                math.floor(min(p[0] for p in points) / step),
                math.floor(max(p[0] for p in points) / step) + 1,
            ):
                polygon = points
                for normal, limit in (
                    ((-1, 0), -ix * step),
                    ((1, 0), (ix + 1) * step),
                    ((0, -1), -iy * step),
                    ((0, 1), (iy + 1) * step),
                ):
                    polygon = self.clip_polygon(polygon, normal, limit)
                    if len(polygon) < 3:
                        break
                if len(polygon) < 3:
                    continue
                for normal, limit in (((-1, 1), (iy - ix) * step), ((1, -1), (ix - iy) * step)):
                    triangle = self.clip_polygon(polygon, normal, limit)
                    if len(triangle) >= 3:
                        yield triangle

    def road_points(self, points):
        if not self.tiles or len(points) < 2:
            return points
        result = [points[0]]
        for a, b in zip(points, points[1:]):
            count = max(1, math.ceil(math.dist(a, b) / (self.spacing * 0.75)))
            result.extend(
                tuple(a[k] + (b[k] - a[k]) * i / count for k in range(2))
                for i in range(1, count + 1)
            )
        return result

    def drape_road(self, vertices, faces, bridge=False):
        if not self.tiles:
            return vertices, faces
        if bridge:
            return self.lift(vertices), faces
        return self.drape_surface(vertices, faces)

    def drape_surface(self, vertices, faces, clearance=0.0003):
        """Align polygons to terrain triangles; callers choose their surface clearance."""
        if not self.tiles:
            return vertices, faces
        output, polygons = [], []
        for face in faces:
            for piece in self.terrain_pieces([vertices[index] for index in face]):
                offset = len(output)
                output.extend((x, y, self.surface_height(x, y) + clearance) for x, y, _ in piece)
                polygons.extend(
                    [[offset, offset + i, offset + i + 1] for i in range(1, len(piece) - 1)]
                )
        return output, polygons

    def lift(self, vertices, building=False, water=False):
        if not vertices or not self.tiles:
            return vertices
        levels = [self.surface_height(v[0], v[1]) for v in vertices]
        highest = max(levels)
        mean = float(np.mean(levels))
        return [
            (v[0], v[1], v[2] + (mean if water else highest if building and v[2] > 1e-8 else h))
            for v, h in zip(vertices, levels)
        ]
