"""Regenerate the agriculture indicators fixtures and golden.

``tests/fixtures/indicators/`` holds:

- ``weights-mini.zarr``: a weights store in the production layout
  (``docs/indicators.md`` §7), written with zarr-python, on the fixture's
  coarse grid, with one region, ``us-ia``, whose box straddles both a
  data tile corner and an inner chunk corner;
- ``store/``: a small data root as the bucket lays one out — ``latest.json``
  naming ``gfs.2026081506/manifest.json``, whose ``tmp2m`` and ``prate``
  bundles are Zarr stores. The planes are synthetic (a diurnal temperature
  cycle with a south-to-north gradient, a rain band on the second local
  day) on a regional 1° grid, encoded by the same machinery as the web
  fixture (``tests/prepare_web_fixture.py``), so the store is what a real
  run's store is, only small;
- ``expected/``: what ``indicators-build`` writes from that store — the
  month file with its one line, the season file and the index — byte for
  byte.

Run it after a deliberate change to the features, the schema or the
fixture, and commit the diff with the change::

    .venv/bin/python tests/prepare_indicators_golden.py

It also provides :func:`serve`, the Range-capable local HTTP server the
tests read the store through.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import re
import shutil
import tempfile
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import zarrstore
from xuebuild.binconvert import GridInfo, build_metadata
from xuebuild.common import crc32_hex
from xuebuild.manifest import build_bin_manifest, build_latest_pointer
from xuebuild.quantize import PROFILES

try:
    from tests.prepare_web_fixture import write_v2_bundle
except ModuleNotFoundError:  # run as a script: tests/ is the path root
    from prepare_web_fixture import write_v2_bundle

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "indicators"
STORE_ROOT = FIXTURES / "store"
WEIGHTS = FIXTURES / "weights-mini.zarr"
EXPECTED = FIXTURES / "expected"

RUN_TIME = datetime(2026, 8, 15, 6, tzinfo=UTC)
RUN = f"{RUN_TIME:%Y%m%d%H}"
HOURS = list(range(61))
PROFILE = "balanced"
GRID = GridInfo(width=30, height=20, first_longitude=-110.0, first_latitude=50.0, longitude_step=1.0, latitude_step=-1.0)
TILE = (8, 8)
REGION = "us-ia"
REGION_BOUNDS = (6, 11, 13, 21)
"""Rows 6..10 (44 °N to 40 °N), columns 13..20 (97 °W to 90 °W): across
the tile boundary at row 8 and column 16."""
WEIGHTS_VERSION = "fixture-1"
GRID_ID = "mini"
SOURCE_GRIDS = {"gfs": GRID_ID}


def _coordinates() -> tuple[np.ndarray, np.ndarray]:
    longitude = GRID.first_longitude + np.arange(GRID.width) * GRID.longitude_step
    latitude = GRID.first_latitude + np.arange(GRID.height) * GRID.latitude_step
    return longitude, latitude


def temperature(hour: int) -> np.ndarray:
    """°C: a diurnal cycle peaking at 15:00 local (UTC−6), warmer to the
    south and a little warmer each day, so the day's maximum crosses 30 °C
    and 35 °C over part of the region."""
    longitude, latitude = _coordinates()
    lon, lat = np.meshgrid(longitude, latitude)
    local = (RUN_TIME.hour - 6 + hour) % 24
    day = (RUN_TIME.hour - 6 + hour) // 24
    return 25.0 + 9.0 * np.sin(2 * np.pi * (local - 9) / 24) - 0.8 * (lat - 42.0) + 0.05 * (lon + 95.0) + 1.5 * day


def precipitation(hour: int) -> np.ndarray:
    """mm/h: dry, but for a band over the region's centre in the second
    local day's afternoon."""
    longitude, latitude = _coordinates()
    lon, _lat = np.meshgrid(longitude, latitude)
    local = (RUN_TIME.hour - 6 + hour) % 24
    day = (RUN_TIME.hour - 6 + hour) // 24
    if day == 1 and 12 < local <= 18:
        return 2.0 * np.exp(-((lon + 94.0) ** 2) / 4.0)
    return np.zeros_like(lon)


PLANES = {"tmp2m": temperature, "prate": precipitation}


def region_weights() -> np.ndarray:
    """The region's weights on the whole grid, float32, zero outside its
    box and at one cell inside it (a cell outside the state's polygon)."""
    row0, row1, col0, col1 = REGION_BOUNDS
    rows, cols = np.meshgrid(np.arange(row1 - row0), np.arange(col1 - col0), indexing="ij")
    raw = 1.0 + rows + 0.5 * cols
    raw[0, 0] = 0.0
    full = np.zeros((GRID.height, GRID.width), dtype=np.float64)
    full[row0:row1, col0:col1] = raw / raw.sum()
    return full.astype(np.float32)


def write_weights(path: Path = WEIGHTS, *, weights: np.ndarray | None = None, attributes: dict | None = None) -> None:
    """A weights store in the production layout (docs/indicators.md §7),
    written by zarr-python as the weights script writes it, with an inner
    chunk of 8 x 8 instead of 180 x 180 so the region's box spans several
    chunks of its shard on this small grid."""
    import zarr  # noqa: PLC0415 — the fixture writer only; the reader is NumPy
    from zarr.codecs import BytesCodec, ZstdCodec  # noqa: PLC0415

    longitude, latitude = _coordinates()
    row0, row1, col0, col1 = REGION_BOUNDS
    if path.exists():
        shutil.rmtree(path)
    group = zarr.open_group(str(path), mode="w", zarr_format=3)
    group.attrs.update(
        {
            "version": WEIGHTS_VERSION,
            "spam": "SPAM 2020 v2r2 SOYB_A",
            "grid": GRID_ID,
            "regions": {REGION: {"rows": [row0, row1], "cols": [col0, col1]}},
            "lon_convention": "cell centres from -110, ascending",
            **(attributes or {}),
        }
    )
    tile = 8
    array = group.create_array(
        "weights",
        shape=(1, GRID.height, GRID.width),
        dtype="float32",
        chunks=(1, tile, tile),
        shards=(1, -(-GRID.height // tile) * tile, -(-GRID.width // tile) * tile),
        serializer=BytesCodec(endian="little"),
        compressors=ZstdCodec(level=9, checksum=False),
        fill_value=0.0,
        dimension_names=["region", "lat", "lon"],
    )
    array[0] = region_weights() if weights is None else weights
    coordinate = {"compressors": ZstdCodec(level=9, checksum=False)}
    group.create_array("region", data=np.array([REGION], dtype="<U6"), dimension_names=["region"], **coordinate)
    group.create_array("lat", data=latitude.astype(np.float64), dimension_names=["lat"], **coordinate)
    group.create_array("lon", data=longitude.astype(np.float64), dimension_names=["lon"], **coordinate)


def write_store(root: Path = STORE_ROOT) -> None:
    """The data root: pointer, manifest and the two stores, no containers."""
    if root.exists():
        shutil.rmtree(root)
    run_dir = root / f"gfs.{RUN}"
    run_dir.mkdir(parents=True)
    bundles = []
    for variable_id, builder in PLANES.items():
        codebook = PROFILES[PROFILE][variable_id]
        bundle_path = run_dir / f"{variable_id}.xue"
        write_v2_bundle(
            bundle_path,
            build_metadata(RUN_TIME, HOURS, GRID, PROFILE, (variable_id,)),
            GRID,
            (variable_id,),
            HOURS,
            {hour: {variable_id: codebook.quantize(builder(hour).ravel())} for hour in HOURS},
            level=3,
            tile=TILE,
        )
        report = zarrstore.export_bundle(bundle_path, zarrstore.store_path_for(bundle_path))
        bundle_path.unlink()
        bundles.append({"variable": variable_id, "zarr": report.descriptor(run_dir)})
    manifest = build_bin_manifest(RUN_TIME, bundles=bundles, expected_hours=HOURS[-1])
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    pointer = build_latest_pointer(
        RUN, RUN_TIME, manifest_path=f"gfs.{RUN}/manifest.json", manifest_crc32=crc32_hex(manifest_bytes)
    )
    (root / "latest.json").write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")


# -- a Range-capable static server ---------------------------------------------

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    root: Path
    requests: list[tuple[str, str | None]]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the base class's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — the base class's name
        path = urllib.parse.urlsplit(self.path).path.lstrip("/")
        target = (self.root / urllib.parse.unquote(path)).resolve()
        header = self.headers.get("Range")
        self.requests.append((path, header))
        if not target.is_file() or self.root.resolve() not in target.parents:
            self.send_error(404)
            return
        data = target.read_bytes()
        if header is None:
            self._send(200, data)
            return
        match = _RANGE.match(header)
        if match is None or match.groups() == ("", ""):
            self.send_error(416)
            return
        first, last = match.groups()
        if first == "":
            start, end = max(0, len(data) - int(last)), len(data) - 1
        else:
            start, end = int(first), min(int(last) if last else len(data) - 1, len(data) - 1)
        if start > end:
            self.send_error(416)
            return
        self._send(206, data[start : end + 1], f"bytes {start}-{end}/{len(data)}")

    def _send(self, status: int, body: bytes, content_range: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def serve(root: Path) -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    """Serve ``root`` on a free local port with Range support, giving the
    base URL and the list every request is logged to (path, Range)."""
    requests: list[tuple[str, str | None]] = []
    handler = type("Handler", (_RangeHandler,), {"root": root, "requests": requests})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", requests
    finally:
        server.shutdown()
        server.server_close()


def build_fixture(output_dir: Path, **options: object) -> dict:
    """``indicators-build`` over the fixture store into ``output_dir``."""
    from xuebuild.indicators.build import build  # noqa: PLC0415

    weights_dir = output_dir.parent / f"{output_dir.name}-weights"
    weights_dir.mkdir(parents=True, exist_ok=True)
    if not (weights_dir / f"{GRID_ID}.zarr").exists():
        shutil.copytree(WEIGHTS, weights_dir / f"{GRID_ID}.zarr")
    with serve(STORE_ROOT) as (base_url, _requests):
        return build(
            output_dir,
            sources=("gfs",),
            base_url=base_url,
            weights_dir=weights_dir,
            source_grids=SOURCE_GRIDS,
            **options,  # type: ignore[arg-type]
        )


def main() -> None:
    write_weights()
    write_store()
    with tempfile.TemporaryDirectory(prefix="xue-indicators-") as scratch:
        output = Path(scratch) / "indicators"
        build_fixture(output)
        if EXPECTED.exists():
            shutil.rmtree(EXPECTED)
        for path in sorted(output.rglob("*")):
            if path.is_file():
                target = EXPECTED / path.relative_to(output)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
                print(target.relative_to(FIXTURES.parent.parent.parent), path.stat().st_size)


if __name__ == "__main__":
    main()
