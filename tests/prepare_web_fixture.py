"""Build a small synthetic 121-frame Xue bundle for browser e2e tests.

The grid is a coarse global 144x73 so the bundle stays tiny while exercising
the full production frame count, temporal grouping, and manifest contract.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xue import binformat, temporal, zstdcli
from xue.binconvert import (
    GridInfo,
    WIND_COMPONENT_IDS,
    _decimate_codes,
    _playback_bandwidth,
    _variable_payloads,
    _wind_bundle_payloads,
    build_metadata,
    encode_poster,
)
from xue.manifest import build_bin_manifest, build_latest_pointer, iso_z
from xue.quantize import QUALITY_FLUX, QUALITY_PRECIPITATION, QUALITY_TEMPERATURE, QUALITY_WIND
from xue.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WEB_FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "generated" / "web"
RUN_TIME = datetime(2026, 8, 15, 6, tzinfo=UTC)
WIDTH, HEIGHT = 144, 73
HOURS = list(range(121))
# The ECMWF fixture models the full IFS open data series: 3-hourly to 144
# hours, 6-hourly to 240 — a mixed-step axis, so its bundles list their hours
# outright and exercise the explicit-hours path end to end.
ECMWF_RUN_TIME = datetime(2026, 8, 15, 0, tzinfo=UTC)
ECMWF_HOURS = list(range(0, 145, 3)) + list(range(150, 241, 6))
# The sflux fixture models the GFS surface flux series: hourly, with the
# de-averaged prate starting at the first real step and the dswrf layer.
SFLUX_RUN_TIME = datetime(2026, 8, 15, 6, tzinfo=UTC)
# The showcase fixture models one historical case: a regional grid over the
# western Pacific, a short axis, and only the variables the event is about.
SHOWCASE_RUN_TIME = datetime(2024, 9, 3, 0, tzinfo=UTC)
SHOWCASE_HOURS = list(range(25))
SHOWCASE_GRID = GridInfo(
    width=40, height=30, first_longitude=110.0, first_latitude=35.0, longitude_step=0.5, latitude_step=-0.5
)


def _temperature_plane(hour: int) -> np.ndarray:
    longitude = np.linspace(-180, 177.5, WIDTH)
    latitude = np.linspace(90, -90, HEIGHT)
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    values = 30 * np.cos(np.radians(lat_grid)) - 10 + 8 * np.sin(np.radians(lon_grid + hour * 3))
    return QUALITY_TEMPERATURE.quantize(values.ravel())


def _precipitation_plane(hour: int) -> np.ndarray:
    longitude = np.linspace(-180, 177.5, WIDTH)
    latitude = np.linspace(90, -90, HEIGHT)
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    band = np.exp(-((lat_grid - 10 * np.sin(np.radians(lon_grid * 2 + hour * 5))) ** 2) / 60)
    values = np.where(band > 0.4, band * 12.0, 0.0)
    return QUALITY_PRECIPITATION.quantize(values.ravel())


def _solar_plane(hour: int) -> np.ndarray:
    """Day hemisphere following the sun westward at 15 degrees per hour."""
    longitude = np.linspace(-180, 177.5, WIDTH)
    latitude = np.linspace(90, -90, HEIGHT)
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    values = 1100 * np.cos(np.radians(lat_grid)) * np.cos(np.radians(lon_grid + hour * 15))
    return QUALITY_FLUX.quantize(np.maximum(values, 0.0).ravel())


def _wind_plane(hour: int, component: str) -> np.ndarray:
    longitude = np.linspace(-180, 177.5, WIDTH)
    latitude = np.linspace(90, -90, HEIGHT)
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    if component == "ugrd10m":
        values = 10 * np.cos(np.radians(lat_grid)) + 8 * np.sin(np.radians(lon_grid * 2 + hour * 4))
    else:
        values = 6 * np.sin(np.radians(lon_grid * 3 - hour * 3)) * np.cos(np.radians(lat_grid))
    return QUALITY_WIND.quantize(values.ravel())


def _entry(variable_id: int, predictor: int, hour: int, dependency: int, group: int, plane: np.ndarray, payload: bytes) -> binformat.PlaneEntry:
    return binformat.PlaneEntry(
        variable_id=variable_id,
        predictor=predictor,
        compression=binformat.COMPRESSION_ZSTD,
        flags=binformat.FLAG_ZSTD_CHECKSUM,
        frame_offset=hour,
        dependency_offset=dependency,
        group_id=group,
        compressed_length=len(payload),
        data_offset=0,
        decoded_length=plane.size,
        crc32=binformat.crc32_plane(plane),
        minimum_code=int(plane.min()),
        maximum_code=int(plane.max()),
    )


def prepare_web_fixture() -> Path:
    WEB_FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    grid = GridInfo(
        width=WIDTH,
        height=HEIGHT,
        first_longitude=-180.0,
        first_latitude=90.0,
        longitude_step=2.5,
        latitude_step=-2.5,
    )

    level = 3  # keep fixture generation fast; the contract is level-independent
    temperature_planes: list[binformat.PlanePayload] = []
    for group_id, group in enumerate(temporal.group_forecast_hours(HOURS)):
        anchor = temporal.anchor_hour(group)
        group_planes = {hour: _temperature_plane(hour) for hour in group}
        for hour in [anchor, *[hour for hour in group if hour != anchor]]:
            if hour == anchor:
                payload = zstdcli.compress(group_planes[hour].tobytes(), level=level)
                temperature_planes.append(
                    binformat.PlanePayload(
                        _entry(1, binformat.PREDICTOR_RAW, hour, binformat.NO_DEPENDENCY, group_id, group_planes[hour], payload),
                        payload,
                    )
                )
            else:
                residual = temporal.encode_residual(group_planes[hour], group_planes[anchor])
                payload = zstdcli.compress(residual.tobytes(), level=level)
                temperature_planes.append(
                    binformat.PlanePayload(
                        _entry(1, binformat.PREDICTOR_ANCHOR, hour, anchor, group_id, group_planes[hour], payload),
                        payload,
                    )
                )
    precipitation_planes: list[binformat.PlanePayload] = []
    for hour in HOURS:
        plane = _precipitation_plane(hour)
        payload = zstdcli.compress(plane.tobytes(), level=level)
        precipitation_planes.append(
            binformat.PlanePayload(
                _entry(2, binformat.PREDICTOR_RAW, hour, binformat.NO_DEPENDENCY, hour, plane, payload),
                payload,
            )
        )

    # Layout mirrors production: the manifest lives
    # inside the run directory (bundle paths are manifest-relative) and the
    # mutable latest.json pointer at the data root names it.
    bundles = []
    first_planes = {"tmp2m": _temperature_plane(HOURS[0]), "prate": _precipitation_plane(HOURS[0])}
    plane_builders = {"tmp2m": _temperature_plane, "prate": _precipitation_plane}
    half_grid = grid.decimated()
    for variable_id, planes in (("tmp2m", temperature_planes), ("prate", precipitation_planes)):
        metadata = build_metadata(RUN_TIME, HOURS, grid, "quality", (variable_id,))
        bundle_path = WEB_FIXTURE_ROOT / f"{variable_id}.xue"
        binformat.write_bundle(bundle_path, metadata, planes)
        bundle = binformat.read_bundle(bundle_path)
        bundle.verify_all()
        data = bundle_path.read_bytes()
        # Half-resolution variant, decimated from the same synthetic codes.
        half_planes = {hour: _decimate_codes(plane_builders[variable_id](hour), grid) for hour in HOURS}
        half_payloads = [
            binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, raw in _variable_payloads(variable_id, HOURS, half_planes)
            for compressed in (zstdcli.compress(raw, level=level),)
        ]
        half_path = WEB_FIXTURE_ROOT / f"{variable_id}.half.xue"
        binformat.write_bundle(
            half_path, build_metadata(RUN_TIME, HOURS, half_grid, "quality", (variable_id,)), half_payloads
        )
        binformat.read_bundle(half_path).verify_all()
        half_data = half_path.read_bytes()
        poster_payload, poster_grid = encode_poster(first_planes[variable_id], grid)
        poster_path = WEB_FIXTURE_ROOT / f"{variable_id}.poster.bin"
        poster_path.write_bytes(poster_payload)
        bundles.append(
            {
                "variable": variable_id,
                "path": f"{variable_id}.xue",
                "byteLength": len(data),
                "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
                "variants": [
                    {
                        "path": f"{variable_id}.half.xue",
                        "width": half_grid.width,
                        "height": half_grid.height,
                        "byteLength": len(half_data),
                        "crc32": f"{zlib.crc32(half_data) & 0xFFFFFFFF:08x}",
                        "bandwidth": _playback_bandwidth(len(half_data), len(HOURS)),
                    }
                ],
                "poster": {
                    "path": f"{variable_id}.poster.bin",
                    "width": poster_grid.width,
                    "height": poster_grid.height,
                    "byteLength": len(poster_payload),
                    "crc32": f"{zlib.crc32(poster_payload) & 0xFFFFFFFF:08x}",
                    "metadataJson": json.dumps(build_metadata(RUN_TIME, HOURS, poster_grid, "quality", (variable_id,))),
                },
            }
        )

    # The optional two-variable wind bundle (u/v pair on one time axis),
    # with its own half-resolution variant and no poster or video artifacts.
    level3 = 3
    wind_planes_by_hour = {
        hour: {component: _wind_plane(hour, component) for component in WIND_COMPONENT_IDS} for hour in HOURS
    }
    wind_payloads = [
        binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
        for entry, raw in _wind_bundle_payloads(HOURS, wind_planes_by_hour)
        for compressed in (zstdcli.compress(raw, level=level3),)
    ]
    wind_path = WEB_FIXTURE_ROOT / "wind10m.xue"
    binformat.write_bundle(wind_path, build_metadata(RUN_TIME, HOURS, grid, "quality", WIND_COMPONENT_IDS), wind_payloads)
    binformat.read_bundle(wind_path).verify_all()
    wind_data = wind_path.read_bytes()
    wind_half_planes = {
        hour: {component: _decimate_codes(planes[component], grid) for component in WIND_COMPONENT_IDS}
        for hour, planes in wind_planes_by_hour.items()
    }
    wind_half_payloads = [
        binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
        for entry, raw in _wind_bundle_payloads(HOURS, wind_half_planes)
        for compressed in (zstdcli.compress(raw, level=level3),)
    ]
    wind_half_path = WEB_FIXTURE_ROOT / "wind10m.half.xue"
    binformat.write_bundle(
        wind_half_path, build_metadata(RUN_TIME, HOURS, half_grid, "quality", WIND_COMPONENT_IDS), wind_half_payloads
    )
    binformat.read_bundle(wind_half_path).verify_all()
    wind_half_data = wind_half_path.read_bytes()
    bundles.append(
        {
            "variable": "wind10m",
            "path": "wind10m.xue",
            "byteLength": len(wind_data),
            "crc32": f"{zlib.crc32(wind_data) & 0xFFFFFFFF:08x}",
            "variants": [
                {
                    "path": "wind10m.half.xue",
                    "width": half_grid.width,
                    "height": half_grid.height,
                    "byteLength": len(wind_half_data),
                    "crc32": f"{zlib.crc32(wind_half_data) & 0xFFFFFFFF:08x}",
                    "bandwidth": _playback_bandwidth(len(wind_half_data), len(HOURS)),
                }
            ],
        }
    )

    manifest = build_bin_manifest(RUN_TIME, bundles=bundles)
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (WEB_FIXTURE_ROOT / "manifest.json").write_bytes(manifest_bytes)
    pointer = build_latest_pointer(
        RUN_TIME.strftime("%Y%m%d%H"),
        RUN_TIME,
        manifest_path="gfs.2026081506/manifest.json",
        manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
    )
    (WEB_FIXTURE_ROOT / "latest.json").write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")

    _prepare_ecmwf_fixture(grid, level)
    _prepare_sflux_fixture(grid, level)
    _prepare_showcase_fixture(level)
    return WEB_FIXTURE_ROOT


def _prepare_ecmwf_fixture(grid: GridInfo, level: int) -> None:
    """A second, independent model dataset: ECMWF identity, mixed-step
    240-hour time axis (an explicitly listed ``hours`` axis), scalar bundles only
    (wind, posters, and variants are optional per the manifest contract, so
    the smallest valid dataset skips them)."""
    ecmwf_root = WEB_FIXTURE_ROOT / "ecmwf"
    ecmwf_root.mkdir(parents=True, exist_ok=True)
    bundles = []
    # Like production: the de-accumulated prate has no analysis frame, so its
    # series starts at the first real step and carries its own shorter axis.
    for variable_id, builder, bundle_hours in (
        ("tmp2m", _temperature_plane, ECMWF_HOURS),
        ("prate", _precipitation_plane, ECMWF_HOURS[1:]),
    ):
        planes_by_hour = {hour: builder(hour) for hour in bundle_hours}
        payloads = [
            binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, raw in _variable_payloads(variable_id, bundle_hours, planes_by_hour)
            for compressed in (zstdcli.compress(raw, level=level),)
        ]
        metadata = build_metadata(
            ECMWF_RUN_TIME, bundle_hours, grid, "quality", (variable_id,), source=source_spec("ecmwf")
        )
        bundle_path = ecmwf_root / f"{variable_id}.xue"
        binformat.write_bundle(bundle_path, metadata, payloads)
        binformat.read_bundle(bundle_path).verify_all()
        data = bundle_path.read_bytes()
        bundles.append(
            {
                "variable": variable_id,
                "path": f"{variable_id}.xue",
                "byteLength": len(data),
                "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
            }
        )
    manifest = build_bin_manifest(
        ECMWF_RUN_TIME, bundles=bundles, expected_hours=ECMWF_HOURS[-1], model="ECMWF", product="ifs-0p25"
    )
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (ecmwf_root / "manifest.json").write_bytes(manifest_bytes)
    pointer = build_latest_pointer(
        ECMWF_RUN_TIME.strftime("%Y%m%d%H"),
        ECMWF_RUN_TIME,
        manifest_path="ecmwf.2026081500/manifest.json",
        manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
        model="ECMWF",
        product="ifs-0p25",
    )
    (WEB_FIXTURE_ROOT / "latest-ecmwf.json").write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")


def _prepare_sflux_fixture(grid: GridInfo, level: int) -> None:
    """A third, independent model dataset: GFS-SFLUX
    identity, hourly axis, the optional dswrf bundle, and — like production —
    a de-averaged prate that has no analysis frame."""
    sflux_root = WEB_FIXTURE_ROOT / "sflux"
    sflux_root.mkdir(parents=True, exist_ok=True)
    bundles = []
    for variable_id, builder, bundle_hours in (
        ("tmp2m", _temperature_plane, HOURS),
        ("prate", _precipitation_plane, HOURS[1:]),
        ("dswrf", _solar_plane, HOURS),
    ):
        planes_by_hour = {hour: builder(hour) for hour in bundle_hours}
        payloads = [
            binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, raw in _variable_payloads(variable_id, bundle_hours, planes_by_hour)
            for compressed in (zstdcli.compress(raw, level=level),)
        ]
        metadata = build_metadata(
            SFLUX_RUN_TIME, bundle_hours, grid, "quality", (variable_id,), source=source_spec("sflux")
        )
        bundle_path = sflux_root / f"{variable_id}.xue"
        binformat.write_bundle(bundle_path, metadata, payloads)
        binformat.read_bundle(bundle_path).verify_all()
        data = bundle_path.read_bytes()
        bundles.append(
            {
                "variable": variable_id,
                "path": f"{variable_id}.xue",
                "byteLength": len(data),
                "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
            }
        )
    manifest = build_bin_manifest(SFLUX_RUN_TIME, bundles=bundles, model="GFS-SFLUX", product="sfluxgrb")
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (sflux_root / "manifest.json").write_bytes(manifest_bytes)
    pointer = build_latest_pointer(
        SFLUX_RUN_TIME.strftime("%Y%m%d%H"),
        SFLUX_RUN_TIME,
        manifest_path="sflux.2026081506/manifest.json",
        manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
        model="GFS-SFLUX",
        product="sfluxgrb",
    )
    (WEB_FIXTURE_ROOT / "latest-sflux.json").write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")


def _showcase_temperature_plane(hour: int) -> np.ndarray:
    """A warm ridge strengthening over the window's north-east corner."""
    longitude = SHOWCASE_GRID.first_longitude + np.arange(SHOWCASE_GRID.width) * SHOWCASE_GRID.longitude_step
    latitude = SHOWCASE_GRID.first_latitude + np.arange(SHOWCASE_GRID.height) * SHOWCASE_GRID.latitude_step
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    values = 26.0 + 0.35 * (lat_grid - 20.0) + 0.2 * hour * np.cos(np.radians(lon_grid - 120.0))
    return QUALITY_TEMPERATURE.quantize(values.ravel())


def _showcase_precipitation_plane(hour: int) -> np.ndarray:
    """A cyclone-shaped rain field drifting north-west across the window."""
    longitude = SHOWCASE_GRID.first_longitude + np.arange(SHOWCASE_GRID.width) * SHOWCASE_GRID.longitude_step
    latitude = SHOWCASE_GRID.first_latitude + np.arange(SHOWCASE_GRID.height) * SHOWCASE_GRID.latitude_step
    lon_grid, lat_grid = np.meshgrid(longitude, latitude)
    center = (126.0 - hour * 0.35, 22.0 + hour * 0.18)
    radius = np.hypot(lon_grid - center[0], lat_grid - center[1])
    values = np.where(radius < 6.0, 24.0 * np.exp(-(radius**2) / 4.0), 0.0)
    return QUALITY_PRECIPITATION.quantize(values.ravel())


def _prepare_showcase_fixture(level: int) -> None:
    """One historical showcase case: a regional (non-wrapping) grid, a single
    variable, and the mutable showcase.json catalog that names it. This is the
    only fixture whose grid does not cover the world, so it is what exercises
    the renderers' out-of-grid clipping."""
    case_root = WEB_FIXTURE_ROOT / "showcase" / "demo-typhoon"
    case_root.mkdir(parents=True, exist_ok=True)
    bundles = []
    for variable_id, builder in (("tmp2m", _showcase_temperature_plane), ("prate", _showcase_precipitation_plane)):
        planes_by_hour = {hour: builder(hour) for hour in SHOWCASE_HOURS}
        payloads = [
            binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, raw in _variable_payloads(variable_id, SHOWCASE_HOURS, planes_by_hour)
            for compressed in (zstdcli.compress(raw, level=level),)
        ]
        metadata = build_metadata(
            SHOWCASE_RUN_TIME, SHOWCASE_HOURS, SHOWCASE_GRID, "quality", (variable_id,)
        )
        bundle_path = case_root / f"{variable_id}.xue"
        binformat.write_bundle(bundle_path, metadata, payloads)
        binformat.read_bundle(bundle_path).verify_all()
        data = bundle_path.read_bytes()

        poster_payload, poster_grid = encode_poster(planes_by_hour[SHOWCASE_HOURS[0]], SHOWCASE_GRID)
        (case_root / f"{variable_id}.poster.bin").write_bytes(poster_payload)
        bundles.append(
            {
                "variable": variable_id,
                "path": f"{variable_id}.xue",
                "byteLength": len(data),
                "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
                "poster": {
                    "path": f"{variable_id}.poster.bin",
                    "width": poster_grid.width,
                    "height": poster_grid.height,
                    "byteLength": len(poster_payload),
                    "crc32": f"{zlib.crc32(poster_payload) & 0xFFFFFFFF:08x}",
                    "metadataJson": json.dumps(
                        build_metadata(
                            SHOWCASE_RUN_TIME, SHOWCASE_HOURS, poster_grid, "quality", (variable_id,)
                        )
                    ),
                },
            }
        )
    data_length = sum(bundle["byteLength"] for bundle in bundles)

    manifest = build_bin_manifest(
        SHOWCASE_RUN_TIME,
        bundles=bundles,
        expected_hours=SHOWCASE_HOURS[-1],
        require_core_variables=False,
    )
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (case_root / "manifest.json").write_bytes(manifest_bytes)

    east = SHOWCASE_GRID.first_longitude + (SHOWCASE_GRID.width - 1) * SHOWCASE_GRID.longitude_step
    south = SHOWCASE_GRID.first_latitude + (SHOWCASE_GRID.height - 1) * SHOWCASE_GRID.latitude_step
    catalog = {
        "schemaVersion": 1,
        "generatedAt": "2026-08-30T00:00:00Z",
        "cases": [
            {
                "id": "demo-typhoon",
                "title": {"zh": "演示台风个例", "en": "Demo typhoon case"},
                "summary": {"zh": "端到端测试用的合成个例。", "en": "A synthetic case for end-to-end tests."},
                "modelId": "gfs",
                "model": "GFS",
                "product": "pgrb2.0p25",
                "run": SHOWCASE_RUN_TIME.strftime("%Y%m%d%H"),
                "runTime": iso_z(SHOWCASE_RUN_TIME),
                "forecastHours": SHOWCASE_HOURS[-1],
                "bbox": [SHOWCASE_GRID.first_longitude, south, east, SHOWCASE_GRID.first_latitude],
                "dataBbox": [SHOWCASE_GRID.first_longitude, south, east, SHOWCASE_GRID.first_latitude],
                "grid": {"width": SHOWCASE_GRID.width, "height": SHOWCASE_GRID.height},
                "variables": ["tmp2m", "prate"],
                "defaultVariable": "tmp2m",
                "manifestPath": "showcase/demo-typhoon/manifest.json",
                "manifestCrc32": f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
                "byteLength": data_length,
                "eventTime": "2024-09-06T00:00:00Z",
                "tags": ["typhoon"],
            }
        ],
    }
    (WEB_FIXTURE_ROOT / "showcase.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    print(prepare_web_fixture())
