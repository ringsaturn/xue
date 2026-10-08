"""Read physical values out of published Xue Zarr stores over HTTP Range
requests, the way any outside reader does (``docs/zarr-profile.md``,
"Reading"), with NumPy and the standard library alone.

The path: the live pointer (``latest*.json``) → the run's manifest → a
bundle's ``zarr`` descriptor → the store's group ``zarr.json`` (its
``attributes.xue`` is the bundle metadata, parsed by :mod:`binformat`'s own
validator) → the array's ``zarr.json`` → the shard index, once per array, as
the suffix range ``bytes=-N`` and CRC-32C checked → only the inner chunks of
the tiles a set of windows covers, the ranges of one time chunk sorted and
merged where they lie close → Zstandard → the variable's codebook
(:func:`quantize.codebook_from_metadata`) → values. Scalar bundles only.

Every store object is fetched under the manifest descriptor's ``?v=``, the
manifest under the pointer's, and the pointer itself bare.
"""

from __future__ import annotations

import json
import math
import posixpath
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import binformat, zarrstore, zstdcli
from ..errors import XueError
from ..quantize import PrecipitationCodebook, TemperatureCodebook, codebook_from_metadata
from ..sources import source_spec
from .schema import IndicatorsError

USER_AGENT = "xue-indicators (+https://github.com/ringsaturn/xue)"
MERGE_GAP = 64 * 1024
"""Neighbouring inner chunks of one time chunk closer than this are fetched
as one range: the over-read costs less than the round trip it saves
(docs/zarr-profile.md, "Reading" 7)."""
TIMEOUT_SECONDS = 60
RETRIES = 3
FETCH_THREADS = 8
_INDEX_ENTRY = struct.Struct("<QQ")
_EMPTY = 0xFFFFFFFFFFFFFFFF


class StoreError(IndicatorsError):
    """A store, manifest or pointer that cannot be read as the profile says."""


# -- HTTP ----------------------------------------------------------------------

Fetch = Callable[[str, tuple[int, int] | int | None], bytes]
"""``fetch(url, span)``: the whole object for ``None``, the inclusive byte
range ``(first, last)``, or the last ``n`` bytes for an ``int``."""


def _expected_span(span: tuple[int, int] | int | None) -> str | None:
    if span is None:
        return None
    if isinstance(span, int):
        return f"bytes=-{span}"
    return f"bytes={span[0]}-{span[1]}"


def http_fetch(url: str, span: tuple[int, int] | int | None = None) -> bytes:
    """GET one object or one range of it, retrying transient failures. A
    server that answers a range request with the whole object (200) is
    sliced to the range; a 206 must be exactly the range asked for."""
    headers = {"User-Agent": USER_AGENT}
    header = _expected_span(span)
    if header is not None:
        headers["Range"] = header
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (404, 403, 410, 416):
                raise StoreError(f"{url}: HTTP {exc.code}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        else:
            if span is None:
                return body
            if status == 200:
                if isinstance(span, int):
                    return body[-span:] if span <= len(body) else body
                return body[span[0] : span[1] + 1]
            if status == 206:
                wanted = span if isinstance(span, int) else span[1] - span[0] + 1
                if isinstance(span, tuple) and len(body) != wanted:
                    raise StoreError(f"{url}: a {len(body)}-byte answer to {header}")
                if isinstance(span, int) and len(body) > wanted:
                    raise StoreError(f"{url}: a {len(body)}-byte answer to {header}")
                return body
            raise StoreError(f"{url}: unexpected HTTP {status}")
        time.sleep(0.5 * (2**attempt))
    raise StoreError(f"{url}: {last_error}")


def local_fetch(path: str, span: tuple[int, int] | int | None = None) -> bytes:
    """:data:`Fetch` over the local file system, the query string ignored:
    the same reader runs on a directory as on the bucket."""
    target = Path(urllib.parse.urlsplit(path).path if path.startswith("file:") else path.split("?", 1)[0])
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise StoreError(f"cannot read {target}: {exc}") from exc
    if span is None:
        return data
    if isinstance(span, int):
        return data[-span:]
    return data[span[0] : span[1] + 1]


def fetch_for(location: str | Path) -> tuple[str, Fetch]:
    """A location's root, ending in ``/``, and the fetch that reads under
    it: HTTP(S) for a URL, the file system for a path."""
    text = str(location)
    if text.startswith(("http://", "https://")):
        return (text if text.endswith("/") else text + "/"), http_fetch
    return str(Path(text)) + "/", local_fetch


def _versioned(url: str, crc32: str | None) -> str:
    return f"{url}?v={crc32}" if crc32 else url


def _json(fetch: Fetch, url: str) -> dict[str, Any]:
    try:
        payload = json.loads(fetch(url, None))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"{url} is not JSON") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"{url} is not a JSON object")
    return payload


# -- runs ----------------------------------------------------------------------

@dataclass(frozen=True)
class RunRef:
    """One published run: where its manifest lives and what it says."""

    source: str
    run: str
    run_time: datetime
    manifest_path: str
    """Relative to the data root, as the pointer names it."""
    manifest_crc32: str | None
    manifest: dict[str, Any]
    base_url: str

    @property
    def directory_url(self) -> str:
        directory = posixpath.dirname(self.manifest_path)
        return urllib.parse.urljoin(self.base_url, f"{directory}/" if directory else "")

    @property
    def forecast_hours(self) -> int:
        return int(self.manifest.get("forecastHours", 0))

    def bundle(self, bundle_id: str) -> dict[str, Any]:
        for entry in self.manifest.get("bundles", []):
            if isinstance(entry, dict) and entry.get("variable") == bundle_id:
                return entry
        raise StoreError(f"{self.source} {self.run} publishes no {bundle_id} bundle")


def _base(base_url: str) -> str:
    return base_url if base_url.endswith("/") else base_url + "/"


def _run_ref(source: str, base_url: str, manifest_path: str, crc32: str | None, fetch: Fetch) -> RunRef:
    manifest = _json(fetch, _versioned(urllib.parse.urljoin(base_url, manifest_path), crc32))
    run_time_text = manifest.get("runTime")
    if not isinstance(run_time_text, str) or not run_time_text.endswith("Z"):
        raise StoreError(f"{manifest_path}: manifest has no runTime")
    run_time = datetime.fromisoformat(run_time_text).astimezone(UTC)
    if manifest.get("model") != source_spec(source).manifest_model:
        raise StoreError(f"{manifest_path}: manifest model {manifest.get('model')!r} is not {source}")
    return RunRef(source, f"{run_time:%Y%m%d%H}", run_time, manifest_path, crc32, manifest, base_url)


def resolve_latest(source: str, base_url: str, fetch: Fetch = http_fetch) -> RunRef:
    """The run a source's live pointer names."""
    spec = source_spec(source)
    if spec.latest_filename is None:
        raise StoreError(f"{source} has no live pointer")
    base = _base(base_url)
    pointer = _json(fetch, urllib.parse.urljoin(base, spec.latest_filename))
    path, crc32 = pointer.get("manifestPath"), pointer.get("manifestCrc32")
    if not isinstance(path, str) or not isinstance(crc32, str):
        raise StoreError(f"{spec.latest_filename} names no manifest")
    ref = _run_ref(source, base, path, crc32, fetch)
    if pointer.get("run") != ref.run:
        raise StoreError(f"{spec.latest_filename} names run {pointer.get('run')} but its manifest is {ref.run}")
    return ref


def open_run(source: str, run: str, base_url: str, fetch: Fetch = http_fetch) -> RunRef:
    """A named run, ``<source>.<run>/manifest.json`` under the data root,
    read bare (an archived run has no pointer to take a ``?v=`` from)."""
    return _run_ref(source, _base(base_url), f"{source}.{run}/manifest.json", None, fetch)


# -- arrays --------------------------------------------------------------------

def parse_bundle_metadata(metadata: object) -> binformat.Bundle:
    """The bundle metadata a store's group carries, validated by the
    container reader's own metadata parser (the profile says they are the
    same document). The returned object has the metadata attributes only."""
    raw = json.dumps(metadata).encode("utf-8")
    parsed = binformat.Bundle.__new__(binformat.Bundle)
    parsed.data = raw
    parsed.metadata_offset = 0
    parsed.metadata_length = len(raw)
    try:
        parsed._parse_metadata()
    except XueError as exc:
        raise StoreError(f"store metadata: {exc}") from exc
    return parsed


@dataclass
class StoreArray:
    """One scalar variable of one store, opened: geometry, codec chain,
    codebook and time axis, and its shard index once read."""

    url: str
    """The array's directory URL, ending in ``/``."""
    crc32: str | None
    variable_id: str
    unit: str
    run_time: datetime
    offsets_seconds: list[int]
    grid: dict[str, Any]
    geometry: zarrstore.ArrayGeometry
    delta: bool
    index_location: str
    fill_value: int
    codebook: TemperatureCodebook | PrecipitationCodebook
    _indexes: dict[int, list[tuple[int, int]]] = field(default_factory=dict)


def open_array(run: RunRef, bundle_id: str, fetch: Fetch = http_fetch) -> StoreArray:
    """Open a scalar bundle's store: the group and the array documents."""
    entry = run.bundle(bundle_id)
    descriptor = entry.get("zarr")
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("path"), str):
        raise StoreError(f"{run.source} {run.run} {bundle_id} has no Zarr store")
    crc32 = descriptor.get("crc32") if isinstance(descriptor.get("crc32"), str) else None
    root = urllib.parse.urljoin(run.directory_url, descriptor["path"].rstrip("/") + "/")
    group = _json(fetch, _versioned(root + "zarr.json", crc32))
    attributes = group.get("attributes") or {}
    if attributes.get("xue_profile") != zarrstore.PROFILE_VERSION or "xue" not in attributes:
        raise StoreError(f"{root}: not a Xue profile {zarrstore.PROFILE_VERSION} store")
    metadata = parse_bundle_metadata(attributes["xue"])
    variables = attributes["xue"]["variables"]
    if len(variables) != 1:
        raise StoreError(f"{root}: {bundle_id} is not a scalar bundle")
    variable = variables[0]
    variable_id = variable["id"]
    array = _json(fetch, _versioned(f"{root}{variable_id}/zarr.json", crc32))
    geometry, delta, index_location, fill_value = _array_layout(array, root)
    if geometry.shape != (metadata.frame_count, metadata.height, metadata.width):
        raise StoreError(f"{root}: the array is not the shape its metadata describes")
    quantization = variable.get("quantization")
    if not isinstance(quantization, dict):
        raise StoreError(f"{root}: {variable_id} has no quantization block")
    try:
        codebook = codebook_from_metadata(quantization, name=variable_id)
    except XueError as exc:
        raise StoreError(f"{root}: {exc}") from exc
    if codebook.nodata_code != fill_value:
        raise StoreError(f"{root}: fill_value is not the variable's nodata code")
    return StoreArray(
        url=f"{root}{variable_id}/",
        crc32=crc32,
        variable_id=variable_id,
        unit=str(variable.get("unit", "")),
        run_time=datetime.fromisoformat(attributes["xue"]["runTime"]).astimezone(UTC),
        offsets_seconds=[offset * metadata.unit_seconds for offset in metadata.frame_offsets],
        grid=attributes["xue"]["grid"],
        geometry=geometry,
        delta=delta,
        index_location=index_location,
        fill_value=fill_value,
        codebook=codebook,
    )


def _array_layout(metadata: dict[str, Any], root: str) -> tuple[zarrstore.ArrayGeometry, bool, str, int]:
    """Validate an array document (docs/zarr-profile.md, "Reading" 2)."""
    try:
        geometry = zarrstore.ArrayGeometry.from_metadata(metadata)
    except XueError as exc:
        raise StoreError(f"{root}: {exc}") from exc
    if metadata.get("zarr_format") != 3 or metadata.get("data_type") != "uint8":
        raise StoreError(f"{root}: not a uint8 Zarr v3 array")
    if metadata.get("chunk_grid", {}).get("name") != "regular":
        raise StoreError(f"{root}: chunk grid is not regular")
    configuration = metadata["codecs"][0].get("configuration", {})
    names = [codec.get("name") for codec in configuration.get("codecs", [])]
    if names not in ([zarrstore.DELTA_CODEC, "bytes", "zstd"], ["bytes", "zstd"]):
        raise StoreError(f"{root}: unexpected inner codec chain {names}")
    index_codecs = configuration.get("index_codecs", [])
    if [codec.get("name") for codec in index_codecs] != ["bytes", "crc32c"] or (
        index_codecs[0].get("configuration", {}).get("endian", "little") != "little"
    ):
        raise StoreError(f"{root}: index codecs must be [bytes little-endian, crc32c]")
    index_location = configuration.get("index_location", "end")
    if index_location not in zarrstore.INDEX_LOCATIONS:
        raise StoreError(f"{root}: unexpected index location {index_location!r}")
    fill_value = metadata.get("fill_value")
    if isinstance(fill_value, bool) or not isinstance(fill_value, int) or not 0 <= fill_value <= 255:
        raise StoreError(f"{root}: fill_value must be a byte")
    return geometry, names[0] == zarrstore.DELTA_CODEC, index_location, fill_value


def _shard_url(array: StoreArray, shard: int) -> str:
    return _versioned(f"{array.url}c/{shard}/0/0", array.crc32)


# -- shards and inner chunks (shared with the weights reader) --------------------

def read_shard_index(
    fetch: Fetch, url: str, count: int, index_location: str, label: str
) -> list[tuple[int, int]]:
    """A shard's ``(offset, nbytes)`` pairs: one read at the end the array
    declares (the suffix range ``bytes=-N`` at the end), CRC-32C checked."""
    length = _INDEX_ENTRY.size * count + 4
    span: tuple[int, int] | int = length if index_location == "end" else (0, length - 1)
    index = fetch(url, span)
    if len(index) != length:
        raise StoreError(f"{label}: shard index is {len(index)} bytes, expected {length}")
    entries, checksum = index[:-4], index[-4:]
    if struct.unpack("<I", checksum)[0] != zarrstore.crc32c(entries):
        raise StoreError(f"{label}: shard index CRC-32C mismatch")
    return [_INDEX_ENTRY.unpack_from(entries, slot * _INDEX_ENTRY.size) for slot in range(count)]


def shard_index(array: StoreArray, shard: int, fetch: Fetch = http_fetch) -> list[tuple[int, int]]:
    """A shard's ``(offset, nbytes)`` pairs, read once and held."""
    if shard not in array._indexes:
        array._indexes[shard] = read_shard_index(
            fetch, _shard_url(array, shard), array.geometry.chunks_per_shard, array.index_location, array.url
        )
    return array._indexes[shard]


def tiles_for(geometry: zarrstore.ArrayGeometry, rows: slice, cols: slice) -> list[int]:
    """The inner-chunk tiles, row-major, a window of the grid touches."""
    first_row, last_row = rows.start // geometry.tile_height, (rows.stop - 1) // geometry.tile_height
    first_col, last_col = cols.start // geometry.tile_width, (cols.stop - 1) // geometry.tile_width
    return [
        tile_row * geometry.tile_columns + tile_col
        for tile_row in range(first_row, last_row + 1)
        for tile_col in range(first_col, last_col + 1)
    ]


Span = tuple[int, int, list[tuple[int, int, int]]]
"""A merged range ``(start, end, members)``, each member ``(offset,
length, tile)``."""


def plan_ranges(index: Sequence[tuple[int, int]], first_slot: int, tiles: Sequence[int], gap: int = MERGE_GAP) -> list[Span]:
    """The ranges that fetch ``tiles`` of one run of a shard index (the run
    starting at ``first_slot``): sorted by offset, neighbours within ``gap``
    merged. A never-written chunk is left out; it reads as the fill value."""
    spans = []
    for tile in tiles:
        offset, length = index[first_slot + tile]
        if offset == _EMPTY and length == _EMPTY:
            continue
        spans.append((offset, length, tile))
    merged: list[Span] = []
    for offset, length, tile in sorted(spans):
        if merged and offset - merged[-1][1] <= gap:
            begin, end, members = merged[-1]
            merged[-1] = (begin, max(end, offset + length), [*members, (offset, length, tile)])
        else:
            merged.append((offset, offset + length, [(offset, length, tile)]))
    return merged


def load_range(
    fetch: Fetch,
    url: str,
    span: Span,
    inner_shape: tuple[int, ...],
    dtype: np.dtype,
    *,
    require_checksum: bool,
    label: str,
) -> list[tuple[int, np.ndarray]]:
    """Fetch one merged range and decompress each inner chunk in it to
    exactly ``inner_shape`` of ``dtype``: ``(tile, block)`` pairs."""
    begin, end, members = span
    payload = fetch(url, (begin, end - 1))
    if len(payload) != end - begin:
        raise StoreError(f"{label}: truncated read of {begin}-{end - 1}")
    expected = math.prod(inner_shape) * np.dtype(dtype).itemsize
    blocks = []
    for offset, length, tile in members:
        frame = payload[offset - begin : offset - begin + length]
        try:
            if require_checksum and not zstdcli.frame_has_checksum(frame):
                raise StoreError(f"{label}: an inner chunk without a content checksum")
            raw = zstdcli.decompress(frame, expected_length=expected)
        except XueError as exc:
            raise StoreError(f"{label}: chunk {tile}: {exc}") from exc
        blocks.append((tile, np.frombuffer(raw, dtype=dtype).reshape(inner_shape)))
    return blocks


def paste_tile(
    out: np.ndarray, rows: slice, cols: slice, geometry: zarrstore.ArrayGeometry, tile: int, block: np.ndarray
) -> None:
    """Copy the part of a tile's block (``(..., tileHeight, tileWidth)``)
    that lies in the window ``rows`` x ``cols`` into ``out`` (``(...,
    window rows, window cols)``); the padding past the grid never lands."""
    origin_row, origin_col = geometry.tile_origin(tile)
    row0, row1 = max(rows.start, origin_row), min(rows.stop, origin_row + geometry.tile_height)
    col0, col1 = max(cols.start, origin_col), min(cols.stop, origin_col + geometry.tile_width)
    out[..., row0 - rows.start : row1 - rows.start, col0 - cols.start : col1 - cols.start] = block[
        ..., row0 - origin_row : row1 - origin_row, col0 - origin_col : col1 - origin_col
    ]


def read_codes(
    array: StoreArray,
    windows: Mapping[str, tuple[slice, slice]],
    fetch: Fetch = http_fetch,
    *,
    threads: int = FETCH_THREADS,
) -> dict[str, np.ndarray]:
    """The codes of every frame inside each window, ``(frames, rows,
    cols)`` uint8 per window name. The tiles all windows need are read
    once: per time chunk, their inner chunks' ranges are sorted and merged
    (:data:`MERGE_GAP`), so a window costs about one range per tile row per
    time chunk and windows sharing tiles share them."""
    geometry = array.geometry
    for name, (rows, cols) in windows.items():
        if not (0 <= rows.start < rows.stop <= geometry.height and 0 <= cols.start < cols.stop <= geometry.width):
            raise StoreError(f"window {name} lies outside the {geometry.height} x {geometry.width} grid")
    tiles = sorted({tile for rows, cols in windows.values() for tile in tiles_for(geometry, rows, cols)})
    requests: list[tuple[int, int, Span]] = []
    for time_chunk in range(geometry.time_chunks):
        shard, first_slot = geometry.shard_of(time_chunk)
        index = shard_index(array, shard, fetch)
        requests.extend((time_chunk, shard, span) for span in plan_ranges(index, first_slot, tiles))

    def load(request: tuple[int, int, Span]) -> list[tuple[int, int, np.ndarray]]:
        time_chunk, shard, span = request
        blocks = load_range(
            fetch, _shard_url(array, shard), span, geometry.inner_shape, np.dtype(np.uint8),
            require_checksum=True, label=f"{array.url} time chunk {time_chunk}",
        )
        if array.delta:
            return [(time_chunk, tile, np.cumsum(block, axis=0, dtype=np.uint8)) for tile, block in blocks]
        return [(time_chunk, tile, block) for tile, block in blocks]

    with ThreadPoolExecutor(max_workers=max(1, threads)) as executor:
        loaded = [block for blocks in executor.map(load, requests) for block in blocks]
    chunks = {(time_chunk, tile): block for time_chunk, tile, block in loaded}

    result: dict[str, np.ndarray] = {}
    for name, (rows, cols) in windows.items():
        out = np.full((geometry.frame_count, rows.stop - rows.start, cols.stop - cols.start), array.fill_value, np.uint8)
        for tile in tiles_for(geometry, rows, cols):
            for time_chunk in range(geometry.time_chunks):
                block = chunks.get((time_chunk, tile))
                if block is not None:
                    frames = geometry.frames(time_chunk)
                    paste_tile(out[frames.start : frames.stop], rows, cols, geometry, tile, block[: len(frames)])
        result[name] = out
    return result


def read_values(
    array: StoreArray,
    windows: Mapping[str, tuple[slice, slice]],
    fetch: Fetch = http_fetch,
    *,
    threads: int = FETCH_THREADS,
) -> dict[str, np.ndarray]:
    """:func:`read_codes` decoded to physical values (float64, in the
    variable's ``unit``). A nodata code inside a window is an error: the
    sources these indicators read have no gaps over land."""
    values = {}
    for name, codes in read_codes(array, windows, fetch, threads=threads).items():
        try:
            values[name] = array.codebook.decode(codes)
        except XueError as exc:
            raise StoreError(f"{array.url} window {name}: {exc}") from exc
    return values
