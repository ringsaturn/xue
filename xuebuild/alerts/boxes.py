"""The boxes of a boundary set's area codes (``<set>.<version>.bbox.json``,
``docs/alerts.md`` §9), which give an alert that names areas rather than
drawing them a box in its index row.

The file is pinned by length and CRC32 in the network table like the tiles,
read from a cache directory and, when the cache lacks it, fetched once over
HTTPS from the public data origin under its ``?v=<crc32>``. A build that
cannot have it (offline, the origin down) leaves those rows' boxes null and
says so in the log: a missing box is a lesser round, not a failed one.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path
from typing import Any

from ..errors import XueError
from .http import get
from .networks import AreaSet
from .schema import AREAS_DIRECTORY, crc32_hex, write_bytes_atomic

LOG = logging.getLogger(__name__)

DATA_ORIGIN = "https://dataset.ringsaturn.me/xue/"
"""Where the boundary sets are published, beside every other data file."""

Boxes = dict[str, dict[str, tuple[float, float, float, float]]]
"""layer → code → (west, south, east, north)."""


def _parse(body: bytes, area_set: AreaSet) -> Boxes:
    payload = json.loads(body)
    if payload.get("set") != area_set.set or payload.get("version") != area_set.version:
        raise ValueError(f"{area_set.bbox_filename} names another set")
    return {
        layer: {code: (box[0], box[1], box[2], box[3]) for code, box in codes.items()}
        for layer, codes in payload["layers"].items()
    }


def load_boxes(area_set: AreaSet, cache: Path, base_url: str | None) -> Boxes | None:
    """The set's boxes from ``cache/alerts-areas/``, else from ``base_url``
    (then cached), each only when its CRC32 is the pinned one; None when
    neither has them."""
    path = cache / AREAS_DIRECTORY / area_set.bbox_filename
    if path.exists():
        body = path.read_bytes()
        if crc32_hex(body) == area_set.bbox_crc32 and len(body) == area_set.bbox_byte_length:
            return _parse(body, area_set)
        LOG.warning("alerts: %s is not the pinned CRC32 %s; fetching it again", path, area_set.bbox_crc32)
    if base_url is None:
        LOG.warning("alerts: no %s and nowhere to fetch it; its codes' rows have no box", area_set.bbox_filename)
        return None
    url = f"{base_url.rstrip('/')}/{AREAS_DIRECTORY}/{area_set.bbox_filename}?v={area_set.bbox_crc32}"
    try:
        response = get(url, accept="application/json")
        if response.body is None:
            raise XueError(f"{url}: HTTP {response.status}")
        if crc32_hex(response.body) != area_set.bbox_crc32:
            raise XueError(f"{url} arrived as CRC32 {crc32_hex(response.body)}, not the pinned {area_set.bbox_crc32}")
        boxes = _parse(response.body, area_set)
    except (XueError, urllib.error.URLError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        LOG.warning("alerts: %s; its codes' rows have no box", exc)
        return None
    write_bytes_atomic(path, response.body)
    return boxes


def area_box(codes: list[str], layers: tuple[str, ...], boxes: Boxes) -> tuple[float, float, float, float] | None:
    """The union of the codes' boxes, each code looked up in ``layers`` in
    order; None when there are no codes or any of them is in none of the
    layers, since a box that leaves out part of the area would hide the
    alert from a reader asking about that part."""
    found: list[tuple[float, float, float, float]] = []
    for code in codes:
        box = next((boxes[layer][code] for layer in layers if code in boxes.get(layer, {})), None)
        if box is None:
            return None
        found.append(box)
    if not found:
        return None
    return (
        min(box[0] for box in found),
        min(box[1] for box in found),
        max(box[2] for box in found),
        max(box[3] for box in found),
    )
