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

A case on an observation source (the CMA radar mosaic) is the same object
built from a different input: it names a local ``dataset`` file instead of a
``run`` to fetch, and its axis is whatever times that file carries. Nothing
fetches it, so such a case is only rebuildable by someone who has the
dataset; the built output is an ordinary case like any other.
"""

from __future__ import annotations

import json
import logging
import os
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .binconvert import bundle_input_ids, convert_bin, published_bundle_ids
from .errors import DownloadError, ManifestError, XueError
from .fetch import fetch_run, parse_run
from .manifest import iso_z, validate_bin_manifest
from .sources import SourceSpec, source_spec

LOG = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 1

CATALOG_FILENAME = "showcase.json"
"""Mutable catalog at the data root, next to the per-model live pointers."""

SHOWCASE_DIRECTORY = "showcase"
"""Directory under the data root holding one subdirectory per case."""

CASE_SIDECAR = "case.json"
"""Per-case catalog entry, written next to the case's manifest."""

LOCALES = ("zh", "en")
"""Locales every human-facing case string must provide, matching the UI."""

OBSERVATION_ROOT_ENV = "XUE_OBSERVATION_ROOT"
"""Environment variable holding the root an observation case's ``dataset``
path is resolved against."""

DEFAULT_OBSERVATION_ROOT = Path("../radar-l3-mst/data")
"""Where observation datasets live by default: the sibling checkout of the
tool that produces them, relative to the working directory. These files are
not published anywhere, so an observation case is only rebuildable by someone
who has them."""

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
    """The archived cycle to fetch, empty on an observation case — the
    dataset file says when its series starts."""
    dataset: str
    """Observation cases only: the NetCDF file holding the series, resolved
    against :data:`OBSERVATION_ROOT_ENV` (default
    :data:`DEFAULT_OBSERVATION_ROOT`) when it is not absolute."""
    hours: int
    """Last hour of the case's axis, counted from its first frame."""
    bbox: tuple[float, float, float, float]
    variables: tuple[str, ...]
    default_variable: str
    event_time: str | None
    tags: tuple[str, ...]
    credit: str | None
    profile: str

    @property
    def output_subdirectory(self) -> str:
        return f"{SHOWCASE_DIRECTORY}/{self.id}"

    @property
    def source(self) -> SourceSpec:
        return source_spec(self.model)

    @property
    def dataset_path(self) -> Path:
        """The observation file this case is built from."""
        if not self.dataset:
            raise ShowcaseError(f"case {self.id} names no observation dataset")
        path = Path(self.dataset).expanduser()
        if path.is_absolute():
            return path
        root = Path(os.environ.get(OBSERVATION_ROOT_ENV, DEFAULT_OBSERVATION_ROOT)).expanduser()
        return root / path


def _localized(value: object, label: str, case_id: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ShowcaseError(f"case {case_id}: {label} must be an object keyed by locale")
    missing = [locale for locale in LOCALES if not isinstance(value.get(locale), str) or not value[locale].strip()]
    if missing:
        raise ShowcaseError(f"case {case_id}: {label} is missing {', '.join(missing)}")
    return {locale: value[locale].strip() for locale in LOCALES}


def parse_case(payload: dict[str, Any], *, source_name: str = "<case>") -> CaseSpec:
    """Validate one case definition."""
    case_id = payload.get("id")
    if not isinstance(case_id, str) or not case_id or set(case_id) - _ID_CHARACTERS:
        raise ShowcaseError(f"{source_name}: id must be lowercase letters, digits and hyphens")
    model = payload.get("model")
    if not isinstance(model, str):
        raise ShowcaseError(f"case {case_id}: model must be a string")
    source = source_spec(model)

    # A forecast case names an archived cycle to fetch; an observation case
    # names the local file that already holds its series, and its start time
    # comes out of that file rather than out of the definition.
    run = payload.get("run", "")
    dataset = payload.get("dataset", "")
    if source.observation:
        if run:
            raise ShowcaseError(f"case {case_id}: an observation case has no run to name")
        if not isinstance(dataset, str) or not dataset:
            raise ShowcaseError(f"case {case_id}: dataset must name the observation file to build from")
    else:
        if dataset:
            raise ShowcaseError(f"case {case_id}: only an observation case is built from a dataset file")
        if not isinstance(run, str) or not run:
            raise ShowcaseError(f"case {case_id}: run must be a UTC cycle in YYYYMMDDHH format")
        parse_run(run)

    hours = payload.get("hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        raise ShowcaseError(f"case {case_id}: hours must be a positive integer forecast hour")
    if not source.observation:
        try:
            source.forecast_hours(hours)
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
    )


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
) -> dict[str, Any]:
    """Fetch (or open), crop and encode one case, and write its manifest and
    sidecar.

    Only the case's own variables are downloaded, and into a per-case raw
    directory so a partial record set never shadows a full run's cache. An
    observation case downloads nothing: its input is the local dataset file
    the definition names.
    """
    source = spec.source
    if source.observation:
        inputs: Path | list[Path] = spec.dataset_path
        if not inputs.is_file():
            raise ShowcaseError(
                f"case {spec.id}: observation dataset not found at {inputs}; "
                f"set {OBSERVATION_ROOT_ENV} to where it lives"
            )
        LOG.info("reading %s observation series %s", source.manifest_model, inputs)
    else:
        run = parse_run(spec.run)
        input_ids = tuple(
            dict.fromkeys(
                input_id for bundle_id in spec.variables for input_id in bundle_input_ids(source, bundle_id)
            )
        )
        case_raw_root = raw_root / SHOWCASE_DIRECTORY / spec.id
        LOG.info(
            "fetching %s run %s f000-f%03d (%s)", source.manifest_model, spec.run, spec.hours, ", ".join(input_ids)
        )
        # Exactly the case's own frames: the raw directory can hold more, left
        # behind by an earlier build of the same case with a longer range.
        inputs = fetch_run(
            run, spec.hours, case_raw_root, force=force_download, model=spec.model, input_ids=input_ids
        )

    output_dir = output_root / spec.output_subdirectory
    manifest_path = output_dir / "manifest.json"
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
        last_hour=spec.hours if source.observation else None,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = build_catalog_entry(spec, manifest, manifest_path.read_bytes(), report)
    (output_dir / CASE_SIDECAR).write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("built case %s (%d bundles, %.2f MB)", spec.id, len(manifest["bundles"]), report["byteLength"] / 1e6)
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
        # An observation case names no cycle, so its run id is the hour its
        # series starts — the same YYYYMMDDHH shape a forecast cycle has.
        "run": spec.run or _run_id(manifest["runTime"]),
        "runTime": manifest["runTime"],
        "forecastHours": manifest["forecastHours"],
        "bbox": [round(value, 6) for value in spec.bbox],
        "dataBbox": _grid_extent(grid),
        "grid": {"width": grid["width"], "height": grid["height"]},
        "variables": [bundle["variable"] for bundle in manifest["bundles"]],
        "defaultVariable": spec.default_variable,
        "manifestPath": f"{spec.output_subdirectory}/manifest.json",
        "manifestCrc32": f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
        "byteLength": report["byteLength"],
    }
    if spec.event_time:
        entry["eventTime"] = spec.event_time
    if spec.tags:
        entry["tags"] = list(spec.tags)
    if spec.credit:
        entry["credit"] = spec.credit
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
        _localized(entry.get(key), key, entry["id"])
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
        crc32 = f"{zlib.crc32(manifest_path.read_bytes()) & 0xFFFFFFFF:08x}"
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
        entries.append(entry)
    entries.sort(key=_catalog_sort_key, reverse=True)
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "generatedAt": iso_z(datetime.now(UTC)),
        "cases": entries,
    }


def write_catalog(output_root: Path) -> Path:
    """(Re)write the mutable ``showcase.json`` catalog at the data root."""
    catalog = collect_catalog(output_root)
    path = output_root / CATALOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("wrote %s (%d case(s))", path, len(catalog["cases"]))
    return path
