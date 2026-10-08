"""``context/oni.json``: the CPC Oceanic Niño Index, the one seasonal
context series the product carries (``docs/indicators.md`` §6).

The ONI is the three-month running mean of the Niño 3.4 sea surface
temperature (``total``) and its departure from a 30-year base that CPC
moves every five years (``anomaly``), both in °C. CPC revises the last
months of the table as data settle, so the file holds the newest table
whole, with every fetch that changed it recorded beneath (``fetches``:
when, how many rows, the CRC-32 of the text as served). A fetch that
returns the text already recorded changes nothing.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..common import iso_z, write_bytes_atomic
from .schema import SCHEMA_VERSION, IndicatorsError, encode_json, validate_context

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
ONI_PATH = "context/oni.json"
_ROW = re.compile(r"^\s*([A-Z]{3})\s+(\d{4})\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$")


def parse_oni(text: str) -> list[dict[str, Any]]:
    """The table's rows: ``SEAS YR TOTAL ANOM`` under one header line."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[0].split() != ["SEAS", "YR", "TOTAL", "ANOM"]:
        raise IndicatorsError("the ONI table does not start with its SEAS YR TOTAL ANOM header")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        match = _ROW.match(line)
        if match is None:
            raise IndicatorsError(f"ONI line {number} is not a row: {line!r}")
        season, year, total, anomaly = match.groups()
        rows.append({"season": season, "year": int(year), "total": float(total), "anomaly": float(anomaly)})
    if not rows:
        raise IndicatorsError("the ONI table has no rows")
    return rows


def build_context(text: str, fetched: datetime, previous: dict[str, Any] | None) -> dict[str, Any]:
    rows = parse_oni(text)
    checksum = f"{zlib.crc32(text.encode('utf-8')) & 0xFFFFFFFF:08x}"
    fetches = list(previous["fetches"]) if previous else []
    if fetches and fetches[-1]["crc32"] == checksum and previous is not None:
        return previous
    fetches.append({"fetched": iso_z(fetched), "rows": len(rows), "crc32": checksum})
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "id": "oni",
        "name": "Oceanic Nino Index (Nino 3.4 sea surface temperature, three-month running mean)",
        "unit": "°C",
        "source": {"name": "NOAA Climate Prediction Center", "url": ONI_URL, "license": "Public domain"},
        "fetches": fetches,
        "rows": rows,
    }
    validate_context(payload)
    return payload


def _fetch_text(url: str) -> str:
    from .store import http_fetch  # noqa: PLC0415 — keeps this module's import light

    return http_fetch(url, None).decode("ascii", errors="strict")


def update_context(
    output_dir: Path,
    fetched: datetime,
    *,
    text: str | None = None,
    fetch_text: Callable[[str], str] = _fetch_text,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch the ONI (or take ``text``) and rewrite ``context/oni.json``
    when it changed. Returns a small report."""
    path = output_dir / ONI_PATH
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            validate_context(previous)
        except (OSError, json.JSONDecodeError) as exc:
            raise IndicatorsError(f"cannot read {path}: {exc}") from exc
    if text is None:
        text = fetch_text(ONI_URL)
    payload = build_context(text, fetched, previous)
    changed = payload is not previous
    data = encode_json(payload)
    if changed and not dry_run:
        write_bytes_atomic(path, data)
    return {
        "path": ONI_PATH,
        "changed": changed,
        "rows": len(payload["rows"]),
        "latest": f"{payload['rows'][-1]['season']} {payload['rows'][-1]['year']}",
        "byteLength": len(data),
    }
