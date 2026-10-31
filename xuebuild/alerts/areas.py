"""``xue alerts-areas``: an upstream boundary release → one PMTiles file,
the boundary set a network's ``areas`` codes resolve against
(``docs/alerts.md`` §9).

Not part of the five-minute build. A set changes when its agency
publishes a new release, a few times a year; this runs by hand on the
downloaded release, and the file goes up once under a name that carries
the release's date (``make upload-r2-alerts-areas``). The network table
(:mod:`.networks`) then pins the new file's length and CRC32, so a round
never names a set that is not on the bucket.

The work is two subprocesses, ``ogr2ogr`` (reproject to EPSG:4326,
simplify, keep only the code attribute) and ``tippecanoe`` (tile to
PMTiles), with a pure-Python pass between them that turns each layer's
attributes into the one feature property ``id`` and holds the layers a
continental view does not need (counties, fire zones) back to their
minimum zoom.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import AlertsProductError, areas_filename, crc32_hex

LOG = logging.getLogger(__name__)

PRE_SIMPLIFY_DEGREES = 0.0005
"""``ogr2ogr -simplify`` before tiling: under a fifth of a pixel at the
sets' highest zoom (z10 ≈ 0.0014° a pixel at 256 px tiles), so the tiles
lose nothing a map shows while tippecanoe reads a tenth of the vertices."""

COORDINATE_PRECISION = 5


@dataclass(frozen=True)
class LayerSpec:
    name: str
    """The layer name in the PMTiles file, as the index's ``areas.layers``
    lists it."""
    pattern: re.Pattern[str]
    """The upstream file stem (``z_16ap26``, ``mz16ap26`` …), matched
    against ``.shp`` files under the source directory and ``.zip`` files
    beside them."""
    fields: tuple[str, ...]
    code: Callable[[dict[str, Any]], str | None]
    """The upstream attributes → the code a round's ``areas`` lists."""
    minzoom: int = 0


@dataclass(frozen=True)
class AreaSet:
    id: str
    layers: tuple[LayerSpec, ...]
    maxzoom: int
    source_srs: str | None
    """Forced source CRS for releases that ship without a ``.prj``."""
    tippecanoe: tuple[str, ...]
    """The low-zoom thinning, tuned to the view budgets in
    ``docs/alerts.md`` §9."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _zone_code(properties: dict[str, Any]) -> str | None:
    state, zone = _text(properties.get("STATE")), _text(properties.get("ZONE"))
    return f"{state}Z{zone}" if len(state) == 2 and len(zone) == 3 else None


def _county_code(properties: dict[str, Any]) -> str | None:
    """UGC county codes are the state's postal code, ``C`` and the last
    three digits of the five-digit FIPS code."""
    state, fips = _text(properties.get("STATE")), _text(properties.get("FIPS"))
    return f"{state}C{fips[2:]}" if len(state) == 2 and len(fips) == 5 else None


def _marine_code(properties: dict[str, Any]) -> str | None:
    code = _text(properties.get("ID"))
    return code or None


def _jma_code(properties: dict[str, Any]) -> str | None:
    code = _text(properties.get("regioncode"))
    return code if re.fullmatch(r"\d{7}", code) else None


SETS: dict[str, AreaSet] = {
    "nws-zones": AreaSet(
        id="nws-zones",
        layers=(
            LayerSpec("zone", re.compile(r"^z_\d{2}[a-z]{2}\d{2}$"), ("STATE", "ZONE"), _zone_code),
            LayerSpec("county", re.compile(r"^c_\d{2}[a-z]{2}\d{2}$"), ("STATE", "FIPS"), _county_code, minzoom=6),
            LayerSpec("marine", re.compile(r"^mz\d{2}[a-z]{2}\d{2}$"), ("ID",), _marine_code),
            LayerSpec("offshore", re.compile(r"^oz\d{2}[a-z]{2}\d{2}$"), ("ID",), _marine_code),
            LayerSpec("fire", re.compile(r"^fz\d{2}[a-z]{2}\d{2}$"), ("STATE", "ZONE"), _zone_code, minzoom=6),
        ),
        maxzoom=9,
        source_srs=None,
        tippecanoe=("--simplification=10", "--low-detail=11", "--detect-shared-borders"),
    ),
    "jma-class20": AreaSet(
        id="jma-class20",
        layers=(LayerSpec("class20", re.compile(r".*"), ("regioncode",), _jma_code),),
        maxzoom=10,
        source_srs="EPSG:6668",
        tippecanoe=("--simplification=10", "--low-detail=11", "--detect-shared-borders"),
    ),
}


def _run(command: list[str], cwd: Path | None = None) -> None:
    LOG.info("%s", " ".join(command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, cwd=cwd)
    except FileNotFoundError as exc:
        raise AlertsProductError(f"{command[0]} is not on PATH; the boundary tool needs GDAL and tippecanoe") from exc
    if completed.returncode != 0:
        raise AlertsProductError(f"{command[0]} failed ({completed.returncode}): {completed.stderr.strip()[-2000:]}")


def find_datasource(source: Path, layer: LayerSpec, area_set: AreaSet) -> str:
    """The one shapefile under ``source`` the layer reads, or the one zip
    holding it (read through ``/vsizip/``). Two candidates is an error:
    the agencies post the current and the next release side by side."""
    shapefiles = [path for path in sorted(source.rglob("*.shp")) if layer.pattern.match(path.stem)]
    if not shapefiles:
        archives = [path for path in sorted(source.rglob("*.zip")) if layer.pattern.match(path.stem)]
        if len(archives) == 1:
            return f"/vsizip/{archives[0]}"
        shapefiles = []
        if len(archives) > 1:
            raise AlertsProductError(f"{area_set.id} {layer.name}: several releases under {source}: {[path.name for path in archives]}")
    if len(shapefiles) != 1:
        found = [path.name for path in shapefiles]
        raise AlertsProductError(f"{area_set.id} {layer.name}: expected one shapefile under {source}, found {found or 'none'}")
    return str(shapefiles[0])


def _features(path: Path, layer: LayerSpec) -> Iterator[tuple[str, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            geometry = feature.get("geometry")
            if not geometry:
                continue
            code = layer.code(feature.get("properties") or {})
            if code is None:
                continue
            yield code, geometry


def write_layer(datasource: str, layer: LayerSpec, area_set: AreaSet, scratch: Path) -> tuple[Path, int, int]:
    """One layer → a GeoJSONSeq file of ``{"id": code}`` features ready
    for tippecanoe; returns it, its feature count and its distinct codes."""
    raw = scratch / f"{layer.name}.ogr.geojsonl"
    command = ["ogr2ogr", "-f", "GeoJSONSeq", "-t_srs", "EPSG:4326", "-simplify", str(PRE_SIMPLIFY_DEGREES)]
    if area_set.source_srs:
        command += ["-s_srs", area_set.source_srs]
    command += ["-select", ",".join(layer.fields), "-lco", f"COORDINATE_PRECISION={COORDINATE_PRECISION}", str(raw), datasource]
    _run(command)
    ready = scratch / f"{layer.name}.geojsonl"
    count = 0
    codes: set[str] = set()
    with ready.open("w", encoding="utf-8") as handle:
        for code, geometry in _features(raw, layer):
            feature: dict[str, Any] = {"type": "Feature", "properties": {"id": code}, "geometry": geometry}
            if layer.minzoom:
                feature["tippecanoe"] = {"minzoom": layer.minzoom}
            handle.write(json.dumps(feature, separators=(",", ":")) + "\n")
            count += 1
            codes.add(code)
    raw.unlink()
    return ready, count, len(codes)


def build_area_set(set_id: str, source: Path, version: str, output: Path) -> dict[str, Any]:
    """Build ``<output>/<set>.<version>.pmtiles`` from the release
    under ``source`` and report what the network table pins."""
    area_set = SETS.get(set_id)
    if area_set is None:
        raise AlertsProductError(f"unknown boundary set {set_id!r}; choose from {', '.join(SETS)}")
    if not re.fullmatch(r"\d{8}", version):
        raise AlertsProductError("version must be the release's date, YYYYMMDD")
    if not source.is_dir():
        raise AlertsProductError(f"{source} is not a directory of the downloaded release")
    if shutil.which("tippecanoe") is None:
        raise AlertsProductError("tippecanoe is not on PATH")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / areas_filename(set_id, version)
    layers: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"alerts-areas-{set_id}-") as scratch_name:
        scratch = Path(scratch_name)
        inputs: list[str] = []
        for layer in area_set.layers:
            datasource = find_datasource(source, layer, area_set)
            path, count, codes = write_layer(datasource, layer, area_set, scratch)
            layers.append({"layer": layer.name, "source": datasource, "features": count, "codes": codes, "minzoom": layer.minzoom})
            inputs.append(f"--named-layer={layer.name}:{path.name}")
        # tippecanoe records its command line in the file's metadata: run
        # it inside the scratch directory on relative names, so the bytes —
        # and the CRC32 the network table pins — do not depend on where
        # the temporary directory happened to be.
        tiles = areas_filename(set_id, version)
        _run(
            [
                "tippecanoe",
                "--output",
                tiles,
                "--force",
                "--quiet",
                "--minimum-zoom=0",
                f"--maximum-zoom={area_set.maxzoom}",
                "--no-tile-size-limit",
                "--no-feature-limit",
                "--no-tiny-polygon-reduction",
                f"--name={set_id}.{version}",
                *area_set.tippecanoe,
                *inputs,
            ],
            cwd=scratch,
        )
        shutil.move(scratch / tiles, destination)
    payload = destination.read_bytes()
    return {
        "set": set_id,
        "version": version,
        "path": str(destination),
        "byteLength": len(payload),
        "crc32": crc32_hex(payload),
        "layers": layers,
    }
