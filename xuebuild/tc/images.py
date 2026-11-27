"""The archived storms drawn on a map: the pictures of the static storm and
season pages (docs/tc.md §10).

The pictures are drawn apart from the archive and live beside it under
``tc-images/``, never inside ``tc/archive/``, which is a record and is never
pruned. Layout under the data root::

    tc-images/index.json                                  the one mutable name
    tc-images/<YYYY>/<STORM_ID>/{light,dark}-<crc32>.webp  a storm, for the page
    tc-images/<YYYY>/<STORM_ID>/card-<crc32>.png           its share card (dark)
    tc-images/<YYYY>/season/{light,dark}-<crc32>.webp      a season's tracks
    tc-images/<YYYY>/season/card-<crc32>.png

Every picture is 1200 × 630. A file's name carries its CRC32, so a redrawn
picture is a new key and none is overwritten; the index names the current
ones and records what each was drawn from (``source``: the style and the
CRC32 of the best-track line or season file), so a run draws only what
changed. Files the index no longer names are deleted by
``prune-r2-tc-images`` once they are old enough that no page or share
card still asks for them.

matplotlib and Pillow come from the ``tcimages`` dependency group and are
imported only when something is drawn.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DownloadError, XueError
from ..fetch import _http_error_code, get_ok
from .archive import KNOT, SSHS_FLOORS_KT

log = logging.getLogger(__name__)

INDEX_PRODUCT = "tc-images"
# Bump to redraw every picture after a change to how they look.
STYLE = 2
WIDTH, HEIGHT = 1200, 630
KINDS = ("light", "dark", "card")
SEASON_KEY = "season"
PATH_RE = re.compile(r"^\d{4}/(?:[A-Z]{2}\d{6}|season)/(?:light|dark|card)-[0-9a-f]{8}\.(?:webp|png)$")

# The storm map's Saffir-Simpson colours (`web/src/tc/intensity.ts`, held to
# it by tests/test_tc_images.py), TD to H5 over SSHS_FLOORS_KT.
SSHS_CODES = ("TD", "TS", "H1", "H2", "H3", "H4", "H5")
SSHS_COLORS = ("#5ebaff", "#00c8c0", "#f0e080", "#ffd24a", "#ffa530", "#ff7020", "#e8303a")

# The static pages' paper and the shell's dark ground (`web/src/basemap.ts`).
PALETTES: dict[str, dict[str, Any]] = {
    "light": {
        "ocean": "#f3efe6",
        "land": "#e2dccd",
        "coast": "#b5ac98",
        "grid": (0.106, 0.102, 0.09, 0.08),
        "ink": "#1b1a17",
        "muted": "#6e6a60",
        "halo": (0.106, 0.102, 0.09, 0.38),
        "halo_width": 1.1,
        "dot_edge": "#5c574d",
        "nodata": "#9a9a9a",
    },
    "dark": {
        "ocean": "#0b1826",
        "land": "#182c3d",
        "coast": "#2c4a62",
        "grid": (1.0, 1.0, 1.0, 0.07),
        "ink": "#ffffff",
        "muted": "#9fb0c0",
        "halo": (0.043, 0.094, 0.149, 0.55),
        "halo_width": 1.6,
        "dot_edge": "#0b1826",
        "nodata": "#7d8a96",
    },
}
# matplotlib's own font, so a picture looks the same on every machine.
FONT = "DejaVu Sans"
CREDIT = "xue.ringsaturn.me · best track IBTrACS · Natural Earth"

# Natural Earth 1:50m land (public domain), pinned to a release.
LAND_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_50m_land.geojson"
LAND_SHA256 = "e874b27a51d146452be360cafb3cc50c86001074a67d534113e6534682f9826b"
USER_AGENT = "Mozilla/5.0 (compatible; xue/0.1; +https://github.com/ringsaturn/xue)"
SYNOPTIC_HOURS = ("00:00", "06:00", "12:00", "18:00")


# --- what is drawn -------------------------------------------------------------------


def synoptic_track(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The static page's track: the ``usa`` best track first, then the others
    by name, six-hourly synoptic times only (`stormpages.ts::synopticTrack`)."""
    best = record.get("best") or {}
    for key in sorted(best, key=lambda name: (name != "usa", name)):
        points = [point for point in best[key]["points"] if point["time"][11:16] in SYNOPTIC_HOURS]
        if points:
            return points
    return []


def knots(vmax: float | None) -> int | None:
    return None if vmax is None else round(vmax / KNOT)


def grade(kt: int | None) -> int:
    """0 (TD) to 6 (H5) for a wind in knots, -1 for none."""
    if kt is None:
        return -1
    found = 0
    for index, floor in enumerate(SSHS_FLOORS_KT):
        if kt >= floor:
            found = index
    return found


def unwrap(longitudes: Iterable[float], anchor: float) -> list[float]:
    """Longitudes continuous across the antimeridian, near ``anchor``."""
    return [anchor + ((lon - anchor + 540) % 360) - 180 for lon in longitudes]


def storm_frame(track: list[dict[str, Any]]) -> tuple[float, float, float, float, float]:
    """(west, east, south, north, squash) fitting the track into the picture
    with room for the title, degrees of longitude shortened by the latitude."""
    xs = unwrap((point["lon"] for point in track), track[0]["lon"])
    ys = [point["lat"] for point in track]
    squash = math.cos(math.radians(max(-70.0, min(70.0, (max(ys) + min(ys)) / 2))))
    aspect = WIDTH / HEIGHT
    span_w = max((max(xs) - min(xs)) * squash, (max(ys) - min(ys)) * aspect, 8.0) * 1.36
    span_h = span_w / aspect
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2 + span_h * 0.06
    return cx - span_w / squash / 2, cx + span_w / squash / 2, cy - span_h / 2, cy + span_h / 2, squash


def storm_label(record: dict[str, Any], track: list[dict[str, Any]]) -> tuple[str, str]:
    name = (record.get("name") or "").title()
    title = f"{name} · {record['storm']}" if name else record["storm"]
    winds = [kt for kt in (knots(point["vmax"]) for point in track) if kt is not None]
    peak = max(winds) if winds else None
    sub = f"{track[0]['time'][:10]} → {track[-1]['time'][:10]}"
    if peak is not None:
        sub += f" · peak {peak} kt {SSHS_CODES[grade(peak)]}"
    return title, sub


def season_label(season: int, storms: int, southern: bool) -> tuple[str, str]:
    sub = f"{storms} numbered storms"
    if southern:
        sub += f" · Southern Hemisphere from July {season - 1} to June {season}"
    return f"{season} tropical cyclone season", sub


# --- drawing -------------------------------------------------------------------------


@dataclass
class Land:
    """Natural Earth land polygons as (west, east, points) rings."""

    rings: list[tuple[float, float, list[tuple[float, float]]]]

    @classmethod
    def load(cls, cache: Path) -> Land:
        path = cache / "ne_50m_land.geojson"
        if not path.exists():
            body = get_ok(LAND_URL, headers={"User-Agent": USER_AGENT}, timeout=120, attempts=3, max_elapsed=300)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != LAND_SHA256:
            path.unlink()
            raise XueError(f"{LAND_URL} does not match its pinned SHA-256")
        rings: list[tuple[float, float, list[tuple[float, float]]]] = []
        for feature in json.loads(body)["features"]:
            geometry = feature["geometry"]
            polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
            for polygon in polygons:
                outer = [(float(x), float(y)) for x, y, *_ in polygon[0]]
                xs = [x for x, _ in outer]
                rings.append((min(xs), max(xs), outer))
        return cls(rings)


class Painter:
    """matplotlib behind the two pictures; imported on first use."""

    def __init__(self, land: Land) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.collections import LineCollection, PatchCollection
        from matplotlib.patches import FancyBboxPatch, Polygon
        from PIL import Image

        self.plt, self.LineCollection, self.PatchCollection = plt, LineCollection, PatchCollection
        self.FancyBboxPatch, self.Polygon, self.Image = FancyBboxPatch, Polygon, Image
        self.land = land

    def _figure(self, palette: dict[str, Any], rect: list[float]) -> tuple[Any, Any]:
        fig = self.plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=100)
        fig.patch.set_facecolor(palette["ocean"])
        ax = fig.add_axes(rect)
        ax.axis("off")
        return fig, ax

    def _land(self, ax: Any, palette: dict[str, Any], west: float, east: float, linewidth: float) -> None:
        patches = []
        for shift in (-360.0, 0.0, 360.0):
            for lo, hi, ring in self.land.rings:
                if hi + shift < west - 1 or lo + shift > east + 1:
                    continue
                patches.append(self.Polygon([(x + shift, y) for x, y in ring], closed=True))
        ax.add_collection(
            self.PatchCollection(patches, facecolor=palette["land"], edgecolor=palette["coast"], linewidth=linewidth, zorder=1)
        )

    def _chrome(self, fig: Any, palette: dict[str, Any], title: str, sub: str) -> None:
        fig.text(0.03, 0.93, title, color=palette["ink"], fontsize=26, fontweight="bold", va="top", family=FONT)
        fig.text(0.03, 0.855, sub, color=palette["muted"], fontsize=15, va="top", family=FONT)
        x = 0.03
        for code, color in zip(SSHS_CODES, SSHS_COLORS):
            fig.patches.append(
                self.FancyBboxPatch(
                    (x, 0.045), 0.022, 0.018, boxstyle="round,pad=0,rounding_size=0.008", transform=fig.transFigure,
                    facecolor=color, edgecolor=palette["dot_edge"], linewidth=0.5,
                )
            )
            fig.text(x + 0.027, 0.054, code, color=palette["ink"], fontsize=12, va="center", family=FONT)
            x += 0.062
        fig.text(0.97, 0.05, CREDIT, color=palette["muted"], fontsize=11, va="center", ha="right", family=FONT)

    def _finish(self, fig: Any, palette: dict[str, Any]) -> Any:
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=100, facecolor=palette["ocean"], metadata={"Software": None})
        self.plt.close(fig)
        buffer.seek(0)
        return self.Image.open(buffer).convert("RGB")

    def storm(self, record: dict[str, Any], track: list[dict[str, Any]], kind: str) -> Any:
        palette = PALETTES[kind]
        west, east, south, north, squash = storm_frame(track)
        fig, ax = self._figure(palette, [0, 0, 1, 1])
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        ax.set_aspect(1 / squash)
        self._land(ax, palette, west, east, 0.6)
        step = 10 if east - west > 40 else 5
        for lon in range(math.floor(west / step) * step, math.floor(east) + 1, step):
            ax.axvline(lon, color=palette["grid"], linewidth=0.8, zorder=2)
        for lat in range(math.floor(south / step) * step, math.floor(north) + 1, step):
            ax.axhline(lat, color=palette["grid"], linewidth=0.8, zorder=2)
        xs = unwrap((point["lon"] for point in track), track[0]["lon"])
        ys = [point["lat"] for point in track]
        colors = [self._color(palette, knots(point["vmax"])) for point in track]
        segments = [[(xs[i], ys[i]), (xs[i + 1], ys[i + 1])] for i in range(len(xs) - 1)]
        ax.add_collection(
            self.LineCollection(segments, colors=[palette["halo"]], linewidths=4.2 + palette["halo_width"] * 2, capstyle="round", zorder=3)
        )
        ax.add_collection(self.LineCollection(segments, colors=colors[:-1], linewidths=4.2, capstyle="round", zorder=4))
        ax.scatter(xs, ys, s=20, c=colors, edgecolors=palette["dot_edge"], linewidths=0.7, zorder=5)
        self._chrome(fig, palette, *storm_label(record, track))
        return self._finish(fig, palette)

    def season(self, season: int, records: list[dict[str, Any]], kind: str) -> Any:
        palette = PALETTES[kind]
        # A Pacific-centred plate carrée band from 50°S to 62°N, centred under the title.
        west, east, south, north = 20.0, 380.0, -50.0, 62.0
        height = (north - south) / (east - west) * WIDTH / HEIGHT
        fig, ax = self._figure(palette, [0, 0.47 - height / 2, 1, height])
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        self._land(ax, palette, west, east, 0.4)
        for lat in (-30, 0, 30):
            ax.axhline(lat, color=palette["grid"], linewidth=0.8, zorder=2)
        drawn = []
        for record in records:
            track = synoptic_track(record)
            if len(track) >= 2:
                drawn.append((max((knots(p["vmax"]) or 0) for p in track), record["storm"], track))
        # The strongest on top.
        for _, _, track in sorted(drawn, key=lambda item: (item[0], item[1])):
            xs = [point["lon"] % 360 for point in track]
            xs = [x + 360 if x < west else x for x in xs]
            segments, colors = [], []
            for i in range(len(xs) - 1):
                if abs(xs[i + 1] - xs[i]) > 180:
                    continue
                segments.append([(xs[i], track[i]["lat"]), (xs[i + 1], track[i + 1]["lat"])])
                colors.append(self._color(palette, knots(track[i]["vmax"])))
            ax.add_collection(self.LineCollection(segments, colors=[palette["halo"]], linewidths=2.6, capstyle="round", zorder=3))
            ax.add_collection(self.LineCollection(segments, colors=colors, linewidths=1.6, capstyle="round", zorder=4))
        southern = any(record["storm"].startswith("SH") for record in records)
        self._chrome(fig, palette, *season_label(season, len(drawn), southern))
        return self._finish(fig, palette)

    @staticmethod
    def _color(palette: dict[str, Any], kt: int | None) -> str:
        index = grade(kt)
        return palette["nodata"] if index < 0 else SSHS_COLORS[index]


def encode(image: Any, kind: str) -> bytes:
    buffer = io.BytesIO()
    if kind == "card":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="WEBP", quality=90, method=6)
    return buffer.getvalue()


# --- the run -------------------------------------------------------------------------


@dataclass
class Subject:
    """One picture set: a storm, or a season (``key`` = ``season``)."""

    season: int
    key: str
    source: str
    records: list[dict[str, Any]] = field(default_factory=list)


def storm_source(line: bytes) -> str:
    return f"{STYLE}:{zlib.crc32(line):08x}"


def season_source(best_crc32: str) -> str:
    return f"{STYLE}:{best_crc32}"


def file_name(kind: str, data: bytes) -> str:
    return f"{kind}-{zlib.crc32(data):08x}.{'png' if kind == 'card' else 'webp'}"


def subjects(seasons: Iterable[tuple[int, str, list[bytes]]]) -> list[Subject]:
    """Every storm of the given seasons, and each season, with the source its
    picture is drawn from. ``seasons`` holds (season, best.jsonl CRC32, its lines)."""
    result: list[Subject] = []
    for season, best_crc32, lines in seasons:
        lines = [line for line in lines if line.strip()]
        records = [json.loads(line) for line in lines]
        for line, record in zip(lines, records):
            # A track the page cannot draw gets no picture either.
            if len(synoptic_track(record)) >= 2:
                result.append(Subject(season, record["storm"], storm_source(line), [record]))
        result.append(Subject(season, SEASON_KEY, season_source(best_crc32), records))
    return result


def entry_valid(entry: object) -> bool:
    return isinstance(entry, dict) and all(
        isinstance(entry.get(kind), dict) and isinstance(entry[kind].get("path"), str) and PATH_RE.match(entry[kind]["path"])
        for kind in KINDS
    )


def plan(live: dict[str, Any] | None, wanted: list[Subject], force: bool = False) -> tuple[list[Subject], dict[str, Any]]:
    """What to draw, and the entries of the live index to keep as they are."""
    storms = (live or {}).get("storms") or {}
    seasons = (live or {}).get("seasons") or {}
    kept: dict[str, Any] = {"storms": {}, "seasons": {}}
    draw: list[Subject] = []
    for subject in wanted:
        if subject.key == SEASON_KEY:
            entry, bucket, key = seasons.get(str(subject.season)), "seasons", str(subject.season)
        else:
            entry, bucket, key = storms.get(subject.key), "storms", subject.key
        if not force and entry_valid(entry) and entry.get("source") == subject.source:
            kept[bucket][key] = entry
        else:
            draw.append(subject)
    return draw, kept


def build_index(entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": INDEX_PRODUCT,
        "style": STYLE,
        "width": WIDTH,
        "height": HEIGHT,
        "storms": dict(sorted(entries["storms"].items())),
        "seasons": dict(sorted(entries["seasons"].items())),
    }


Draw = Callable[[Subject, str], bytes]


def run(
    seasons: list[tuple[int, str, list[bytes]]],
    live: dict[str, Any] | None,
    output_dir: Path,
    draw: Draw,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Draw what changed into ``output_dir/tc-images/`` and write the index.

    The report's ``written`` lists the new files, then ``index.json`` when it
    changed: the upload order, the index last."""
    root = output_dir / "tc-images"
    todo, entries = plan(live, subjects(seasons), force)
    written: list[str] = []
    for subject in todo:
        folder = f"{subject.season:04d}/{subject.key}"
        entry: dict[str, Any] = {"season": subject.season, "source": subject.source}
        for kind in KINDS:
            data = draw(subject, kind)
            relative = f"{folder}/{file_name(kind, data)}"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written.append(f"tc-images/{relative}")
            entry[kind] = {"path": relative, "byteLength": len(data)}
        if subject.key == SEASON_KEY:
            entries["seasons"][str(subject.season)] = entry
        else:
            entries["storms"][subject.key] = entry
    index = build_index(entries)
    if index != live:
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.json").write_text(json.dumps(index, separators=(",", ":"), ensure_ascii=False) + "\n")
        written.append("tc-images/index.json")
    return {
        "storms": len(index["storms"]),
        "seasons": len(index["seasons"]),
        "drawn": [f"{s.season}/{s.key}" for s in todo],
        "written": written,
    }


def painter_draw(painter: Painter) -> Draw:
    cache: dict[tuple[int, str], dict[str, Any]] = {}

    def draw(subject: Subject, kind: str) -> bytes:
        # The card is the dark picture as PNG: draw each look once.
        look = "dark" if kind == "card" else kind
        images = cache.setdefault((subject.season, subject.key), {})
        if look not in images:
            if subject.key == SEASON_KEY:
                images[look] = painter.season(subject.season, subject.records, look)
            else:
                record = subject.records[0]
                images[look] = painter.storm(record, synoptic_track(record), look)
        data = encode(images[look], kind)
        if kind == "card":
            cache.pop((subject.season, subject.key), None)
        return data

    return draw


def _get_json(url: str) -> Any:
    return json.loads(get_ok(url, headers={"User-Agent": USER_AGENT}, timeout=60, attempts=3, max_elapsed=120))


def published_seasons(base_url: str) -> list[tuple[int, str, list[bytes]]]:
    """The seasons that have static pages (some storm with archived
    forecasts, `archive.ts::seasonHasForecasts`), with their best tracks."""
    root = base_url.rstrip("/") + "/tc/archive/"
    catalog = _get_json(root + "catalog.json")
    result = []
    for entry in catalog["seasons"]:
        index = _get_json(f"{root}{entry['path']}?v={entry['crc32']}")
        if not any(storm.get("months") for storm in index["storms"]):
            continue
        best = index["best"]
        body = get_ok(
            f"{root}{index['season']:04d}/{best['path']}?v={best['crc32']}",
            headers={"User-Agent": USER_AGENT},
            timeout=120,
            attempts=3,
            max_elapsed=300,
        )
        if f"{zlib.crc32(body):08x}" != best["crc32"]:
            raise XueError(f"season {index['season']} best.jsonl does not match its CRC32")
        result.append((int(index["season"]), best["crc32"], body.splitlines()))
    return result


def live_index(base_url: str) -> dict[str, Any] | None:
    """The published index, or None before the first."""
    url = base_url.rstrip("/") + "/tc-images/index.json"
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
