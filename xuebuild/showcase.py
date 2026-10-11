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
before.
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
    radar: dict[str, str] | None = None
    """The single-site radar overlay the case plays over its own axis
    (``docs/nexrad.md``): ``window``, the window manifest of a
    ``xue nexrad-case`` build, relative to the case's directory, and the
    ``defaultSite`` and ``defaultProduct`` the viewer opens on."""
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

    profile = payload.get("profile", "quality")
    if profile not in ("quality", "balanced", "compact"):
        raise ShowcaseError(f"case {case_id}: profile must be quality, balanced or compact")

    radar = payload.get("radar")
    if radar is not None:
        radar = _radar_block(radar, case_id)

    view = payload.get("view")
    if view is not None:
        view = _view_block(view, case_id, ordered)

    return CaseSpec(
        id=case_id,
        title=_localized(payload.get("title"), "title", case_id),
        summary=_localized(payload.get("summary"), "summary", case_id),
        model=model,
        run=run,
        dataset=dataset,
        hours=hours,
        bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        variables=ordered,
        default_variable=default_variable,
        event_time=event_time,
        tags=tuple(tags),
        credit=credit,
        profile=profile,
        radar=radar,
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
    zarr: bool = False,
    container: bool = True,
) -> dict[str, Any]:
    """Fetch (or open), crop and encode one case, and write its manifest and
    sidecar. ``zarr`` derives a Zarr store beside every bundle, the way
    ``build-bin --zarr`` does for a run, and ``container=False`` retires the
    ``.xue`` behind it, the way ``--no-xue`` does.

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
    if spec.radar:
        entry["radar"] = dict(spec.radar)
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
    if "radar" in entry:
        _radar_block(entry["radar"], entry["id"])
    if "view" in entry:
        _view_block(entry["view"], entry["id"], variables)


def refresh_sidecar(spec: CaseSpec, output_root: Path) -> dict[str, Any]:
    """Rewrite a built case's sidecar from its definition without rebuilding
    it: the prose (title, summary), the default variable, the event time,
    the tags, the credit and the view are the definition's to change — a
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
        ("radar", dict(spec.radar) if spec.radar else None),
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
        entries.append(entry)
    entries.sort(key=_catalog_sort_key, reverse=True)
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "generatedAt": iso_z(datetime.now(UTC)),
        "cases": entries,
    }


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
