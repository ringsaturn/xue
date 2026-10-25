"""Build a showcase case's context file: the reference lines and areas the
shell draws under its markers (``showcase/README.md``, ``context`` block).

The geometry comes from OpenStreetMap through the Overpass API, clipped to
the case's box and simplified to what a regional view resolves. Borders and
rivers are taken across the whole box; roads, tracks and streams only inside
one OSM area (``--detail-area``, an administrative relation), since across a
box of several degrees they outweigh everything else and the case is about
one side of it. Protected areas are named relation by relation: a box holds
many, and the case is about one. Features drawn by hand or computed (a
route, a line read off imagery, a measured distance) are merged in from
GeoJSON files that already carry their ``kind`` and, optionally, a
``label``.

Needs shapely (``uv sync --group context``)::

    python -m xuebuild.context showcase/cases-local/arxan-fire-2026.json \\
        --detail-area 7304690 --protected 21785019 --extra routes.geojson
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .showcase import CONTEXT_KINDS, SHOWCASE_DIRECTORY, parse_case, read_context

LOG = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "xue-context (+https://github.com/ringsaturn/xue)"

SIMPLIFY_DEGREES = 0.0002
"""About 20 m: below a pixel at the zooms a case is looked at."""

DECIMALS = 5

ROAD_KINDS = {
    "track": "track",
    "unclassified": "road",
    "tertiary": "road",
    "secondary": "road",
    "primary": "road",
    "trunk": "road",
    "motorway": "road",
    "residential": "road",
    "service": "road",
}
"""OSM ``highway`` values kept, and the kind each is drawn as."""

WATERWAY_KINDS = {"river": "river", "stream": "stream"}


def overpass(query: str, attempts: int = 5) -> list[dict[str, Any]]:
    """One Overpass query's elements; the public server refuses work when
    it is busy, so a refusal is retried with a growing wait."""
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(attempts):
        request = urllib.request.Request(OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)["elements"]
        except (OSError, ValueError) as exc:
            if attempt == attempts - 1:
                raise
            wait = 15 * 2**attempt
            LOG.warning("overpass: %s; retrying in %d s", exc, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _line(element: dict[str, Any]) -> list[tuple[float, float]]:
    return [(point["lon"], point["lat"]) for point in element.get("geometry", [])]


def fetch_features(
    bbox: tuple[float, float, float, float], detail_area: int | None, protected: list[int]
) -> list[tuple[str, Any]]:
    """``(kind, shapely geometry)`` for every OSM feature the case draws,
    unclipped."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    west, south, east, north = bbox
    box = f"({south},{west},{north},{east})"
    features: list[tuple[str, Any]] = []

    for element in overpass(f'[out:json][timeout:300];way["boundary"="administrative"]["admin_level"="2"]{box};out geom;'):
        if len(line := _line(element)) >= 2:
            features.append(("border", LineString(line)))

    rivers = '"waterway"="river"' if detail_area is not None else '"waterway"~"^(river|stream)$"'
    for element in overpass(f"[out:json][timeout:300];way[{rivers}]{box};out geom tags;"):
        kind = WATERWAY_KINDS.get(element["tags"].get("waterway", ""))
        if kind and len(line := _line(element)) >= 2:
            features.append((kind, LineString(line)))

    if detail_area is not None:
        # Overpass numbers a relation's area 3600000000 above its id.
        area = f"area({3600000000 + detail_area})->.a;"
        highways = "|".join(ROAD_KINDS)
        query = (
            f'[out:json][timeout:300];{area}(way["highway"~"^({highways})$"](area.a);'
            f'way["waterway"="stream"](area.a););out geom tags;'
        )
        for element in overpass(query):
            tags = element["tags"]
            kind = ROAD_KINDS.get(tags.get("highway", "")) or WATERWAY_KINDS.get(tags.get("waterway", ""))
            if kind and len(line := _line(element)) >= 2:
                features.append((kind, LineString(line)))

    for relation_id in protected:
        (relation,) = overpass(f"[out:json][timeout:300];rel(id:{relation_id});out geom;")
        rings = {
            role: unary_union(
                [LineString(line) for member in relation["members"] if member["type"] == "way" and member["role"] == role
                 if len(line := _line(member)) >= 2]
            )
            for role in ("outer", "inner")
        }
        area_geometry = unary_union(list(polygonize(rings["outer"])))
        if not rings["inner"].is_empty:
            area_geometry = area_geometry.difference(unary_union(list(polygonize(rings["inner"]))))
        if area_geometry.is_empty:
            raise ValueError(f"protected area relation {relation_id} has no closed outer ring")
        features.append(("protected-area", area_geometry))
    return features


def _rounded(geometry: dict[str, Any]) -> dict[str, Any]:
    def walk(value: Any) -> Any:
        if isinstance(value[0], (int, float)):
            return [round(value[0], DECIMALS), round(value[1], DECIMALS)]
        return [walk(part) for part in value]

    return {"type": geometry["type"], "coordinates": walk(geometry["coordinates"])}


def build_collection(
    features: list[tuple[str, Any]], bbox: tuple[float, float, float, float], extras: list[dict[str, Any]]
) -> dict[str, Any]:
    """The context FeatureCollection: each feature clipped to the box,
    simplified and split into the line and polygon types the file admits,
    the hand-made ones merged unsimplified, each ``kind`` checked."""
    from shapely.geometry import box as shapely_box
    from shapely.geometry import mapping, shape
    from shapely.ops import linemerge, unary_union

    clip = shapely_box(*bbox)
    grouped: dict[str, list[Any]] = {}
    for kind, geometry in features:
        grouped.setdefault(kind, []).append(geometry)
    out: list[dict[str, Any]] = []
    # Most-drawn first, so a road sits above the area it crosses.
    for kind in CONTEXT_KINDS:
        if kind not in grouped:
            continue
        merged = unary_union(grouped[kind])
        if merged.geom_type in ("LineString", "MultiLineString"):
            merged = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
        clipped = merged.intersection(clip).simplify(SIMPLIFY_DEGREES, preserve_topology=True)
        parts = getattr(clipped, "geoms", [clipped])
        lines = [part for part in parts if part.geom_type in ("LineString", "MultiLineString") and not part.is_empty]
        polygons = [part for part in parts if part.geom_type in ("Polygon", "MultiPolygon") and not part.is_empty]
        for group in (lines, polygons):
            if group:
                out.append({"type": "Feature", "properties": {"kind": kind}, "geometry": _rounded(mapping(unary_union(group)))})
    for collection in extras:
        for feature in collection["features"]:
            kind = feature.get("properties", {}).get("kind")
            if kind not in CONTEXT_KINDS:
                raise ValueError(f"an extra feature's kind {kind!r} is not one of {CONTEXT_KINDS}")
            geometry = shape(feature["geometry"]).intersection(clip)
            if geometry.is_empty:
                continue
            properties = {"kind": kind}
            if "label" in feature["properties"]:
                properties["label"] = feature["properties"]["label"]
            out.append({"type": "Feature", "properties": properties, "geometry": _rounded(mapping(geometry))})
    return {"type": "FeatureCollection", "features": out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xuebuild.context", description=__doc__.split("\n\n")[0])
    parser.add_argument("case", type=Path, help="the case definition, whose bbox and context.file are used")
    parser.add_argument("--data-root", type=Path, default=Path("web/public/data"))
    parser.add_argument("--detail-area", type=int, help="OSM relation id roads, tracks and streams are taken inside")
    parser.add_argument("--protected", type=int, action="append", default=[], help="OSM relation id of a protected area")
    parser.add_argument("--extra", type=Path, action="append", default=[], help="a GeoJSON file of features carrying their kind")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    spec = parse_case(json.loads(args.case.read_text(encoding="utf-8")))
    if spec.context is None:
        parser.error(f"case {spec.id} names no context block")
    extras = [json.loads(path.read_text(encoding="utf-8")) for path in args.extra]
    collection = build_collection(fetch_features(spec.bbox, args.detail_area, args.protected), spec.bbox, extras)
    data = json.dumps(collection, separators=(",", ":"), ensure_ascii=False).encode()
    read_context(data, spec.bbox)
    target = args.data_root / SHOWCASE_DIRECTORY / spec.id / spec.context["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    LOG.info("wrote %s (%d features, %d bytes)", target, len(collection["features"]), len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
