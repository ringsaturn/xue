"""The airport pages' share cards (docs/airport.md §8): one 1200 × 630 PNG
per airport in the pages' checked-in list, drawn with the storm pictures'
map and look (`xuebuild/tc/images.py`).

Layout under the data root::

    airport-images/index.json                  the one mutable name
    airport-images/<ICAO>/card-<crc32>.png     an airport's share card

A file's name carries its CRC32, so a redrawn card is a new key and none is
overwritten. The index records what each card was drawn from (``source``:
the style and the CRC32 of the airport's own fields and of the listed
airports inside its frame), so a run draws only what changed. Files the
index no longer names are deleted by ``prune-r2-airport-images`` once they
are old enough that no page or cached card still asks for them.

matplotlib and Pillow come from the ``tcimages`` dependency group and are
imported only when something is drawn.
"""

from __future__ import annotations

import json
import math
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DownloadError, XueError
from ..fetch import _http_error_code, get_ok
from ..tc.images import FONT, HEIGHT, PALETTES, USER_AGENT, WIDTH, Painter, encode

INDEX_PRODUCT = "airport-images"
# Bump to redraw every card after a change to how they look.
STYLE = 1
PATH_RE = re.compile(r"^[A-Z0-9]{4}/card-[0-9a-f]{8}\.png$")
CREDIT = "xue.ringsaturn.me · Natural Earth"
TAGLINE = "Live METAR and TAF · multi-model forecast"
# The map's half height in degrees of latitude; the width follows the card.
HALF_HEIGHT = 9.0
# The airport sits a quarter of the half width right of centre, clear of the
# title on the left.
OFFSET = 0.25
MARK = "#5ebaff"


# --- what is drawn -------------------------------------------------------------------


def load_list(path: Path) -> list[dict[str, Any]]:
    """The pages' list (`web/tooling/airports.json`), which the page build
    validates; here only what a card reads is required."""
    data = json.loads(path.read_text())
    airports = data.get("airports") if isinstance(data, dict) else None
    if not isinstance(airports, list) or not airports:
        raise XueError(f"{path}: no airports")
    for airport in airports:
        if not isinstance(airport, dict) or not isinstance(airport.get("icao"), str) or not re.fullmatch(r"[A-Z0-9]{4}", airport["icao"]):
            raise XueError(f"{path}: an airport without a valid icao")
        for key in ("lat", "lon"):
            if not isinstance(airport.get(key), (int, float)):
                raise XueError(f"{path}: {airport['icao']}.{key} is not a number")
        for key in ("name", "city"):
            if not isinstance(airport.get(key), str) or not airport[key].strip():
                raise XueError(f"{path}: {airport['icao']}.{key} is missing")
    return airports


def frame(airport: dict[str, Any]) -> tuple[float, float, float, float, float]:
    """(west, east, south, north, squash) of an airport's card, the longitudes
    unwrapped around the airport's own, the latitudes kept inside ±90."""
    squash = math.cos(math.radians(airport["lat"]))
    half_width = HALF_HEIGHT * WIDTH / HEIGHT / squash
    centre = airport["lon"] - half_width * OFFSET
    south = airport["lat"] - HALF_HEIGHT * 0.95
    north = airport["lat"] + HALF_HEIGHT * 1.05
    if north > 90:
        south, north = south - (north - 90), 90.0
    if south < -90:
        south, north = -90.0, north + (-90 - south)
    return centre - half_width, centre + half_width, south, north, squash


def nearest_copy(lon: float, anchor: float) -> float:
    return lon + 360 * round((anchor - lon) / 360)


def neighbours(airport: dict[str, Any], airports: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """The other listed airports inside the card's frame, as (lon, lat) with
    the longitude on the frame's side of the antimeridian, sorted."""
    west, east, south, north, _ = frame(airport)
    result = []
    for other in airports:
        if other["icao"] == airport["icao"]:
            continue
        lon = nearest_copy(other["lon"], airport["lon"])
        if west < lon < east and south < other["lat"] < north:
            result.append((round(lon, 3), round(other["lat"], 3)))
    return sorted(result)


def card_label(airport: dict[str, Any]) -> tuple[str, str]:
    codes = " · ".join(code for code in (airport["icao"], airport.get("iata")) if code)
    return airport["name"], f"{codes} · {airport['city']}"


@dataclass
class Subject:
    """One airport's card and what it is drawn from."""

    icao: str
    source: str
    airport: dict[str, Any]
    others: list[tuple[float, float]] = field(default_factory=list)


def subject_source(airport: dict[str, Any], others: list[tuple[float, float]]) -> str:
    drawn = {
        "airport": [airport["icao"], airport.get("iata"), airport["name"], airport["city"], airport["lat"], airport["lon"]],
        "others": others,
    }
    body = json.dumps(drawn, separators=(",", ":"), ensure_ascii=False).encode()
    return f"{STYLE}:{zlib.crc32(body):08x}"


def subjects(airports: list[dict[str, Any]]) -> list[Subject]:
    result = []
    for airport in sorted(airports, key=lambda item: item["icao"]):
        others = neighbours(airport, airports)
        result.append(Subject(airport["icao"], subject_source(airport, others), airport, others))
    return result


def file_name(data: bytes) -> str:
    return f"card-{zlib.crc32(data):08x}.png"


def entry_valid(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("card"), dict)
        and isinstance(entry["card"].get("path"), str)
        and PATH_RE.match(entry["card"]["path"]) is not None
    )


def plan(live: dict[str, Any] | None, wanted: list[Subject], force: bool = False) -> tuple[list[Subject], dict[str, Any]]:
    """What to draw, and the entries of the live index to keep as they are.
    An airport no longer listed is left out of both."""
    airports = (live or {}).get("airports") or {}
    kept: dict[str, Any] = {}
    draw: list[Subject] = []
    for subject in wanted:
        entry = airports.get(subject.icao)
        if not force and entry_valid(entry) and entry.get("source") == subject.source:
            kept[subject.icao] = entry
        else:
            draw.append(subject)
    return draw, kept


def build_index(entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": INDEX_PRODUCT,
        "style": STYLE,
        "width": WIDTH,
        "height": HEIGHT,
        "airports": dict(sorted(entries.items())),
    }


Draw = Callable[[Subject], bytes]


def run(airports: list[dict[str, Any]], live: dict[str, Any] | None, output_dir: Path, draw: Draw, *, force: bool = False) -> dict[str, Any]:
    """Draw what changed into ``output_dir/airport-images/`` and write the index.

    The report's ``written`` lists the new files, then ``index.json`` when it
    changed: the upload order, the index last."""
    root = output_dir / INDEX_PRODUCT
    todo, entries = plan(live, subjects(airports), force)
    written: list[str] = []
    for subject in todo:
        data = draw(subject)
        relative = f"{subject.icao}/{file_name(data)}"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(f"{INDEX_PRODUCT}/{relative}")
        entries[subject.icao] = {"source": subject.source, "card": {"path": relative, "byteLength": len(data)}}
    index = build_index(entries)
    if index != live:
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.json").write_text(json.dumps(index, separators=(",", ":"), ensure_ascii=False) + "\n")
        written.append(f"{INDEX_PRODUCT}/index.json")
    return {"airports": len(index["airports"]), "drawn": [s.icao for s in todo], "written": written}


# --- drawing -------------------------------------------------------------------------


class AirportPainter(Painter):
    """The storm pictures' painter, with an airport's card."""

    def card(self, subject: Subject) -> Any:
        palette = PALETTES["dark"]
        airport = subject.airport
        west, east, south, north, squash = frame(airport)
        fig, ax = self._figure(palette, [0, 0, 1, 1])
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        ax.set_aspect(1 / squash)
        self._land(ax, palette, west, east, 0.6)
        step = 5
        for lon in range(math.floor(west / step) * step, math.floor(east) + 1, step):
            ax.axvline(lon, color=palette["grid"], linewidth=0.8, zorder=2)
        for lat in range(math.floor(south / step) * step, math.floor(north) + 1, step):
            ax.axhline(lat, color=palette["grid"], linewidth=0.8, zorder=2)
        if subject.others:
            xs, ys = zip(*subject.others)
            ax.scatter(xs, ys, s=14, c=palette["muted"], linewidths=0, zorder=4)
        x, y = airport["lon"], airport["lat"]
        ax.scatter([x], [y], s=420, facecolors="none", edgecolors=MARK, linewidths=2, zorder=5)
        ax.scatter([x], [y], s=60, c=palette["ink"], linewidths=0, zorder=6)
        ax.annotate(
            airport["icao"], (x, y), xytext=(16, 0), textcoords="offset points", color=palette["ink"],
            fontsize=17, fontweight="bold", va="center", family=FONT, zorder=6,
        )
        title, sub = card_label(airport)
        fig.text(0.03, 0.93, title, color=palette["ink"], fontsize=26, fontweight="bold", va="top", family=FONT)
        fig.text(0.03, 0.855, sub, color=palette["muted"], fontsize=15, va="top", family=FONT)
        fig.text(0.03, 0.05, TAGLINE, color=palette["ink"], fontsize=13, va="center", family=FONT)
        fig.text(0.97, 0.05, CREDIT, color=palette["muted"], fontsize=11, va="center", ha="right", family=FONT)
        return self._finish(fig, palette)


def painter_draw(painter: AirportPainter) -> Draw:
    return lambda subject: encode(painter.card(subject), "card")


def live_index(base_url: str) -> dict[str, Any] | None:
    """The published index, or None before the first."""
    url = base_url.rstrip("/") + f"/{INDEX_PRODUCT}/index.json"
    try:
        raw = get_ok(f"{url}?t={int(time.time())}", headers={"User-Agent": USER_AGENT}, timeout=60, attempts=3, max_elapsed=120)
    except DownloadError as exc:
        if _http_error_code(exc) == 404:
            return None
        raise
    index = json.loads(raw)
    if index.get("product") != INDEX_PRODUCT:
        raise XueError(f"{url} is not a {INDEX_PRODUCT} index")
    return index
