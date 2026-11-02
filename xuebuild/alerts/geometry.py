"""An upstream alert polygon → the MultiPolygon a network file carries:
the polygon as drawn, not simplified, with coordinates rounded to four
decimals (about 11 m) and the positions rounding made repeat dropped.
Pure NumPy: no shapely, no GEOS.

A ring that rounding leaves with fewer than three distinct corners has no
area: an outer ring like that drops its polygon, a hole like that is
dropped.
"""

from __future__ import annotations

from typing import Any

import numpy as np

DECIMALS = 4


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


def _ring(coordinates: Any) -> list[list[float]] | None:
    """A ring rounded, or None when it is malformed or has no area."""
    try:
        points = np.asarray([[float(point[0]), float(point[1])] for point in coordinates], dtype=np.float64)
    except (TypeError, ValueError, IndexError):
        return None
    if points.ndim != 2 or len(points) < 3 or not np.isfinite(points).all():
        return None
    return _round_ring(points)


def normalize(geometry: Any) -> dict[str, Any] | None:
    """A GeoJSON Polygon or MultiPolygon → a rounded MultiPolygon, or
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
        outer = _ring(polygon[0])
        if outer is None:
            continue
        rings = [outer]
        for hole in polygon[1:]:
            ring = _ring(hole)
            if ring is not None:
                rings.append(ring)
        result.append(rings)
    if not result:
        return None
    return {"type": "MultiPolygon", "coordinates": result}


def bbox(geometry: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    """``(west, south, east, north)`` of a normalized MultiPolygon: plain
    minimum and maximum, the coordinates being four-decimal already."""
    if geometry is None:
        return None
    lons = [point[0] for polygon in geometry["coordinates"] for ring in polygon for point in ring]
    lats = [point[1] for polygon in geometry["coordinates"] for ring in polygon for point in ring]
    return min(lons), min(lats), max(lons), max(lats)
