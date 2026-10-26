"""Historical showcase cases: past runs cropped to one weather event.

A case is a small, immutable slice of one archived forecast run — a bounding
box, a forecast-hour range, and the subset of variables the event is about —
built with exactly the same encoder as the live feed. The output is an
ordinary schema v5 manifest plus its bundles under
``<data root>/showcase/<case id>/``, and a ``case.json`` sidecar carrying the
catalog entry. :func:`write_catalog` collects those sidecars into the mutable
``showcase.json`` catalog the showcase page lists, so rebuilding one case
never requires rebuilding the rest.

Delivery mirrors the live feed's two layers: the catalog is the only mutable
object, and every manifest and bundle it names is immutable and addressed
with ``?v=<crc32>``.

The archives reach back far enough for this to be interesting but not
forever — the NOAA bucket holds GFS and sflux from about 2021-01 (with a
directory-layout change in 2021-03, see :data:`xue.fetch.ATMOS_SUBDIRECTORY_FROM`)
and the ECMWF open data mirrors from about 2024-02. A case naming a run
older than its source published simply fails to fetch.

A case on an observation source is the same object built from a different
input, and comes in two shapes. A *fetched* observation
(``SourceSpec.fetched``: the NOAA MRMS mosaic, the CMA radar mosaic out of
its archive) is like a forecast case: it names a ``run`` — the first hour
of its window, which the MRMS bucket holds from 2020-10-14 and the CMA
archive from 2026-09-06 — and ``hours`` is the window's length rather than
a point on a published axis, since the frames come every few minutes with
gaps wherever the archive skipped one. A case on a series-file observation
(``SourceSpec.series_file``) may instead name a local ``dataset`` file the
source's tool wrote, and its axis is whatever times that file carries;
nothing fetches it, so such a case is only rebuildable by someone who has
the dataset — how the CMA cases were built before the archive. The built
output is an ordinary case like any other.

A forecast source with no live feed (the WOOF nest: ``SourceSpec.fetched``
false on a source that is not an observation) takes the same shape as the
file case: it names the ``dataset`` directory the ``xue wrf-series`` tool
wrote and no ``run``, and the cycle comes out of the series' own time axis.
Its ``hours`` is still a point on the source's published axis, counted from
the source's first hour.

A case may also carry a ``view``: the camera the shell opens the case on
(center, zoom, pitch, bearing, terrain exaggeration) and the volume bundle
it opens drawn over the field, carried onto the catalog row verbatim. A
shell that does not know the block opens the case on its bounding box as
before. Its ``markers`` are labelled points of context inside the box (a
crossing, an ignition), each with a note and its sources, carried onto the
row the same way; a shell that does not know them draws none. Its
``river`` names the river index built into its directory
(``docs/river.md``): reach streamflow, gauges and flood extents the shell
plays beside the field, and ignores when it does not know the block. Its
``context`` names a GeoJSON file of reference lines and areas built into its
directory (:func:`read_context`): borders, a protected area's boundary,
rivers, roads and the routes a case's story runs along, drawn under the
markers; a shell that does not know the block draws none.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import crc32_hex
from .binconvert import VOLUME_BUNDLES, bundle_input_ids, published_bundle_ids
from .encoder import convert_bin
from .errors import DownloadError, ManifestError, XueError
from .fetch import parse_run
from .localrun import LocalRunError, build_local_run
from .manifest import iso_z, validate_bin_manifest
from .sources import SourceSpec, source_spec
from .stac import write_showcase_documents

LOG = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 1

CATALOG_FILENAME = "showcase.json"
"""Mutable catalog at the data root, next to the per-model live pointers."""

SHOWCASE_DIRECTORY = "showcase"
"""Directory under the data root holding one subdirectory per case."""

CASE_SIDECAR = "case.json"
"""Per-case catalog entry, written next to the case's manifest."""

LOCALES = ("zh", "zh-Hant", "en", "ja", "ko", "de", "fr", "es", "pt", "tr", "ru")
"""Locales every human-facing case string must provide: the UI's eleven
(`web/src/i18n.ts`, held to this tuple by ``tests/fixtures/locales.json``).
A case is published content, and a shell that speaks eleven languages must
not show a card in English to nine of them. The frontend still falls back
onto English (Traditional Chinese onto Simplified first) for a catalog row
that predates a locale, and a definition may carry more — any extra locale
key is kept and published as it stands."""

TIMELINES = ("daily",)
"""The axis readings a case may name (``CaseSpec.timeline``)."""

REQUIRED_LOCALES = ("zh", "en")
"""The two a catalog row is refused without: the languages every case was
first authored in, which is what a row already on the bucket is held to."""

_LOCALE_TAG = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
"""Shape of an extra locale key: a language subtag, optionally with a script
or region. Matching the tags the frontend's own detection normalizes."""

OBSERVATION_ROOT_ENV = "XUE_OBSERVATION_ROOT"
"""Environment variable holding the root a case's ``dataset`` path is
resolved against."""

DEFAULT_OBSERVATION_ROOT = Path("data/observations")
"""Where local datasets live by default, relative to the working directory.
These files are not published anywhere, so a case built from one is only
rebuildable by someone who has it."""

_ID_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class ShowcaseError(XueError):
    """A case definition or a built case is not usable."""


@dataclass(frozen=True)
class CaseSpec:
    """One case definition, as checked into ``showcase/cases/<id>.json``."""

    id: str
    title: dict[str, str]
    summary: dict[str, str]
    model: str
    run: str
    """The archived cycle to fetch — on a fetched observation, the first
    hour of the window — and empty on a case built from a dataset, where
    the series says when it starts."""
    dataset: str
    """Cases built from a local dataset only: the NetCDF series file, or
    the directory holding one series per variable, resolved against
    :data:`OBSERVATION_ROOT_ENV` (default :data:`DEFAULT_OBSERVATION_ROOT`)
    when it is not absolute."""
    hours: int
    """Last hour of the case's axis: on a forecast a point on the published
    axis, on a file observation counted from its first frame, on a fetched
    observation the length of the window."""
    bbox: tuple[float, float, float, float]
    variables: tuple[str, ...]
    default_variable: str
    event_time: str | None
    tags: tuple[str, ...]
    credit: str | None
    profile: str
    timeline: str | None = None
    """How the viewer reads the case's axis: absent, by its dataset (a
    forecast's leads, an observation window's clock); ``daily``, as a record
    of days, each frame named by its UTC date."""
    radar: dict[str, str] | None = None
    """The single-site radar overlay the case plays over its own axis
    (``docs/nexrad.md``): ``window``, the window manifest of a
    ``xue nexrad-case`` build, relative to the case's directory, and the
    ``defaultSite`` and ``defaultProduct`` the viewer opens on."""
    fire: dict[str, str] | None = None
    """The active fire detections the case plays over its own axis
    (``docs/fire.md``): ``window``, the ``index.json`` of a ``xue
    fire-build`` round written into the case's directory, relative to it."""
    river: dict[str, str] | None = None
    """The river flow the case plays beside its field (``docs/river.md``):
    ``index``, the ``river/index.json`` a ``python -m xuebuild.river
    build`` wrote into the case's directory, relative to it; optionally
    ``color``, one of the case's bundles, whose palette and codebook then
    colour the reaches by their flow in place of the ratio classes."""
    imagery: dict[str, Any] | None = None
    """The NASA GIBS daily picture the shell offers in place of the case's
    field and opens on (``web/src/gibs.ts``): ``layer``, one of
    :data:`GIBS_LAYERS`, and optionally ``days``, UTC dates mapped to the
    layer whose platform is drawn on that day instead. Nothing is built for
    it: the tiles come from GIBS."""
    wind: dict[str, str] | None = None
    """The wind the shell runs its particles and the fire marks' wind arrow
    off when the case ships none of its own: ``case``, the id of another
    case whose ``wind10m`` is read on this case's clock (a satellite window
    borrowing the reanalysis of the same event)."""
    context: dict[str, str] | None = None
    """The reference lines and areas the shell draws over the case
    (:func:`read_context`): ``file``, a GeoJSON file inside the case's
    directory, and ``credit``, the line naming where its geometry comes
    from, shown on the map's credit line while the case is open."""
    markers: tuple[dict[str, Any], ...] = ()
    """Labelled points of context the shell draws over the case
    (:func:`_markers_block`): a place or an event, each with a label and a
    note in every locale, an optional time and the sources it rests on.
    Carried onto the catalog row as given."""
    view: dict[str, Any] | None = None
    """The camera the shell opens the case on (:func:`_view_block`):
    ``center`` as ``[lon, lat]``, ``zoom``, and optionally ``pitch``,
    ``bearing``, ``terrain`` (a vertical exaggeration, or ``false`` for an
    explicitly flat view) and ``volume`` (one of the case's volume bundles,
    drawn over the field from the first frame). Carried onto the catalog
    row as given; a case without one opens on its bounding box."""

    @property
    def output_subdirectory(self) -> str:
        return f"{SHOWCASE_DIRECTORY}/{self.id}"

    @property
    def source(self) -> SourceSpec:
        return source_spec(self.model)

    @property
    def from_dataset(self) -> bool:
        """Whether the case is built from a local dataset rather than
        fetched: a series-file observation whose definition names a
        ``dataset`` (the CMA cases cut before the archive) or a forecast
        with no feed to fetch (the WOOF nest), never MRMS or a cycle on a
        bucket. :func:`parse_case` holds the definition to the source, so
        a dataset named is a dataset built from."""
        return bool(self.dataset)

    @property
    def dataset_path(self) -> Path:
        """The series file or directory this case is built from."""
        if not self.dataset:
            raise ShowcaseError(f"case {self.id} names no dataset")
        path = Path(self.dataset).expanduser()
        if path.is_absolute():
            return path
        root = Path(os.environ.get(OBSERVATION_ROOT_ENV, DEFAULT_OBSERVATION_ROOT)).expanduser()
        return root / path


def _localized(
    value: object, label: str, case_id: str, required: tuple[str, ...] = LOCALES
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ShowcaseError(f"case {case_id}: {label} must be an object keyed by locale")
    missing = [locale for locale in required if not isinstance(value.get(locale), str) or not value[locale].strip()]
    if missing:
        raise ShowcaseError(f"case {case_id}: {label} is missing {', '.join(missing)}")
    # The required locales first, in the UI's order, then whatever else the
    # definition carries, so a case translated beyond the UI's set reaches
    # the catalog instead of being silently dropped here.
    text = {locale: value[locale].strip() for locale in required}
    for locale, item in value.items():
        if locale in text:
            continue
        if not isinstance(locale, str) or not _LOCALE_TAG.match(locale):
            raise ShowcaseError(f"case {case_id}: {label} has a key that is not a locale tag: {locale!r}")
        if not isinstance(item, str) or not item.strip():
            raise ShowcaseError(f"case {case_id}: {label} has an empty {locale}")
        text[locale] = item.strip()
    return text


def parse_case(payload: dict[str, Any], *, source_name: str = "<case>") -> CaseSpec:
    """Validate one case definition."""
    case_id = payload.get("id")
    if not isinstance(case_id, str) or not case_id or set(case_id) - _ID_CHARACTERS:
        raise ShowcaseError(f"{source_name}: id must be lowercase letters, digits and hyphens")
    model = payload.get("model")
    if not isinstance(model, str):
        raise ShowcaseError(f"case {case_id}: model must be a string")
    source = source_spec(model)

    # A fetched case names what to fetch: a forecast cycle, or the first
    # hour of a fetched observation's window. A source nothing fetches — a
    # file observation, or a forecast with no feed (the WOOF nest) — names
    # the dataset that already holds its series instead, and its start time
    # comes out of that series rather than out of the definition. A
    # series-file observation with an archive (the CMA mosaic) takes either.
    run = payload.get("run", "")
    dataset = payload.get("dataset", "")
    if not source.fetched and not source.series_file:
        raise ShowcaseError(f"case {case_id}: {source.manifest_model} is neither fetched nor read from a series")
    from_file = not source.fetched or (source.observation and source.series_file and bool(dataset))
    parsed_run = None
    if from_file:
        if run:
            raise ShowcaseError(f"case {case_id}: a case built from a dataset has no run to name")
        if not isinstance(dataset, str) or not dataset:
            raise ShowcaseError(
                f"case {case_id}: dataset must name the series file or directory to build "
                f"{source.manifest_model} from"
            )
    else:
        if dataset:
            raise ShowcaseError(
                f"case {case_id}: only a case read from a dataset names a dataset; {source.manifest_model} is fetched"
            )
        if not isinstance(run, str) or not run:
            what = "the window's first hour" if source.observation else "a UTC cycle"
            raise ShowcaseError(f"case {case_id}: run must be {what} in YYYYMMDDHH format")
        parsed_run = parse_run(run, source.id)

    hours = payload.get("hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        raise ShowcaseError(f"case {case_id}: hours must be a positive integer forecast hour")
    # A forecast hour must sit on the published axis; an observation's axis
    # is the file's or the window's own, so any whole hour is a legal
    # declaration until the frames are read.
    if not source.observation:
        try:
            source.forecast_hours(hours, cycle=parsed_run.time.hour if parsed_run else None)
        except DownloadError as exc:
            raise ShowcaseError(f"case {case_id}: {exc}") from exc

    bbox = payload.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(value, (int, float)) for value in bbox):
        raise ShowcaseError(f"case {case_id}: bbox must be [west, south, east, north] in degrees")

    variables = payload.get("variables")
    published = published_bundle_ids(source)
    if not isinstance(variables, list) or not variables or any(item not in published for item in variables):
        raise ShowcaseError(
            f"case {case_id}: variables must be a non-empty subset of {list(published)} for {source.manifest_model}"
        )
    # Manifest order, deduplicated.
    ordered = tuple(bundle_id for bundle_id in published if bundle_id in variables)
    # The run is keyed by forecast hour off its first variable, so a case
    # whose every input is absent from the analysis file has nothing to key on.
    if all(
        input_id in source.optional_at_analysis
        for bundle_id in ordered
        for input_id in bundle_input_ids(source, bundle_id)
    ):
        raise ShowcaseError(
            f"case {case_id}: {source.manifest_model} publishes no {ordered[0]} record at f000, so the case "
            "needs at least one more variable"
        )

    default_variable = payload.get("defaultVariable", ordered[0])
    if default_variable not in ordered:
        raise ShowcaseError(f"case {case_id}: defaultVariable must be one of {list(ordered)}")

    event_time = payload.get("eventTime")
    if event_time is not None:
        if not isinstance(event_time, str):
            raise ShowcaseError(f"case {case_id}: eventTime must be an ISO timestamp")
        try:
            datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ShowcaseError(f"case {case_id}: eventTime is not a valid ISO timestamp") from exc

    tags = payload.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ShowcaseError(f"case {case_id}: tags must be a list of strings")

    credit = payload.get("credit")
    if credit is not None and not isinstance(credit, str):
        raise ShowcaseError(f"case {case_id}: credit must be a string")

    timeline = payload.get("timeline")
    if timeline is not None and timeline not in TIMELINES:
        raise ShowcaseError(f"case {case_id}: timeline must be one of {', '.join(TIMELINES)}")

    profile = payload.get("profile", "quality")
    if profile not in ("quality", "balanced", "compact"):
        raise ShowcaseError(f"case {case_id}: profile must be quality, balanced or compact")

    radar = payload.get("radar")
    if radar is not None:
        radar = _radar_block(radar, case_id)

    fire = payload.get("fire")
    if fire is not None:
        fire = _fire_block(fire, case_id)

    river = payload.get("river")
    if river is not None:
        river = _river_block(river, case_id, ordered)

    imagery = payload.get("imagery")
    if imagery is not None:
        imagery = _imagery_block(imagery, case_id)

    view = payload.get("view")
    if view is not None:
        view = _view_block(view, case_id, ordered)

    wind = payload.get("wind")
    if wind is not None:
        wind = _wind_block(wind, case_id)

    context = payload.get("context")
    if context is not None:
        context = _context_block(context, case_id)

    bounds = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    markers = payload.get("markers")
    markers = _markers_block(markers, case_id, bounds) if markers is not None else []

    return CaseSpec(
        id=case_id,
        title=_localized(payload.get("title"), "title", case_id),
        summary=_localized(payload.get("summary"), "summary", case_id),
        model=model,
        run=run,
        dataset=dataset,
        hours=hours,
        bbox=bounds,
        variables=ordered,
        default_variable=default_variable,
        event_time=event_time,
        tags=tuple(tags),
        credit=credit,
        timeline=timeline,
        profile=profile,
        radar=radar,
        fire=fire,
        river=river,
        imagery=imagery,
        wind=wind,
        context=context,
        markers=tuple(markers),
        view=view,
    )


RADAR_PRODUCTS = ("n0b", "n0g")


def _radar_block(value: object, case_id: str) -> dict[str, str]:
    """A case's ``radar`` block: a window manifest inside the case's own
    directory, a three-letter site and one of the two products."""
    if not isinstance(value, dict) or set(value) - {"window", "defaultSite", "defaultProduct", "byteLength", "crc32"}:
        raise ShowcaseError(f"case {case_id}: radar must be {{window, defaultSite, defaultProduct}}")
    window = value.get("window")
    if (
        not isinstance(window, str)
        or not window.endswith("/index.json")
        or window.startswith("/")
        or ".." in window.split("/")
    ):
        raise ShowcaseError(f"case {case_id}: radar.window must be an index.json inside the case directory")
    site = value.get("defaultSite")
    if not isinstance(site, str) or not re.fullmatch(r"[A-Z0-9]{3}", site):
        raise ShowcaseError(f"case {case_id}: radar.defaultSite must be a three-character site id")
    product = value.get("defaultProduct")
    if product not in RADAR_PRODUCTS:
        raise ShowcaseError(f"case {case_id}: radar.defaultProduct must be one of {RADAR_PRODUCTS}")
    return {"window": window, "defaultSite": site, "defaultProduct": product}


def _fire_block(value: object, case_id: str) -> dict[str, str]:
    """A case's ``fire`` block: the index of a fire round inside the case's
    own directory."""
    if not isinstance(value, dict) or set(value) - {"window", "byteLength", "crc32"}:
        raise ShowcaseError(f"case {case_id}: fire must be {{window}}")
    window = value.get("window")
    if (
        not isinstance(window, str)
        or not window.endswith("/index.json")
        or window.startswith("/")
        or ".." in window.split("/")
    ):
        raise ShowcaseError(f"case {case_id}: fire.window must be an index.json inside the case directory")
    return {"window": window}


def _river_block(value: object, case_id: str, variables: Sequence[str]) -> dict[str, str]:
    """A case's ``river`` block: the river index inside the case's own
    directory (``docs/river.md``), and the bundle that colours its reaches."""
    if not isinstance(value, dict) or set(value) - {"index", "byteLength", "crc32", "color"}:
        raise ShowcaseError(f"case {case_id}: river must be {{index[, color]}}")
    index = value.get("index")
    if (
        not isinstance(index, str)
        or not index.endswith("/index.json")
        or index.startswith("/")
        or ".." in index.split("/")
    ):
        raise ShowcaseError(f"case {case_id}: river.index must be an index.json inside the case directory")
    color = value.get("color")
    if color is None:
        return {"index": index}
    if not isinstance(color, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", color):
        raise ShowcaseError(f"case {case_id}: river.color must be a bundle id")
    if color not in variables:
        raise ShowcaseError(f"case {case_id}: river.color names {color}, which is not among the case's variables")
    return {"index": index, "color": color}


GIBS_LAYERS = (
    "snpp",
    "noaa20",
    "noaa21",
    "terra",
    "aqua",
    "snpp-fc",
    "noaa20-fc",
    "noaa21-fc",
    "terra-721",
    "aqua-721",
)
"""The GIBS pictures the shell draws (``web/src/gibs.ts`` ``GIBS_LAYERS``),
by their ``?gibs=`` spelling."""


def _imagery_block(value: object, case_id: str) -> dict[str, Any]:
    """A case's ``imagery`` block: the GIBS picture it opens on, and the
    UTC days another platform stands in for it (GIBS serves a black picture
    for a day an orbiter has none)."""
    if (
        not isinstance(value, dict)
        or "layer" not in value
        or not set(value) <= {"layer", "days"}
        or value["layer"] not in GIBS_LAYERS
    ):
        raise ShowcaseError(f"case {case_id}: imagery must be {{layer, days?}}, layer one of {GIBS_LAYERS}")
    block: dict[str, Any] = {"layer": value["layer"]}
    if "days" in value:
        days = value["days"]
        if not isinstance(days, dict) or not days:
            raise ShowcaseError(f"case {case_id}: imagery days must map UTC dates to GIBS layers")
        for day, layer in days.items():
            if not isinstance(day, str) or not _is_utc_date(day) or layer not in GIBS_LAYERS:
                raise ShowcaseError(f"case {case_id}: imagery day {day!r} must be YYYY-MM-DD naming one of {GIBS_LAYERS}")
        block["days"] = dict(sorted(days.items()))
    return block


WIND_BUNDLE = "wind10m"
"""The bundle a case's ``wind`` block borrows from the case it names."""


def _wind_block(value: object, case_id: str) -> dict[str, str]:
    """A case's ``wind`` block: the other case whose ``wind10m`` it borrows.
    That the case exists and ships the bundle is checked with the catalog."""
    if not isinstance(value, dict) or set(value) != {"case"}:
        raise ShowcaseError(f"case {case_id}: wind must be {{case}}")
    other = value["case"]
    if not isinstance(other, str) or not other or set(other) - _ID_CHARACTERS:
        raise ShowcaseError(f"case {case_id}: wind.case must be a case id")
    if other == case_id:
        raise ShowcaseError(f"case {case_id}: wind.case names the case itself")
    return {"case": other}


def _is_utc_date(value: str) -> bool:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


CONTEXT_KINDS = ("border", "protected-area", "river", "stream", "road", "track", "route", "trace", "measure")
"""What a context feature is, which is all the shell styles it by: a
national border, a protected area's boundary, a river and a smaller
watercourse, a road and an unpaved track, the route a case's story follows,
a line read off imagery whose nature is not known, and a measured distance
between two points, labelled with it."""

MAX_CONTEXT_LABEL = 40
"""A feature's ``label`` is drawn along it: a distance, a short name."""

_CONTEXT_GEOMETRIES = {
    "LineString": 1,
    "MultiLineString": 2,
    "Polygon": 2,
    "MultiPolygon": 3,
}
"""Each geometry type the context file may hold and how many arrays deep
its positions sit."""

MAX_CONTEXT_BYTES = 4 * 1024 * 1024
"""A context file is read whole before the case draws it."""


def _context_block(value: object, case_id: str) -> dict[str, str]:
    """A case's ``context`` block: a GeoJSON file inside the case's own
    directory and the credit for its geometry."""
    if not isinstance(value, dict) or set(value) - {"file", "credit", "byteLength", "crc32"}:
        raise ShowcaseError(f"case {case_id}: context must be {{file, credit}}")
    file = value.get("file")
    if (
        not isinstance(file, str)
        or not file.endswith(".geojson")
        or file.startswith("/")
        or ".." in file.split("/")
    ):
        raise ShowcaseError(f"case {case_id}: context.file must be a .geojson inside the case directory")
    credit = value.get("credit")
    # One line on the shell's credit line.
    if not isinstance(credit, str) or not credit.strip() or len(credit) > 200 or not credit.isprintable():
        raise ShowcaseError(f"case {case_id}: context.credit must be a line of at most 200 characters")
    return {"file": file, "credit": credit.strip()}


def _context_positions(coordinates: object, depth: int) -> list[Sequence[float]]:
    """The positions of one geometry's ``coordinates``, an array of them
    nested ``depth`` arrays deep; raises ``ValueError`` on any other shape."""
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("empty or malformed coordinates")
    if depth == 1:
        return coordinates
    return [position for part in coordinates for position in _context_positions(part, depth - 1)]


def read_context(data: bytes, bbox: Sequence[float]) -> dict[str, Any]:
    """A context file, checked the way the shell reads it: a GeoJSON
    FeatureCollection of lines and polygons, each feature's ``kind`` one of
    :data:`CONTEXT_KINDS` and its optional ``label`` one short line, every position finite and inside the case's
    ``bbox``."""
    if len(data) > MAX_CONTEXT_BYTES:
        raise ValueError(f"is {len(data)} bytes, more than {MAX_CONTEXT_BYTES}")
    collection = json.loads(data)
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("is not a GeoJSON FeatureCollection")
    features = collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("holds no features")
    for index, feature in enumerate(features):
        where = f"feature {index}"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"{where} is not a Feature")
        properties = feature.get("properties")
        kind = properties.get("kind") if isinstance(properties, dict) else None
        if kind not in CONTEXT_KINDS:
            raise ValueError(f"{where}: properties.kind must be one of {CONTEXT_KINDS}")
        label = properties.get("label")
        if label is not None and (
            not isinstance(label, str) or not label.strip() or len(label) > MAX_CONTEXT_LABEL or not label.isprintable()
        ):
            raise ValueError(f"{where}: properties.label must be one line of at most {MAX_CONTEXT_LABEL} characters")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in _CONTEXT_GEOMETRIES:
            raise ValueError(f"{where}: geometry must be one of {tuple(_CONTEXT_GEOMETRIES)}")
        try:
            positions = _context_positions(geometry.get("coordinates"), _CONTEXT_GEOMETRIES[geometry["type"]])
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
        for position in positions:
            if (
                not isinstance(position, list)
                or len(position) != 2
                or any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in position)
                or not all(math.isfinite(number) for number in position)
            ):
                raise ValueError(f"{where}: a position must be [longitude, latitude]")
            if not _in_bounds(position[0], position[1], bbox):
                raise ValueError(f"{where}: {position} lies outside the case bbox")
    return collection


MARKER_KINDS = ("place", "event")
"""What a marker stands for: somewhere (a crossing, a town) or something
that happened there (an ignition), which may name when."""

MAX_MARKERS = 24
"""More than this is a layer of its own, not context for one case."""

_MARKER_KEYS = {"id", "kind", "lon", "lat", "time", "label", "note", "sources"}


def _in_bounds(lon: float, lat: float, bbox: Sequence[float]) -> bool:
    """Whether a point lies in ``[west, south, east, north]``, a box whose
    east is less than its west crossing the antimeridian."""
    west, south, east, north = bbox
    if not south <= lat <= north:
        return False
    return west <= lon <= east if west <= east else lon >= west or lon <= east


def _markers_block(
    value: object, case_id: str, bbox: Sequence[float], required: tuple[str, ...] = LOCALES
) -> list[dict[str, Any]]:
    """A case's ``markers``: labelled points inside its ``bbox``. Each has a
    unique slug ``id``, a ``kind`` (:data:`MARKER_KINDS`), ``lon``/``lat``,
    optionally a ``time`` (ISO), a ``label`` and a ``note`` held to the same
    locales as the title, and ``sources``, each a ``title`` and an https
    ``url``. The values pass through as written."""
    if not isinstance(value, list) or len(value) > MAX_MARKERS:
        raise ShowcaseError(f"case {case_id}: markers must be a list of at most {MAX_MARKERS}")
    markers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        where = f"markers[{index}]"
        if not isinstance(item, dict) or set(item) - _MARKER_KEYS:
            raise ShowcaseError(f"case {case_id}: {where} must be {{id, kind, lon, lat, time?, label, note, sources?}}")
        marker_id = item.get("id")
        if not isinstance(marker_id, str) or not marker_id or set(marker_id) - _ID_CHARACTERS:
            raise ShowcaseError(f"case {case_id}: {where}.id must be lowercase letters, digits and hyphens")
        if marker_id in seen:
            raise ShowcaseError(f"case {case_id}: marker id {marker_id} appears twice")
        seen.add(marker_id)
        where = f"marker {marker_id}"
        kind = item.get("kind")
        if kind not in MARKER_KINDS:
            raise ShowcaseError(f"case {case_id}: {where}.kind must be one of {MARKER_KINDS}")
        lon, lat = item.get("lon"), item.get("lat")
        for name, number in (("lon", lon), ("lat", lat)):
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ShowcaseError(f"case {case_id}: {where}.{name} must be a finite number")
        if not _in_bounds(lon, lat, bbox):
            raise ShowcaseError(f"case {case_id}: {where} at {lon}, {lat} lies outside the case bbox")
        marker: dict[str, Any] = {"id": marker_id, "kind": kind, "lon": lon, "lat": lat}
        if "time" in item:
            time = item["time"]
            try:
                if not isinstance(time, str):
                    raise ValueError
                parsed = datetime.fromisoformat(time.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ShowcaseError(f"case {case_id}: {where}.time must be an ISO timestamp") from exc
            # A time without a zone means a different instant to every viewer.
            if parsed.tzinfo is None:
                raise ShowcaseError(f"case {case_id}: {where}.time must name its zone (end it in Z)")
            marker["time"] = time
        marker["label"] = _localized(item.get("label"), f"{where}.label", case_id, required)
        marker["note"] = _localized(item.get("note"), f"{where}.note", case_id, required)
        sources = item.get("sources", [])
        if not isinstance(sources, list):
            raise ShowcaseError(f"case {case_id}: {where}.sources must be a list")
        checked = []
        for source in sources:
            if (
                not isinstance(source, dict)
                or set(source) != {"title", "url"}
                or not isinstance(source["title"], str)
                or not source["title"].strip()
                or not isinstance(source["url"], str)
                or not re.fullmatch(r"https://[^\s/]+\.[^\s]+", source["url"])
            ):
                raise ShowcaseError(f"case {case_id}: {where}.sources must each be {{title, url}} with an https url")
            checked.append({"title": source["title"].strip(), "url": source["url"]})
        if checked:
            marker["sources"] = checked
        markers.append(marker)
    return markers


_VIEW_KEYS = ("center", "zoom", "pitch", "bearing", "terrain", "volume")


def _view_number(value: object, name: str, case_id: str, low: float, high: float) -> float:
    """A finite number in ``[low, high]``; a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShowcaseError(f"case {case_id}: view.{name} must be a number between {low:g} and {high:g}")
    if not low <= value <= high:
        raise ShowcaseError(f"case {case_id}: view.{name} must be between {low:g} and {high:g}, not {value}")
    return value


def _view_block(value: object, case_id: str, variables: Sequence[str]) -> dict[str, Any]:
    """A case's ``view`` block, the camera the shell opens it on: a center
    and a zoom, optionally a pitch, a bearing and the terrain exaggeration
    (a positive number) or ``false`` for an explicitly flat view, and
    optionally the ``volume`` the shell opens drawn over the field — one of
    the volume bundles (:data:`xuebuild.binconvert.VOLUME_BUNDLES`) among
    the case's own ``variables``. The ranges are MapLibre's; the values
    pass through as written."""
    if not isinstance(value, dict) or set(value) - set(_VIEW_KEYS):
        raise ShowcaseError(f"case {case_id}: view must be {{center, zoom, pitch?, bearing?, terrain?, volume?}}")
    center = value.get("center")
    if not isinstance(center, list) or len(center) != 2:
        raise ShowcaseError(f"case {case_id}: view.center must be [longitude, latitude]")
    view: dict[str, Any] = {
        "center": [
            _view_number(center[0], "center[0]", case_id, -180.0, 180.0),
            _view_number(center[1], "center[1]", case_id, -90.0, 90.0),
        ],
        "zoom": _view_number(value.get("zoom"), "zoom", case_id, 0.0, 22.0),
    }
    if "pitch" in value:
        view["pitch"] = _view_number(value["pitch"], "pitch", case_id, 0.0, 85.0)
    if "bearing" in value:
        view["bearing"] = _view_number(value["bearing"], "bearing", case_id, -180.0, 180.0)
    if "terrain" in value:
        terrain = value["terrain"]
        if terrain is False:
            view["terrain"] = False
        else:
            exaggeration = _view_number(terrain, "terrain", case_id, 0.0, math.inf)
            if exaggeration <= 0:
                raise ShowcaseError(f"case {case_id}: view.terrain must be a positive exaggeration or false")
            view["terrain"] = exaggeration
    if "volume" in value:
        volume = value["volume"]
        if not isinstance(volume, str) or volume not in VOLUME_BUNDLES:
            raise ShowcaseError(f"case {case_id}: view.volume must be one of the volume bundles {sorted(VOLUME_BUNDLES)}")
        if volume not in variables:
            raise ShowcaseError(f"case {case_id}: view.volume names {volume}, which is not among the case's variables")
        view["volume"] = volume
    return view


def load_case(path: Path) -> CaseSpec:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShowcaseError(f"could not read case definition {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShowcaseError(f"case definition {path} must be a JSON object")
    spec = parse_case(payload, source_name=str(path))
    if spec.id != path.stem:
        raise ShowcaseError(f"case definition {path} declares id {spec.id}; name the file {spec.id}.json")
    return spec


def load_cases(directory: Path, ids: tuple[str, ...] = ()) -> list[CaseSpec]:
    """Every case definition in ``directory``, or just the named ones."""
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ShowcaseError(f"no case definitions in {directory}")
    specs = [load_case(path) for path in paths]
    if not ids:
        return specs
    known = {spec.id: spec for spec in specs}
    unknown = [case_id for case_id in ids if case_id not in known]
    if unknown:
        raise ShowcaseError(f"unknown case(s): {', '.join(unknown)}; {directory} has {', '.join(sorted(known))}")
    return [known[case_id] for case_id in ids]


def _grid_extent(grid: dict[str, Any]) -> list[float]:
    """The [west, south, east, north] a cropped grid's cell centers span.

    Longitudes stay in the grid's own frame, so a window crossing the
    antimeridian reports an east smaller than its west, exactly like the
    requested bbox does."""
    east = grid["firstLongitude"] + (grid["width"] - 1) * grid["longitudeStep"]
    if grid["width"] * grid["longitudeStep"] < 360.0:
        east = (east + 180.0) % 360.0 - 180.0
    return [
        round(grid["firstLongitude"], 6),
        round(grid["firstLatitude"] + (grid["height"] - 1) * grid["latitudeStep"], 6),
        round(east, 6),
        round(grid["firstLatitude"], 6),
    ]


def build_case(
    spec: CaseSpec,
    *,
    output_root: Path,
    raw_root: Path,
    work_root: Path,
    force: bool = False,
    force_download: bool = False,
    zarr: bool = True,
    container: bool = False,
) -> dict[str, Any]:
    """Fetch (or open), crop and encode one case, and write its manifest and
    sidecar. A case ships its Zarr stores alone, the way a run does;
    ``container`` writes each bundle's ``.xue`` as well (``showcase build
    --xue``).

    Only the case's own variables are downloaded, and into a per-case raw
    directory so a partial record set never shadows a full run's cache. A
    case built from a dataset downloads nothing: its input is the series
    file, or the directory of one series per variable, the definition
    names. A fetched observation (MRMS) downloads its window frame by frame,
    the products of the case's variables only.
    """
    source = spec.source
    output_dir = output_root / spec.output_subdirectory
    manifest_path = output_dir / "manifest.json"
    if spec.from_dataset:
        inputs = spec.dataset_path
        if not inputs.exists():
            raise ShowcaseError(
                f"case {spec.id}: dataset not found at {inputs}; set {OBSERVATION_ROOT_ENV} to where it lives"
            )
        LOG.info("reading %s series %s", source.manifest_model, inputs)
        report = convert_bin(
            inputs,
            output_dir,
            profile=spec.profile,
            work_root=work_root,
            expected_hours=spec.hours,
            manifest_path=manifest_path,
            force=force,
            # A cropped case is already a few megabytes: the half-resolution
            # ladder has nothing left to save, and the H.264 companion cannot
            # beat it at these sizes either.
            skip_video=True,
            skip_variants=True,
            model=spec.model,
            bbox=spec.bbox,
            bundle_ids=spec.variables,
            # A local dataset may hold more than the case.
            last_hour=spec.hours,
            zarr=zarr,
            container=container,
        )
    else:
        # A per-case raw directory, so a partial record set never shadows a
        # full run's cache.
        try:
            report = build_local_run(
                spec.model,
                spec.run,
                spec.hours,
                bbox=spec.bbox,
                bundle_ids=spec.variables,
                output_dir=output_dir,
                raw_root=raw_root / SHOWCASE_DIRECTORY / spec.id,
                work_root=work_root,
                profile=spec.profile,
                force=force,
                force_download=force_download,
                zarr=zarr,
                container=container,
            )
        except LocalRunError as exc:
            raise ShowcaseError(f"case {spec.id}: {exc}") from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["forecastHours"] != spec.hours:
        # A local-file case is held to its axis by ``last_hour``; a case's
        # declared range is never silently shortened.
        raise ShowcaseError(
            f"case {spec.id}: the {source.manifest_model} window reaches +{manifest['forecastHours']} h, "
            f"not the declared {spec.hours}"
        )
    entry = build_catalog_entry(spec, manifest, manifest_path.read_bytes(), report)
    (output_dir / CASE_SIDECAR).write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("built case %s (%d bundles, %.2f MB)", spec.id, len(manifest["bundles"]), entry["byteLength"] / 1e6)
    return entry


def _run_id(run_time: str) -> str:
    return datetime.fromisoformat(run_time.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y%m%d%H")


def build_catalog_entry(
    spec: CaseSpec, manifest: dict[str, Any], manifest_bytes: bytes, report: dict[str, Any]
) -> dict[str, Any]:
    """The catalog row for one built case."""
    # The build report carries the grid the bundles were encoded on — the
    # posters' own metadata describes their decimated grid, not this one.
    grid = report["grid"]
    entry: dict[str, Any] = {
        "id": spec.id,
        "title": spec.title,
        "summary": spec.summary,
        "modelId": spec.model,
        "model": manifest["model"],
        "product": manifest["product"],
        # A case built from a dataset names no cycle, so its run id comes
        # out of the manifest: the cycle the series counts from, or the hour
        # an observation series starts — the same YYYYMMDDHH shape.
        "run": spec.run or _run_id(manifest["runTime"]),
        "runTime": manifest["runTime"],
        "forecastHours": manifest["forecastHours"],
        "bbox": [round(value, 6) for value in spec.bbox],
        "dataBbox": _grid_extent(grid),
        "grid": {"width": grid["width"], "height": grid["height"]},
        "variables": [bundle["variable"] for bundle in manifest["bundles"]],
        "defaultVariable": spec.default_variable,
        "manifestPath": f"{spec.output_subdirectory}/manifest.json",
        "manifestCrc32": crc32_hex(manifest_bytes),
        # What the case weighs on the bucket: each bundle's container, or
        # its store on a bundle that ships only the store.
        "byteLength": sum(
            bundle["byteLength"] if "byteLength" in bundle else bundle["zarr"]["byteLength"]
            for bundle in manifest["bundles"]
        ),
    }
    if spec.event_time:
        entry["eventTime"] = spec.event_time
    if spec.tags:
        entry["tags"] = list(spec.tags)
    if spec.credit:
        entry["credit"] = spec.credit
    if spec.timeline:
        entry["timeline"] = spec.timeline
    if spec.radar:
        entry["radar"] = dict(spec.radar)
    if spec.fire:
        entry["fire"] = dict(spec.fire)
    if spec.river:
        entry["river"] = dict(spec.river)
    if spec.imagery:
        entry["imagery"] = _imagery_block(spec.imagery, spec.id)
    if spec.wind:
        entry["wind"] = dict(spec.wind)
    if spec.markers:
        entry["markers"] = [dict(marker) for marker in spec.markers]
    if spec.context:
        entry["context"] = dict(spec.context)
    if spec.view:
        entry["view"] = dict(spec.view)
    validate_catalog_entry(entry)
    return entry


def validate_catalog_entry(entry: dict[str, Any]) -> None:
    """Reject a catalog row the frontend could not render."""
    for key in ("id", "modelId", "model", "product", "run", "runTime", "manifestPath", "manifestCrc32"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ShowcaseError(f"catalog entry is missing {key}")
    if set(entry["id"]) - _ID_CHARACTERS:
        raise ShowcaseError(f"catalog entry id is not a slug: {entry['id']}")
    if entry["manifestPath"] != f"{SHOWCASE_DIRECTORY}/{entry['id']}/manifest.json":
        raise ShowcaseError(f"catalog entry {entry['id']} names a manifest outside its own directory")
    if len(entry["manifestCrc32"]) != 8:
        raise ShowcaseError(f"catalog entry {entry['id']} has an invalid manifest crc32")
    for key in ("title", "summary"):
        _localized(entry.get(key), key, entry["id"], REQUIRED_LOCALES)
    for key in ("bbox", "dataBbox"):
        box = entry.get(key)
        if not isinstance(box, list) or len(box) != 4:
            raise ShowcaseError(f"catalog entry {entry['id']} has an invalid {key}")
    variables = entry.get("variables")
    if not isinstance(variables, list) or not variables:
        raise ShowcaseError(f"catalog entry {entry['id']} lists no variables")
    if entry.get("defaultVariable") not in variables:
        raise ShowcaseError(f"catalog entry {entry['id']} defaults to a variable it does not ship")
    if not isinstance(entry.get("forecastHours"), int) or entry["forecastHours"] <= 0:
        raise ShowcaseError(f"catalog entry {entry['id']} has an invalid forecastHours")
    if "timeline" in entry and entry["timeline"] not in TIMELINES:
        raise ShowcaseError(f"catalog entry {entry['id']} has an unknown timeline")
    if "radar" in entry:
        _radar_block(entry["radar"], entry["id"])
    if "fire" in entry:
        _fire_block(entry["fire"], entry["id"])
    if "river" in entry:
        _river_block(entry["river"], entry["id"], variables)
    if "imagery" in entry:
        _imagery_block(entry["imagery"], entry["id"])
    if "wind" in entry:
        _wind_block(entry["wind"], entry["id"])
    if "view" in entry:
        _view_block(entry["view"], entry["id"], variables)
    if "markers" in entry:
        _markers_block(entry["markers"], entry["id"], entry["bbox"], REQUIRED_LOCALES)
    if "context" in entry:
        _context_block(entry["context"], entry["id"])


def refresh_sidecar(spec: CaseSpec, output_root: Path) -> dict[str, Any]:
    """Rewrite a built case's sidecar from its definition without rebuilding
    it: the prose (title, summary), the default variable, the event time,
    the tags, the credit, the river, the context, the markers and the view are the definition's to change — a
    translation added, a summary corrected, the camera moved — and none of
    them names a byte of the bundles. What does (model, run, box, variables) must still match, so a
    definition that has moved on from what was built is refused and the
    case rebuilt instead."""
    sidecar = output_root / spec.output_subdirectory / CASE_SIDECAR
    if not sidecar.is_file():
        raise ShowcaseError(f"case {spec.id} is not built at {sidecar.parent}; build it instead")
    entry = json.loads(sidecar.read_text(encoding="utf-8"))
    validate_catalog_entry(entry)
    # The bytes are identified by the manifest's model string, not the
    # source's shorthand: the shell derives ``modelId`` from that string and
    # refuses a row whose shorthand disagrees, so a source id renamed since
    # the case was built (``radar`` → ``cma``) is carried onto the row here
    # rather than treated as another dataset.
    built = (
        entry["model"],
        entry["run"],
        entry["forecastHours"],
        [round(value, 6) for value in entry["bbox"]],
        entry["variables"],
    )
    wanted = (
        source_spec(spec.model).manifest_model,
        spec.run or entry["run"],
        spec.hours,
        [round(value, 6) for value in spec.bbox],
        list(spec.variables),
    )
    if built != wanted:
        raise ShowcaseError(f"case {spec.id}: the definition no longer describes the built case; rebuild it")
    if spec.default_variable not in entry["variables"]:
        raise ShowcaseError(f"case {spec.id}: defaultVariable {spec.default_variable} is not among the built bundles")
    entry["modelId"] = spec.model
    entry["title"] = spec.title
    entry["summary"] = spec.summary
    entry["defaultVariable"] = spec.default_variable
    for key, value in (
        ("eventTime", spec.event_time),
        ("tags", list(spec.tags)),
        ("credit", spec.credit),
        ("timeline", spec.timeline),
        ("radar", dict(spec.radar) if spec.radar else None),
        ("fire", dict(spec.fire) if spec.fire else None),
        ("river", dict(spec.river) if spec.river else None),
        ("imagery", _imagery_block(spec.imagery, spec.id) if spec.imagery else None),
        ("wind", dict(spec.wind) if spec.wind else None),
        ("markers", [dict(marker) for marker in spec.markers] if spec.markers else None),
        ("context", dict(spec.context) if spec.context else None),
        ("view", dict(spec.view) if spec.view else None),
    ):
        if value:
            entry[key] = value
        else:
            entry.pop(key, None)
    validate_catalog_entry(entry)
    sidecar.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("refreshed case %s", spec.id)
    return entry


def _catalog_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    """Newest event first; cases without an event time fall back to the run."""
    return (entry.get("eventTime") or entry["runTime"], entry["id"])


def collect_catalog(output_root: Path) -> dict[str, Any]:
    """Build the catalog from the case sidecars actually present on disk."""
    entries: list[dict[str, Any]] = []
    for sidecar in sorted((output_root / SHOWCASE_DIRECTORY).glob(f"*/{CASE_SIDECAR}")):
        entry = json.loads(sidecar.read_text(encoding="utf-8"))
        validate_catalog_entry(entry)
        manifest_path = output_root / entry["manifestPath"]
        if not manifest_path.exists():
            raise ShowcaseError(f"case {entry['id']} has a sidecar but no manifest at {manifest_path}")
        crc32 = crc32_hex(manifest_path.read_bytes())
        if crc32 != entry["manifestCrc32"]:
            raise ShowcaseError(
                f"case {entry['id']} sidecar names manifest crc32 {entry['manifestCrc32']}, "
                f"but the manifest on disk is {crc32}; rebuild the case"
            )
        try:
            validate_bin_manifest(
                json.loads(manifest_path.read_text(encoding="utf-8")),
                expected_hours=entry["forecastHours"],
                require_core_variables=False,
            )
        except ManifestError as exc:
            raise ShowcaseError(f"case {entry['id']} has an invalid manifest: {exc}") from exc
        if "radar" in entry:
            entry["radar"] = _catalog_radar(output_root, entry)
        if "fire" in entry:
            entry["fire"] = _catalog_fire(output_root, entry)
        if "river" in entry:
            entry["river"] = _catalog_river(output_root, entry)
        if "context" in entry:
            entry["context"] = _catalog_context(output_root, entry)
        entries.append(entry)
    _check_wind_references(entries)
    entries.sort(key=_catalog_sort_key, reverse=True)
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "generatedAt": iso_z(datetime.now(UTC)),
        "cases": entries,
    }


def _check_wind_references(entries: Sequence[dict[str, Any]]) -> None:
    """Every ``wind`` block names a case in the same catalog that ships the
    bundle it borrows, so the shell never chases a row that is not there."""
    variables = {entry["id"]: entry["variables"] for entry in entries}
    for entry in entries:
        other = entry.get("wind", {}).get("case")
        if other is None:
            continue
        if other not in variables:
            raise ShowcaseError(f"case {entry['id']} borrows its wind from {other}, which is not in the catalog")
        if WIND_BUNDLE not in variables[other]:
            raise ShowcaseError(f"case {entry['id']} borrows its wind from {other}, which ships no {WIND_BUNDLE}")


def _catalog_radar(output_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The radar block as the catalog publishes it: the window manifest's
    length and CRC32 (its ``?v=``), measured from the built file, which
    must be a valid window holding the default site."""
    from .nexrad.schema import read_window  # the nexrad package imports nothing of the showcase's

    block = _radar_block(entry["radar"], entry["id"])
    path = output_root / SHOWCASE_DIRECTORY / entry["id"] / block["window"]
    if not path.is_file():
        raise ShowcaseError(f"case {entry['id']} names a radar window that is not built: {path}")
    data = path.read_bytes()
    try:
        window = read_window(path)
    except XueError as exc:
        raise ShowcaseError(f"case {entry['id']} has an invalid radar window: {exc}") from exc
    if block["defaultSite"] not in {row[0] for row in window["sites"]}:
        raise ShowcaseError(f"case {entry['id']}: radar.defaultSite {block['defaultSite']} is not in its window")
    return {**block, "byteLength": len(data), "crc32": crc32_hex(data)}


def _catalog_fire(output_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The fire block as the catalog publishes it: the round index's length
    and CRC32 (its ``?v=``), measured from the built file, which must be a
    valid fire index whose sensor files sit beside it."""
    from .fire.schema import read_index  # the fire package imports nothing of the showcase's

    block = _fire_block(entry["fire"], entry["id"])
    path = output_root / SHOWCASE_DIRECTORY / entry["id"] / block["window"]
    if not path.is_file():
        raise ShowcaseError(f"case {entry['id']} names a fire window that is not built: {path}")
    data = path.read_bytes()
    try:
        index = read_index(path)
    except XueError as exc:
        raise ShowcaseError(f"case {entry['id']} has an invalid fire window: {exc}") from exc
    for sensor in index["sensors"]:
        if not (path.parent / sensor["file"]["path"]).is_file():
            raise ShowcaseError(f"case {entry['id']}: fire window names {sensor['file']['path']}, which is not built")
    return {**block, "byteLength": len(data), "crc32": crc32_hex(data)}


def _catalog_river(output_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The river block as the catalog publishes it: the river index's
    length and CRC32 (its ``?v=``), measured from the built file, which
    must be a valid index whose every store, GeoParquet, gauge line and
    extent matches what is beside it."""
    from .river.schema import read_index  # the river package imports nothing of the showcase's

    block = _river_block(entry["river"], entry["id"], entry.get("variables", ()))
    path = output_root / SHOWCASE_DIRECTORY / entry["id"] / block["index"]
    if not path.is_file():
        raise ShowcaseError(f"case {entry['id']} names a river index that is not built: {path}")
    data = path.read_bytes()
    try:
        read_index(path)
    except XueError as exc:
        raise ShowcaseError(f"case {entry['id']} has an invalid river index: {exc}") from exc
    return {**block, "byteLength": len(data), "crc32": crc32_hex(data)}


def _catalog_context(output_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The context block as the catalog publishes it: the file's length and
    CRC32 (its ``?v=``), measured from the file in the case's directory,
    which must read as :func:`read_context` reads it."""
    block = _context_block(entry["context"], entry["id"])
    path = output_root / SHOWCASE_DIRECTORY / entry["id"] / block["file"]
    if not path.is_file():
        raise ShowcaseError(f"case {entry['id']} names a context file that is not built: {path}")
    data = path.read_bytes()
    try:
        read_context(data, entry["bbox"])
    except ValueError as exc:
        raise ShowcaseError(f"case {entry['id']} has an invalid context file {block['file']}: {exc}") from exc
    return {**block, "byteLength": len(data), "crc32": crc32_hex(data)}


def write_catalog(output_root: Path) -> Path:
    """(Re)write the mutable ``showcase.json`` catalog at the data root."""
    catalog = collect_catalog(output_root)
    path = output_root / CATALOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("wrote %s (%d case(s))", path, len(catalog["cases"]))
    # The STAC face of the same rows (docs/stac.md): one Item per case
    # beside its manifest, the showcase Collection, the root catalog.
    documents = write_showcase_documents(output_root, catalog)
    LOG.info("wrote %s and %d case item(s)", documents["collection"], len(documents["items"]))
    return path
