"""Coastline classification on the terrain lattice; yields land/sea polygons."""

import math

import numpy as np


def surface(self):
    """Sample terrain inland; split coastal cells along coastline segments."""
    step = getattr(self, "spacing", max(0.03, max(self.grid.width, self.grid.height) / 200))
    nx = math.ceil(self.grid.max_x / step) - math.floor(self.grid.min_x / step)
    ny = math.ceil(self.grid.max_y / step) - math.floor(self.grid.min_y / step)
    if nx * ny > 400000:
        raise ValueError(
            "Expanded terrain exceeds 400,000 cells. Choose Rebuild for a coarser grid."
        )
    xs = np.arange(math.floor(self.grid.min_x / step), math.ceil(self.grid.max_x / step) + 1) * step
    ys = np.arange(math.floor(self.grid.min_y / step), math.ceil(self.grid.max_y / step) + 1) * step
    xx, yy = np.meshgrid((xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2)
    closest = np.full(xx.shape, np.inf)
    sea = np.zeros(xx.shape, dtype=bool)
    for a, b in self.segments:
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        t = np.clip(((xx - ax) * dx + (yy - ay) * dy) / (dx * dx + dy * dy), 0, 1)
        distance = (xx - ax - t * dx) ** 2 + (yy - ay - t * dy) ** 2
        nearer = distance < closest
        sea[nearer] = (dx * (yy - ay) - dy * (xx - ax))[nearer] < 0
        closest[nearer] = distance[nearer]
    segments = np.asarray(self.segments, dtype=float).reshape(-1, 2, 2)
    if len(segments):
        low, high = segments.min(axis=1), segments.max(axis=1)

    def split(polygon, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        sides = [dx * (p[1] - a[1]) - dy * (p[0] - a[0]) for p in polygon]
        if min(sides) >= -1e-10 or max(sides) <= 1e-10:
            return [polygon]
        left, right = [], []
        for i, point in enumerate(polygon):
            following = polygon[(i + 1) % len(polygon)]
            da, db = sides[i], sides[(i + 1) % len(polygon)]
            if da >= -1e-10:
                left.append(point)
            if da <= 1e-10:
                right.append(point)
            if (da > 1e-10 and db < -1e-10) or (da < -1e-10 and db > 1e-10):
                t = da / (da - db)
                crossing = tuple(point[k] + t * (following[k] - point[k]) for k in range(3))
                left.append(crossing)
                right.append(crossing)
        return [part for part in (left, right) if len(part) >= 3]

    def is_sea(polygon):
        center = np.mean(np.asarray(polygon)[:, :2], axis=0)
        a, direction = segments[:, 0], segments[:, 1] - segments[:, 0]
        t = np.clip(np.sum((center - a) * direction, axis=1) / np.sum(direction**2, axis=1), 0, 1)
        nearest = np.argmin(np.sum((center - a - t[:, None] * direction) ** 2, axis=1))
        dx, dy = direction[nearest]
        return bool(dx * (center[1] - a[nearest, 1]) - dy * (center[0] - a[nearest, 0]) < 0)

    # Center-outward ordering keeps progressive surface chunks coherent.
    for index in np.argsort((xx * xx + yy * yy).ravel()):
        iy, ix = np.unravel_index(index, xx.shape)
        points = [
            (xs[ix], ys[iy], 0),
            (xs[ix + 1], ys[iy], 0),
            (xs[ix + 1], ys[iy + 1], 0),
            (xs[ix], ys[iy + 1], 0),
        ]
        inner = getattr(self, "inner_radius", 0)
        if inner and all(math.hypot(p[0], p[1]) <= inner * math.cos(math.pi / 96) for p in points):
            continue
        # Split ground along the same diagonal used by surface_height.
        parts = [points[:3], [points[0], points[2], points[3]]]
        if len(segments):
            nearby = np.flatnonzero(
                (low[:, 0] <= xs[ix + 1])
                & (high[:, 0] >= xs[ix])
                & (low[:, 1] <= ys[iy + 1])
                & (high[:, 1] >= ys[iy])
            )
            for index in nearby:
                a, b = segments[index]
                parts = [piece for part in parts for piece in split(part, a, b)]
            if len(nearby):
                for part in parts:
                    for normal, limit in (
                        ((-1, 0), -self.grid.min_x),
                        ((1, 0), self.grid.max_x),
                        ((0, -1), -self.grid.min_y),
                        ((0, 1), self.grid.max_y),
                    ):
                        part = self.clip_polygon(part, normal, limit)
                        if len(part) < 3:
                            break
                    if len(part) >= 3:
                        yield is_sea(part), part
                continue
        for part in parts:
            for normal, limit in (
                ((-1, 0), -self.grid.min_x),
                ((1, 0), self.grid.max_x),
                ((0, -1), -self.grid.min_y),
                ((0, 1), self.grid.max_y),
            ):
                part = self.clip_polygon(part, normal, limit)
                if len(part) < 3:
                    break
            if len(part) >= 3:
                yield bool(sea[iy, ix]), part
