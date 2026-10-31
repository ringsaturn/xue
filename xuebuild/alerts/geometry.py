"""An upstream alert polygon → the MultiPolygon a network file carries:
Douglas–Peucker at 0.001° (about 100 m, under a pixel at the zooms a
warning is read at), then coordinates to four decimals, then the
positions rounding made repeat dropped. Pure NumPy: no shapely, no GEOS.

Simplification never turns a ring invalid by construction — it only
removes vertices — but it can collapse a sliver to fewer than four
positions. Such an outer ring falls back to its rounded, unsimplified
self; a hole that collapses is dropped (a hole a hundred metres across is
not a feature of a warning area); a polygon whose outer ring collapses
even unsimplified is dropped.
"""

from __future__ import annotations

from typing import Any

import numpy as np

TOLERANCE_DEGREES = 0.001
DECIMALS = 4


def _douglas_peucker(points: np.ndarray, tolerance: float) -> np.ndarray:
    """The kept vertices of an open polyline (first and last always kept);
    a closed ring passes as a polyline whose ends coincide, the distance
    to a zero-length chord being the distance to its point."""
    count = len(points)
    if count < 3:
        return points
    keep = np.zeros(count, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        segment = points[first + 1 : last]
        start, end = points[first], points[last]
        chord = end - start
        length = float(np.hypot(chord[0], chord[1]))
        offsets = segment - start
        if length == 0.0:
            distances = np.hypot(offsets[:, 0], offsets[:, 1])
        else:
            distances = np.abs(chord[0] * offsets[:, 1] - chord[1] * offsets[:, 0]) / length
        farthest = int(np.argmax(distances))
        if distances[farthest] > tolerance:
            middle = first + 1 + farthest
            keep[middle] = True
            stack.append((first, middle))
            stack.append((middle, last))
    return points[keep]


def _round_ring(points: np.ndarray) -> list[list[float]] | None:
    """Round to :data:`DECIMALS`, drop repeats, close; None when fewer
    than four positions (three distinct corners) remain."""
    rounded = np.round(points, DECIMALS)
    ring: list[list[float]] = []
    for lon, lat in rounded.tolist():
        position = [round(lon, DECIMALS) + 0.0, round(lat, DECIMALS) + 0.0]
        if not ring or ring[-1] != position:
            ring.append(position)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        return None
    ring.append(list(ring[0]))
    return ring


def _ring(coordinates: Any, tolerance: float) -> tuple[list[list[float]] | None, list[list[float]] | None]:
    """A ring simplified, and the same ring only rounded (the fallback)."""
    try:
        points = np.asarray([[float(point[0]), float(point[1])] for point in coordinates], dtype=np.float64)
    except (TypeError, ValueError, IndexError):
        return None, None
    if points.ndim != 2 or len(points) < 3 or not np.isfinite(points).all():
        return None, None
    return _round_ring(_douglas_peucker(points, tolerance)), _round_ring(points)


def simplify(geometry: Any, tolerance: float = TOLERANCE_DEGREES) -> dict[str, Any] | None:
    """A GeoJSON Polygon or MultiPolygon → a simplified MultiPolygon, or
    None when nothing with an area survives (or there was no polygon)."""
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = coordinates
    else:
        return None
    if not isinstance(polygons, list):
        return None
    result: list[list[list[list[float]]]] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            continue
        outer, fallback = _ring(polygon[0], tolerance)
        outer = outer or fallback
        if outer is None:
            continue
        rings = [outer]
        for hole in polygon[1:]:
            simplified, _ = _ring(hole, tolerance)
            if simplified is not None:
                rings.append(simplified)
        result.append(rings)
    if not result:
        return None
    return {"type": "MultiPolygon", "coordinates": result}


def bbox(geometry: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    """``(west, south, east, north)`` of a simplified MultiPolygon: plain
    minimum and maximum, the coordinates being four-decimal already."""
    if geometry is None:
        return None
    lons = [point[0] for polygon in geometry["coordinates"] for ring in polygon for point in ring]
    lats = [point[1] for polygon in geometry["coordinates"] for ring in polygon for point in ring]
    return min(lons), min(lats), max(lons), max(lats)
