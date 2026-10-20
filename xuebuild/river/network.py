"""The network store (``docs/zarr-profile.md``, "Network store"): one value
per river reach per frame, the Zarr profile's third branch.

A store holds one track of one product — an analysis window, or one
forecast cycle — as a ``[time, feature]`` ``uint8`` array of log1p codes in
one shard, beside the reach ids, the frame times and each reach's
baseflow. The feature axis is the GeoParquet's row order, and an inner
chunk spans exactly one of its row groups, so one index addresses both
files.

The shard, its index and the codecs are the grid stores' own
(:mod:`xuebuild.zarrstore`); only the dimensions, the chunking and the
attributes differ. The reader here is NumPy-only, as ``zarrstore``'s is,
so the tests read back without zarr-python.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import zarrstore, zstdcli
from ..binconvert import _time_metadata, axis_unit_seconds
from ..common import crc32_hex, iso_z
from ..errors import XueError
from ..quantize import PrecipitationCodebook

NETWORK_VERSION = 1
"""The ``xue_network`` block's ``version``."""

ARRAY = "streamflow"
DIMENSIONS = ("time", "feature")
TIME_CHUNK = zarrstore.TIME_CHUNK
ROW_GROUP_ROWS = 4096
"""Reaches per GeoParquet row group, and so per inner chunk's feature span."""

CODEBOOK = PrecipitationCodebook(trace=0.01, scale=1.0, maximum=100000.0, name="streamflow")
"""log1p over 0.01–100 000 m³/s: about 4.7 % between neighbouring codes,
the same relative step for a creek as for the Mississippi."""

VARIABLE = {
    "numericId": 1,
    "id": ARRAY,
    "label": "Streamflow",
    "unit": "m3/s",
    "quantization": CODEBOOK.metadata(),
}

DELTA = True
"""Whether the writer puts ``xue.delta`` in front of ``bytes``. Measured on
the Guadalupe 2025 case (10 940 reaches): the 73-frame analysis shard is
145,650 bytes under the delta chain against 163,086 under ``[bytes, zstd]``,
the 18-frame forecast 33,631 against 38,768 — consecutive hours of one
reach move by a code or two (docs/zarr-profile.md, "Network store")."""


class NetworkStoreError(XueError):
    """A network store that cannot be written or does not read back."""


@dataclass
class NetworkStoreReport:
    """What a river index records of a store just written."""

    path: Path
    byte_length: int = 0
    crc32: str = ""
    shard_bytes: int = 0
    frame_count: int = 0
    feature_count: int = 0
    arrays: dict[str, int] = field(default_factory=dict)


def quantize(values: np.ndarray) -> np.ndarray:
    """Streamflow in m³/s → codes; NaN (no value, a lake reach) → nodata."""
    values = np.asarray(values, dtype=np.float64)
    codes = np.full(values.shape, CODEBOOK.nodata_code, dtype=np.uint8)
    finite = np.isfinite(values)
    if finite.any():
        # A tiny negative from the model's own arithmetic is no flow.
        codes[finite] = CODEBOOK.quantize(np.maximum(values[finite], 0.0))
    return codes


def dequantize(codes: np.ndarray) -> np.ndarray:
    """Codes → m³/s, NaN where nodata."""
    codes = np.asarray(codes, dtype=np.uint8)
    values = np.full(codes.shape, np.nan, dtype=np.float64)
    valid = codes != CODEBOOK.nodata_code
    values[valid] = CODEBOOK.decode(codes[valid])
    return values


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise NetworkStoreError(f"time {value!r} names no zone")
    return moment.astimezone(UTC)


def time_block(run_time: datetime, valid_times: list[datetime]) -> dict[str, Any]:
    """The container's ``time`` block over these frames: offsets from
    ``run_time`` on the coarsest unit that fits (docs/format.md, "Time
    Axis")."""
    leads = [int((moment - run_time).total_seconds()) for moment in valid_times]
    if not leads or any(lead < 0 for lead in leads) or any(b <= a for a, b in zip(leads, leads[1:])):
        raise NetworkStoreError("frames must be at or after runTime and strictly increasing")
    unit = axis_unit_seconds(leads)
    if any(lead % unit for lead in leads):
        raise NetworkStoreError("a frame lies off every whole divisor of an hour")
    offsets = [lead // unit for lead in leads]
    if offsets[-1] > 65534:
        raise NetworkStoreError("the axis reaches past offset 65534")
    return _time_metadata(offsets, unit)


def geometry_descriptor(parquet: Path, row_group_rows: int, feature_count: int) -> dict[str, Any]:
    """The ``geometry`` block a store and a river index carry: the
    GeoParquet beside the store, by length and CRC-32."""
    data = parquet.read_bytes()
    return {
        "path": parquet.name,
        "byteLength": len(data),
        "crc32": crc32_hex(data),
        "featureCount": feature_count,
        "rowGroupRows": row_group_rows,
    }


def _array_metadata(frame_count: int, feature_count: int, row_group_rows: int, delta: bool) -> dict[str, Any]:
    time_chunks = math.ceil(frame_count / TIME_CHUNK)
    feature_chunks = math.ceil(feature_count / row_group_rows)
    inner = ([zarrstore._CODEC_DELTA] if delta else []) + [zarrstore._CODEC_BYTES, zarrstore._CODEC_ZSTD]
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [frame_count, feature_count],
        "data_type": "uint8",
        "chunk_grid": {
            "name": "regular",
            "configuration": {"chunk_shape": [time_chunks * TIME_CHUNK, feature_chunks * row_group_rows]},
        },
        "chunk_key_encoding": {"name": "default"},
        "fill_value": CODEBOOK.nodata_code,
        "codecs": [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [TIME_CHUNK, row_group_rows],
                    "codecs": inner,
                    "index_codecs": [zarrstore._CODEC_BYTES_LITTLE, zarrstore._CODEC_CRC32C],
                    "index_location": "end",
                },
            }
        ],
        "attributes": {"xue_network": {"variable": VARIABLE}, "_FillValue": CODEBOOK.nodata_code},
        "dimension_names": list(DIMENSIONS),
    }


def _side_arrays(
    feature_ids: np.ndarray, valid_times: list[datetime], baseflow: np.ndarray
) -> dict[str, tuple[dict[str, Any], bytes]]:
    seconds = np.array([int(moment.timestamp()) for moment in valid_times], dtype="<i8")
    return {
        "feature_id": (
            zarrstore._coordinate_metadata(len(feature_ids), "int64", 0, {"long_name": "reach identifier"}, "feature"),
            feature_ids.astype("<i8").tobytes(),
        ),
        "time": (
            zarrstore._coordinate_metadata(
                len(seconds),
                "int64",
                0,
                {"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian", "standard_name": "time"},
                "time",
            ),
            seconds.tobytes(),
        ),
        "baseflow": (
            zarrstore._coordinate_metadata(len(baseflow), "float32", "NaN", {"units": "m3/s", "long_name": "baseflow"}, "feature"),
            baseflow.astype("<f4").tobytes(),
        ),
    }


def write_network_store(
    zarr_dir: Path,
    *,
    model: str,
    product: str,
    run_time: datetime,
    valid_times: list[datetime],
    values: np.ndarray,
    feature_ids: np.ndarray,
    baseflow: np.ndarray,
    geometry: dict[str, Any],
    delta: bool = DELTA,
) -> NetworkStoreReport:
    """Write one track as a network store. ``values`` is ``[frame,
    feature]`` m³/s in the GeoParquet's row order (NaN for no value), and
    ``geometry`` the descriptor of that GeoParquet
    (:func:`geometry_descriptor`). ``zarr_dir`` is replaced whole."""
    values = np.asarray(values)
    frame_count, feature_count = values.shape if values.ndim == 2 else (0, 0)
    if values.ndim != 2 or not frame_count or not feature_count:
        raise NetworkStoreError("values must be a non-empty [frame, feature] matrix")
    if len(valid_times) != frame_count:
        raise NetworkStoreError("one valid time per frame")
    feature_ids = np.asarray(feature_ids, dtype=np.int64)
    baseflow = np.asarray(baseflow, dtype=np.float32)
    if feature_ids.shape != (feature_count,) or baseflow.shape != (feature_count,):
        raise NetworkStoreError("feature ids and baseflow must hold one value per feature")
    if len(np.unique(feature_ids)) != feature_count:
        raise NetworkStoreError("feature ids must be unique")
    if geometry.get("featureCount") != feature_count:
        raise NetworkStoreError("the geometry's featureCount is not the store's")
    rows = geometry.get("rowGroupRows")
    if not isinstance(rows, int) or rows < 1:
        raise NetworkStoreError("rowGroupRows must be a positive integer")
    run_time = run_time.astimezone(UTC)
    time = time_block(run_time, valid_times)

    if zarr_dir.exists():
        import shutil

        shutil.rmtree(zarr_dir)
    zarr_dir.mkdir(parents=True)
    report = NetworkStoreReport(path=zarr_dir, frame_count=frame_count, feature_count=feature_count)

    codes = quantize(values)
    time_chunks = math.ceil(frame_count / TIME_CHUNK)
    feature_chunks = math.ceil(feature_count / rows)
    padded = np.full((time_chunks * TIME_CHUNK, feature_chunks * rows), CODEBOOK.nodata_code, dtype=np.uint8)
    padded[:frame_count, :feature_count] = codes

    array = _array_metadata(frame_count, feature_count, rows, delta)
    shard = zarrstore._ShardWriter(zarr_dir / ARRAY / "c" / "0" / "0", time_chunks * feature_chunks, "end")
    for chunk in range(time_chunks):
        blocks = [
            np.ascontiguousarray(padded[chunk * TIME_CHUNK : (chunk + 1) * TIME_CHUNK, group * rows : (group + 1) * rows])
            for group in range(feature_chunks)
        ]
        if delta:
            blocks = [zarrstore._delta_encode(block) for block in blocks]
        shard.extend([zarrstore._compress_block(block) for block in blocks])
    report.shard_bytes = shard.close()
    report.arrays[ARRAY] = report.shard_bytes + zarrstore._write(zarr_dir / ARRAY / "zarr.json", zarrstore._dump_json(array))

    metadata = {ARRAY: array}
    for name, (document, data) in _side_arrays(feature_ids, valid_times, baseflow).items():
        report.arrays[name] = zarrstore._write(zarr_dir / name / "zarr.json", zarrstore._dump_json(document)) + zarrstore._write(
            zarr_dir / name / "c" / "0", data
        )
        metadata[name] = document

    block = {
        "version": NETWORK_VERSION,
        "model": model,
        "product": product,
        "runTime": iso_z(run_time),
        "time": time,
        "geometry": dict(geometry),
        "variables": [VARIABLE],
    }
    root = zarrstore._dump_json(
        {
            "zarr_format": 3,
            "node_type": "group",
            "attributes": {"xue_network": block},
            "consolidated_metadata": {"kind": "inline", "must_understand": False, "metadata": metadata},
        }
    )
    zarrstore._write(zarr_dir / "zarr.json", root)
    report.byte_length = zarrstore.store_byte_length(zarr_dir)
    report.crc32 = crc32_hex(root)
    return report


# -- reading (NumPy only) -------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NetworkStoreError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise NetworkStoreError(f"{path} is not a JSON object")
    return payload


def frame_times(block: dict[str, Any]) -> list[datetime]:
    """The valid time of every frame the block's ``time`` declares."""
    run_time = _parse_time(block["runTime"])
    time = block["time"]
    unit = time["unitSeconds"]
    if "frameOffsets" in time:
        offsets = list(time["frameOffsets"])
    else:
        offsets = [time["firstFrameOffset"] + i * time["frameStep"] for i in range(time["frameCount"])]
    return [datetime.fromtimestamp(run_time.timestamp() + offset * unit, UTC) for offset in offsets]


def read_group(zarr_dir: Path) -> dict[str, Any]:
    """The store's ``xue_network`` block, checked: a group carrying it and
    neither of the other branches' blocks, this version, a well-formed time
    block and geometry, one variable."""
    group = _read_json(zarr_dir / "zarr.json")
    attributes = group.get("attributes")
    if group.get("node_type") != "group" or not isinstance(attributes, dict):
        raise NetworkStoreError(f"{zarr_dir} is not a Zarr group")
    block = attributes.get("xue_network")
    if not isinstance(block, dict) or "xue" in attributes or "xue_polar" in attributes:
        raise NetworkStoreError(f"{zarr_dir} is not a network store (exactly one xue_network block)")
    if block.get("version") != NETWORK_VERSION:
        raise NetworkStoreError(f"{zarr_dir}: network store version {block.get('version')!r} is not {NETWORK_VERSION}")
    for key in ("model", "product", "runTime"):
        if not isinstance(block.get(key), str) or not block[key]:
            raise NetworkStoreError(f"{zarr_dir}: xue_network.{key} must be a non-empty string")
    time = block.get("time")
    if not isinstance(time, dict) or ("frameStep" in time) == ("frameOffsets" in time):
        raise NetworkStoreError(f"{zarr_dir}: time must declare exactly one of frameStep and frameOffsets")
    for key in ("unitSeconds", "firstFrameOffset", "frameCount"):
        if isinstance(time.get(key), bool) or not isinstance(time.get(key), int):
            raise NetworkStoreError(f"{zarr_dir}: time.{key} must be an integer")
    if time["unitSeconds"] < 1 or 3600 % time["unitSeconds"] or time["frameCount"] < 1:
        raise NetworkStoreError(f"{zarr_dir}: time block is malformed")
    if "frameOffsets" in time and len(time["frameOffsets"]) != time["frameCount"]:
        raise NetworkStoreError(f"{zarr_dir}: frameOffsets must hold frameCount offsets")
    geometry = block.get("geometry")
    if not isinstance(geometry, dict) or set(geometry) != {"path", "byteLength", "crc32", "featureCount", "rowGroupRows"}:
        raise NetworkStoreError(f"{zarr_dir}: geometry must be {{path, byteLength, crc32, featureCount, rowGroupRows}}")
    variables = block.get("variables")
    if not isinstance(variables, list) or len(variables) != 1 or not isinstance(variables[0], dict) or variables[0].get("id") != ARRAY:
        raise NetworkStoreError(f"{zarr_dir}: variables must list the one {ARRAY} variable")
    return block


def _layout(zarr_dir: Path, block: dict[str, Any]) -> tuple[list[int], list[int], bool]:
    """Shape, inner chunk and chain of the data array, held to the block."""
    array = _read_json(zarr_dir / ARRAY / "zarr.json")
    shape = array.get("shape")
    frames, features = block["time"]["frameCount"], block["geometry"]["featureCount"]
    rows = block["geometry"]["rowGroupRows"]
    if array.get("data_type") != "uint8" or shape != [frames, features] or array.get("dimension_names") != list(DIMENSIONS):
        raise NetworkStoreError(f"{zarr_dir}: {ARRAY} must be uint8 [time, feature] matching the block")
    codecs = array.get("codecs")
    if not isinstance(codecs, list) or len(codecs) != 1 or codecs[0].get("name") != "sharding_indexed":
        raise NetworkStoreError(f"{zarr_dir}: {ARRAY} must carry exactly one sharding_indexed codec")
    configuration = codecs[0]["configuration"]
    inner = configuration.get("chunk_shape")
    if inner != [TIME_CHUNK, rows]:
        raise NetworkStoreError(f"{zarr_dir}: the inner chunk must be [{TIME_CHUNK}, rowGroupRows]")
    outer = array["chunk_grid"]["configuration"]["chunk_shape"]
    if outer != [math.ceil(frames / TIME_CHUNK) * TIME_CHUNK, math.ceil(features / rows) * rows]:
        raise NetworkStoreError(f"{zarr_dir}: the shard must be the whole array in whole inner chunks")
    names = [codec.get("name") for codec in configuration.get("codecs", [])]
    if names not in (["bytes", "zstd"], [zarrstore.DELTA_CODEC, "bytes", "zstd"]):
        raise NetworkStoreError(f"{zarr_dir}: inner chain must be [bytes, zstd] or [xue.delta, bytes, zstd]")
    if configuration.get("index_location") != "end" or array.get("fill_value") != CODEBOOK.nodata_code:
        raise NetworkStoreError(f"{zarr_dir}: index at the end and fill_value {CODEBOOK.nodata_code} expected")
    return shape, inner, names[0] == zarrstore.DELTA_CODEC


def read_codes(zarr_dir: Path) -> np.ndarray:
    """Every frame's codes, ``[frame, feature]``, through the shard index."""
    block = read_group(zarr_dir)
    (frames, features), (depth, rows), delta = _layout(zarr_dir, block)
    time_chunks, feature_chunks = math.ceil(frames / depth), math.ceil(features / rows)
    shard = (zarr_dir / ARRAY / "c" / "0" / "0").read_bytes()
    count = time_chunks * feature_chunks
    try:
        pairs = zarrstore._parse_index(zarrstore.shard_index_bytes(shard, count, "end"), count)
    except XueError as exc:
        raise NetworkStoreError(f"{zarr_dir}: {exc}") from exc
    out = np.full((time_chunks * depth, feature_chunks * rows), CODEBOOK.nodata_code, dtype=np.uint8)
    for position, (offset, nbytes) in enumerate(pairs):
        if offset == zarrstore._EMPTY_ENTRY and nbytes == zarrstore._EMPTY_ENTRY:
            continue
        chunk, group = divmod(position, feature_chunks)
        raw = zstdcli.decompress(shard[offset : offset + nbytes], expected_length=depth * rows)
        stored = np.frombuffer(raw, dtype=np.uint8).reshape(depth, rows)
        if delta:
            stored = zarrstore._delta_decode(stored)
        out[chunk * depth : (chunk + 1) * depth, group * rows : (group + 1) * rows] = stored
    return out[:frames, :features]


def read_side(zarr_dir: Path, name: str) -> np.ndarray:
    """One side array (``feature_id``, ``time``, ``baseflow``)."""
    dtypes = {"feature_id": "<i8", "time": "<i8", "baseflow": "<f4"}
    if name not in dtypes:
        raise NetworkStoreError(f"no side array {name!r}")
    return np.frombuffer((zarr_dir / name / "c" / "0").read_bytes(), dtype=dtypes[name])
