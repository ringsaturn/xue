"""The ``cma-areas`` boundary set's source: the Amap province, prefecture
and county outlines packaged by cnmaps-data, one GeoJSON file per
administrative division named by its six-digit code (``docs/alerts.md``
§9.4).

Amap draws in GCJ-02, the mainland's obfuscated datum, a few hundred
metres off WGS84 and varying with position; :func:`prepare` turns every
file into WGS84 and files it under the layer its code's level names, so
the boundary tool reads three ordinary GeoJSON layers. The inverse of
GCJ-02 has no closed form: it is solved by fixed-point iteration on the
published forward formula, which converges to well under a centimetre in
a few steps.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from .schema import AlertsProductError

LAND = Path("cnmaps_data/data/datasets/administrative/amap/land")
"""Where a cnmaps-data checkout keeps the land outlines, one file per code."""

COUNTRY = "100000"
"""The national outline, which no warning names."""

ALIASES = {"654203": "654223"}
"""Codes the warnings name that the release files under an older code:
沙湾市 since it became a city in 2021 (the release names it 沙湾市 under
the county's 654223). Each is drawn twice, under both codes. 新林区 and
呼中区 (232703, 232704), 大兴安岭's management districts, have no outline
in the release and stay unresolved."""

_A = 6378245.0
_EE = 0.00669342162296594323


def _delta(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = lon - 105.0
    y = lat - 35.0
    dlat = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * np.sqrt(np.abs(x))
        + (20.0 * np.sin(6.0 * x * math.pi) + 20.0 * np.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * np.sin(y * math.pi) + 40.0 * np.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        + (160.0 * np.sin(y / 12.0 * math.pi) + 320.0 * np.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    )
    dlon = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * np.sqrt(np.abs(x))
        + (20.0 * np.sin(6.0 * x * math.pi) + 20.0 * np.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * np.sin(x * math.pi) + 40.0 * np.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        + (150.0 * np.sin(x / 12.0 * math.pi) + 300.0 * np.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    )
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * np.sin(rad) ** 2
    sqrt_magic = np.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * np.cos(rad) * math.pi)
    return dlon, dlat


def gcj_to_wgs(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    wlon, wlat = lon.copy(), lat.copy()
    for _ in range(8):
        dlon, dlat = _delta(wlon, wlat)
        wlon -= wlon + dlon - lon
        wlat -= wlat + dlat - lat
    return wlon, wlat


def layer_of(code: str) -> str:
    """A six-digit code's level: ``xx0000`` a province, ``xxxx00`` a
    prefecture, anything else a county (a district, a county-level city)."""
    if code.endswith("0000"):
        return "cn_province"
    if code.endswith("00"):
        return "cn_prefecture"
    return "cn_county"


def _to_wgs(coordinates: Any) -> Any:
    """A GeoJSON coordinate array of any depth, GCJ-02 → WGS84."""
    if coordinates and isinstance(coordinates[0], list) and coordinates[0] and isinstance(coordinates[0][0], (int, float)):
        ring = np.asarray([position[:2] for position in coordinates], dtype=np.float64)
        lon, lat = gcj_to_wgs(ring[:, 0], ring[:, 1])
        return np.column_stack((lon, lat)).tolist()
    return [_to_wgs(part) for part in coordinates]


def _geometry(document: dict[str, Any]) -> dict[str, Any]:
    geometry = document.get("geometry") if document.get("type") == "Feature" else document
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise AlertsProductError(f"not a polygon: {document.get('type')}")
    return {"type": geometry["type"], "coordinates": _to_wgs(geometry["coordinates"])}


def prepare(source: Path, scratch: Path) -> Path:
    """A cnmaps-data checkout (or a snapshot of its land outlines) →
    ``<scratch>/cnmaps/cn_<level>.geojson``, WGS84 features whose one
    property ``adcode`` is the file's code."""
    land = source / LAND
    if not land.is_dir():
        raise AlertsProductError(f"{land} is not a cnmaps-data land directory")
    layers: dict[str, list[dict[str, Any]]] = {"cn_province": [], "cn_prefecture": [], "cn_county": []}
    for path in sorted(land.glob("*.geojson")):
        code = path.stem
        if not re.fullmatch(r"\d{6}", code) or code == COUNTRY:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        try:
            geometry = _geometry(document)
        except AlertsProductError as exc:
            raise AlertsProductError(f"{path.name}: {exc}") from exc
        layers[layer_of(code)].append({"type": "Feature", "properties": {"adcode": code}, "geometry": geometry})
        for alias in sorted(alias for alias, released in ALIASES.items() if released == code):
            layers[layer_of(alias)].append({"type": "Feature", "properties": {"adcode": alias}, "geometry": geometry})
    directory = scratch / "cnmaps"
    directory.mkdir(parents=True, exist_ok=True)
    for name, features in layers.items():
        (directory / f"{name}.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")), encoding="utf-8"
        )
    return directory
