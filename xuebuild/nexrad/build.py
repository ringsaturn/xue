"""``nexrad-build``: one round's polar stores and the window manifest over
the rounds before it (``docs/nexrad.md``).

A round takes, per site and product, every sweep newer than the window's
newest for that site and no later than the round itself, so a sweep that
reaches the bucket late lands in the next round instead of being lost, and
none is in two. The stores are written once; the manifest is rewritten
every round from the previous one, dropping rounds that fell out of the
window. A showcase case is the same rounds over a closed past interval,
written as one window store per product instead of a store per round
(``build_case``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import NexradProductError, XueError
from .fetch import Station, download, fetch_stations, key_time, list_keys, parse_stations, raw_path
from .level3 import Sweep, read_sweep
from .schema import (
    POINTER_FILENAME,
    PRODUCTS,
    ROUND_MINUTES,
    SCHEMA_VERSION,
    WINDOW_FILENAME,
    WINDOW_SECONDS,
    WINDOW_STORE_PATH,
    build_pointer,
    encode_json,
    iso_z,
    read_window,
    round_directory,
    round_name,
    validate_window,
    write_bytes_atomic,
)
from .store import SiteSweeps, write_store, write_window_store

LOG = logging.getLogger(__name__)


@dataclass
class _Round:
    """One round as the builder holds it: by site id rather than by the
    manifest's site index, which is renumbered every time the table is."""

    round: datetime
    path: str
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    """product → {group, shard, chunks: [(site, offset, length, sweeps)],
    scans: {site: [times]}}"""


def _rounds_from_window(window: dict[str, Any]) -> list[_Round]:
    sites = [row[0] for row in window["sites"]]
    rounds: list[_Round] = []
    for entry in window["rounds"]:
        held = _Round(datetime.fromisoformat(entry["round"]), entry["path"])
        for product in PRODUCTS:
            block = entry.get(product)
            if block is None:
                continue
            held.products[product] = {
                "group": block["group"],
                "shard": block["shard"],
                "chunks": [(sites[site], offset, length, sweeps) for site, offset, length, sweeps in block["chunks"]],
                "scans": {sites[site]: list(times) for site, times in block["scans"]},
            }
        rounds.append(held)
    return rounds


def _newest(rounds: list[_Round]) -> dict[tuple[str, str], int]:
    newest: dict[tuple[str, str], int] = {}
    for held in rounds:
        for product, block in held.products.items():
            for site, times in block["scans"].items():
                newest[(product, site)] = max(newest.get((product, site), 0), max(times))
    return newest


def _read_sweeps(raw_root: Path, keys: list[str]) -> tuple[list[Sweep], list[str]]:
    sweeps: list[Sweep] = []
    failed: list[str] = []
    for key in keys:
        path = raw_path(raw_root, key)
        if not path.exists():
            failed.append(key)
            continue
        try:
            sweeps.append(read_sweep(path.read_bytes()))
        except XueError as exc:
            LOG.warning("nexrad: %s: %s", key, exc)
            failed.append(key)
    sweeps.sort(key=lambda sweep: sweep.scan_time)
    unique: list[Sweep] = []
    for sweep in sweeps:
        if unique and sweep.scan_time <= unique[-1].scan_time:
            continue  # a re-sent product: the first copy stands
        unique.append(sweep)
    return unique, failed


def build_round(
    moment: datetime,
    *,
    raw_root: Path,
    output_root: Path,
    stations: dict[str, Station],
    sites: list[str],
    previous: dict[str, Any] | None,
    window_seconds: int = WINDOW_SECONDS,
    first_start: datetime | None = None,
    fetch: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build round ``moment`` under ``output_root``: the stores of every
    product with a new sweep, and the window manifest. ``first_start`` is
    where a site with no sweep in the window starts (default: the window's
    start)."""
    now = now or datetime.now(UTC)
    directory = output_root / round_directory(moment)
    rounds = _rounds_from_window(previous) if previous else []
    floor = moment - timedelta(seconds=window_seconds)
    rounds = [held for held in rounds if held.round > floor and held.round < moment]
    newest = _newest(rounds)
    start_default = max(first_start or floor, floor)

    held = _Round(moment, f"{round_directory(moment)}/")
    sources: list[dict[str, Any]] = []
    for product in PRODUCTS:
        per_site: list[SiteSweeps] = []
        reports = 0
        failed_total: list[str] = []
        error: str | None = None
        for site in sites:
            last = newest.get((product, site))
            start = datetime.fromtimestamp(last, UTC) if last else start_default
            try:
                keys = list_keys(site, product, start, moment) if fetch else _keys_on_disk(raw_root, site, product, start, moment)
                if fetch:
                    _fetched, failed = download(raw_root, keys)
                    failed_total += failed
            except XueError as exc:
                error = f"{site}: {exc}"
                LOG.warning("nexrad %s: %s", product, error)
                continue
            sweeps, failed = _read_sweeps(raw_root, keys)
            failed_total += [key for key in failed if key not in failed_total]
            sweeps = [sweep for sweep in sweeps if (last is None or sweep.scan_time.timestamp() > last) and sweep.scan_time <= moment]
            if not sweeps:
                continue
            station = stations.get(site)
            per_site.append(
                SiteSweeps(
                    site=site,
                    latitude=sweeps[0].site_latitude,
                    longitude=sweeps[0].site_longitude,
                    height_m=sweeps[0].site_height_m if station is None else station.height_m,
                    sweeps=sweeps,
                )
            )
            reports += len(sweeps)
        status: dict[str, Any] = {"id": f"unidata-{product}", "ok": error is None or reports > 0, "reports": reports}
        if failed_total:
            status["missing"] = len(failed_total)
        if error is not None:
            status["error"] = error
        if not status["ok"] and "error" not in status:
            status["error"] = "no sweep read"
        sources.append(status)
        if not per_site:
            continue
        report = write_store(directory / f"{product}.zarr", product=product, round_time=moment, sites=per_site)
        held.products[product] = {
            "group": {"byteLength": report.group_bytes, "crc32": report.group_crc32},
            "shard": {"byteLength": report.shard_bytes, "crc32": report.shard_crc32},
            "chunks": report.chunks,
            "scans": {entry.site: [int(sweep.scan_time.timestamp()) for sweep in entry.sweeps] for entry in per_site},
        }
    if held.products:
        rounds.append(held)

    window = _window_payload(moment, rounds, stations, window_seconds, sources, now)
    validate_window(window)
    window_bytes = encode_json(window)
    write_bytes_atomic(directory / WINDOW_FILENAME, window_bytes)
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "window": window,
        "windowBytes": window_bytes,
        "stores": {product: {"sites": len(block["chunks"]), "shardBytes": block["shard"]["byteLength"]} for product, block in held.products.items()},
        "sources": sources,
    }


def _keys_on_disk(raw_root: Path, site: str, product: str, start: datetime, end: datetime) -> list[str]:
    code = {"n0b": "N0B", "n0g": "N0G"}[product]
    folder = raw_root / "nexrad" / site
    if not folder.is_dir():
        return []
    return sorted(
        path.name
        for path in folder.iterdir()
        if path.name.startswith(f"{site}_{code}_") and start < key_time(path.name) <= end
    )


def _window_payload(
    moment: datetime,
    rounds: list[_Round],
    stations: dict[str, Station],
    window_seconds: int,
    sources: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    used = sorted({site for held in rounds for block in held.products.values() for site, *_ in block["chunks"]})
    index = {site: position for position, site in enumerate(used)}
    table = []
    for site in used:
        station = stations.get(site)
        if station is None:
            raise NexradProductError(f"site {site} is not in the station table")
        table.append([site, station.icao, round(station.latitude, 6), round(station.longitude, 6), round(station.height_m, 1)])
    entries = []
    for held in rounds:
        entry: dict[str, Any] = {
            "round": iso_z(held.round),
            # Relative to the manifest, which lives in the newest round's
            # directory: one level up and back down, for its own too. A
            # case's window stores sit beside it.
            "path": held.path if held.path == WINDOW_STORE_PATH else f"../{round_directory(held.round)}/",
        }
        for product in PRODUCTS:
            block = held.products.get(product)
            if block is None:
                continue
            entry[product] = {
                "group": block["group"],
                "shard": block["shard"],
                **({"depth": block["depth"]} if "depth" in block else {}),
                "chunks": [[index[site], offset, length, sweeps] for site, offset, length, sweeps in block["chunks"]],
                "scans": [[index[site], block["scans"][site]] for site, *_ in block["chunks"]],
            }
        entries.append(entry)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(moment),
        "generated": iso_z(now),
        "windowSeconds": window_seconds,
        "sites": table,
        "rounds": entries,
        "sources": sources,
    }


def write_pointer(output_root: Path, moment: datetime, window_bytes: bytes) -> Path:
    pointer = build_pointer(moment, f"{round_directory(moment)}/{WINDOW_FILENAME}", window_bytes)
    path = output_root / POINTER_FILENAME
    write_bytes_atomic(path, encode_json(pointer))
    return path


def load_previous_window(output_root: Path) -> dict[str, Any] | None:
    pointer = output_root / POINTER_FILENAME
    if not pointer.exists():
        return None
    named = json.loads(pointer.read_text(encoding="utf-8")).get("path")
    path = output_root / str(named)
    return read_window(path) if path.exists() else None


def shell_sites(stations: dict[str, Station], published: set[str]) -> list[list[Any]]:
    """The shell's static site table (``docs/nexrad.md`` §7): every station
    that publishes, as the window manifest's rows, by id. A site that
    publishes but is missing from the station table is left out, as a
    window would refuse it."""
    return [
        [site, station.icao, round(station.latitude, 6), round(station.longitude, 6), round(station.height_m, 1)]
        for site, station in sorted(stations.items())
        if site in published
    ]


def load_stations(raw_root: Path, *, fetch: bool = True) -> dict[str, Station]:
    path = fetch_stations(raw_root) if fetch else raw_root / "nexrad" / "nexrad-stations.txt"
    return parse_stations(path.read_text(encoding="utf-8"))


def catch_up(
    moment: datetime,
    *,
    raw_root: Path,
    output_root: Path,
    stations: dict[str, Station],
    sites: list[str],
    previous: dict[str, Any] | None,
    window_seconds: int = WINDOW_SECONDS,
    fetch: bool = True,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every round from the one after the window's newest — or from the
    window's start, on a first build — up to ``moment``, each one five
    minutes of sweeps. A schedule that ran late, or not at all for an hour,
    fills its gap with rounds of the ordinary size rather than one round
    holding everything since."""
    if previous is not None:
        last = datetime.fromisoformat(previous["issued"])
        first = max(last, moment - timedelta(seconds=window_seconds)) + timedelta(minutes=ROUND_MINUTES)
    else:
        first = moment - timedelta(seconds=window_seconds) + timedelta(minutes=ROUND_MINUTES)
    reports: list[dict[str, Any]] = []
    round_time = first
    while round_time <= moment:
        report = build_round(
            round_time,
            raw_root=raw_root,
            output_root=output_root,
            stations=stations,
            sites=sites,
            previous=previous,
            window_seconds=window_seconds,
            fetch=fetch,
            now=now,
        )
        reports.append(report)
        previous = report["window"]
        round_time += timedelta(minutes=ROUND_MINUTES)
    return reports


def replay(
    *,
    start: datetime,
    end: datetime,
    sites: list[str],
    raw_root: Path,
    output_root: Path,
    fetch: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replay live rounds over ``(start, end]`` into ``output_root`` with a
    window as long as the interval, a round store per round, returning the
    last round's report: the live layout over a past interval."""
    if start.minute % ROUND_MINUTES or end.minute % ROUND_MINUTES or end <= start:
        raise NexradProductError("a replay runs between two round minutes")
    stations = load_stations(raw_root, fetch=fetch)
    window_seconds = int((end - start).total_seconds())
    previous: dict[str, Any] | None = None
    moment = start + timedelta(minutes=ROUND_MINUTES)
    report: dict[str, Any] = {}
    while moment <= end:
        report = build_round(
            moment,
            raw_root=raw_root,
            output_root=output_root,
            stations=stations,
            sites=sites,
            previous=previous,
            window_seconds=window_seconds,
            first_start=start,
            fetch=fetch,
            now=now,
        )
        previous = report["window"]
        moment += timedelta(minutes=ROUND_MINUTES)
    return report


def build_case(
    *,
    start: datetime,
    end: datetime,
    sites: list[str],
    raw_root: Path,
    output_root: Path,
    fetch: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a case over ``(start, end]`` into ``output_root``: one window
    store per product and the window manifest beside them. Its rounds are
    the ones a replay would build — every sweep in the first round at or
    after its start — with nothing late to wait for, since the interval is
    past."""
    if start.minute % ROUND_MINUTES or end.minute % ROUND_MINUTES or end <= start:
        raise NexradProductError("a case runs between two round minutes")
    now = now or datetime.now(UTC)
    stations = load_stations(raw_root, fetch=fetch)
    rounds: list[datetime] = []
    moment = start + timedelta(minutes=ROUND_MINUTES)
    while moment <= end:
        rounds.append(moment)
        moment += timedelta(minutes=ROUND_MINUTES)

    held = [_Round(moment, WINDOW_STORE_PATH) for moment in rounds]
    sources: list[dict[str, Any]] = []
    stores: dict[str, dict[str, Any]] = {}
    for product in PRODUCTS:
        per_site: list[SiteSweeps] = []
        reports = 0
        failed_total: list[str] = []
        error: str | None = None
        for site in sorted(sites):
            try:
                keys = list_keys(site, product, start, end) if fetch else _keys_on_disk(raw_root, site, product, start, end)
                if fetch:
                    _fetched, failed = download(raw_root, keys)
                    failed_total += failed
            except XueError as exc:
                error = f"{site}: {exc}"
                LOG.warning("nexrad %s: %s", product, error)
                continue
            sweeps, failed = _read_sweeps(raw_root, keys)
            failed_total += [key for key in failed if key not in failed_total]
            sweeps = [sweep for sweep in sweeps if start < sweep.scan_time <= end]
            if not sweeps:
                continue
            station = stations.get(site)
            per_site.append(
                SiteSweeps(
                    site=site,
                    latitude=sweeps[0].site_latitude,
                    longitude=sweeps[0].site_longitude,
                    height_m=sweeps[0].site_height_m if station is None else station.height_m,
                    sweeps=sweeps,
                )
            )
            reports += len(sweeps)
        status: dict[str, Any] = {"id": f"unidata-{product}", "ok": error is None or reports > 0, "reports": reports}
        if failed_total:
            status["missing"] = len(failed_total)
        if error is not None:
            status["error"] = error
        if not status["ok"] and "error" not in status:
            status["error"] = "no sweep read"
        sources.append(status)
        if not per_site:
            continue
        report = write_window_store(output_root / f"{product}.zarr", product=product, rounds=rounds, sites=per_site)
        stores[product] = {"sites": len(per_site), "shardBytes": report.shard_bytes, "depth": report.depth}
        times = {entry.site: [int(sweep.scan_time.timestamp()) for sweep in entry.sweeps] for entry in per_site}
        for entry, chunks in zip(held, report.rounds):
            if not chunks:
                continue
            floor = entry.round - timedelta(minutes=ROUND_MINUTES)
            entry.products[product] = {
                "group": {"byteLength": report.group_bytes, "crc32": report.group_crc32},
                "shard": {"byteLength": report.shard_bytes, "crc32": report.shard_crc32},
                "depth": report.depth,
                "chunks": chunks,
                "scans": {
                    site: [time for time in times[site] if floor.timestamp() < time <= entry.round.timestamp()]
                    for site, *_ in chunks
                },
            }

    window_seconds = int((end - start).total_seconds())
    window = _window_payload(end, [entry for entry in held if entry.products], stations, window_seconds, sources, now)
    validate_window(window)
    window_bytes = encode_json(window)
    write_bytes_atomic(output_root / WINDOW_FILENAME, window_bytes)
    return {"window": window, "windowBytes": window_bytes, "stores": stores, "sources": sources}
