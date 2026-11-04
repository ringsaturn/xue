"""What a build reads (mirrored in ``encode/discover.rs``): which bundles a
source publishes and the inputs each is built from, the frames of a run and
the records every one of them must carry, and the store dimensions a bundle
takes. Every rule here is a function of the source registry and the files,
so a fan-out job, a whole build and the native encoder all answer alike.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import binformat, grib2
from .bundles import (
    PRECIPITATION_RATE_BUNDLE_ID,
    bundle,
    bundle_variable_ids,
    vector_input_ids,
)
from .derive import precipitation_mode
from .errors import ConversionError
from .gdal import discover_inputs, inspect_grib_multi, raster_expression
from .model import SourceFrame
from .sources import SourceSpec, family_frame_path
from .variables import VARIABLES, variable_spec

LOG = logging.getLogger(__name__)


# gdalinfo inspection is a ~1 s subprocess per file; oversubscribe mildly.
_INSPECT_WORKERS = min(32, 2 * (os.cpu_count() or 4))


def bundle_input_ids(source: SourceSpec, bundle_id: str) -> tuple[str, ...]:
    """The source input variables one published bundle is built from
    (:mod:`xuebuild.bundles`): a plain scalar's own record; a vector's or a
    volume's members; a derivation's inputs; a composite's channels — what
    the fetch must download for it to be composed, since the components
    themselves are never fetched — less an optional one the source does
    not fetch. The precipitation rate's input is whichever of the
    derivation's inputs the source lists (GFS prate, ECMWF tp, sflux
    prate_ave, Open-Meteo apcp), resolved off the source's own input list
    rather than hard-coded per model."""
    spec = bundle(bundle_id)
    if spec is None:
        return (bundle_id,)
    if spec.kind == "composite":
        return tuple(
            input_id
            for input_id in spec.inputs
            if input_id not in spec.optional_inputs or input_id in source.input_variable_ids
        )
    if spec.derivation is None:
        return spec.members
    if spec.derivation.kind == "precipitation-rate":
        return (next(vid for vid in source.input_variable_ids if vid in spec.derivation.inputs),)
    return spec.derivation.inputs


def grid_family_of(source: SourceSpec, variable_id: str) -> str | None:
    """The id of the grid family one input is read from, or None for an input
    on the source's own grid (:meth:`SourceSpec.grid_family_of`)."""
    companion = source.grid_family_of(variable_id)
    return companion.id if companion is not None else None


def bundle_grid_family(source: SourceSpec, bundle_id: str) -> str | None:
    """The grid family one bundle is built on: the family every one of its
    inputs is read from, so the bundle's grid, tile, variants and poster are
    that family's.

    A bundle whose inputs span two grids could not be written at all — its
    planes would not be the same shape — so a source that declares one is a
    registry bug, and it is refused here rather than half-built. (The
    vapour flux at 850 hPa reads the specific humidity and the wind pair
    there, all three from the CFSv2 pressure-level family, which is what
    makes it one bundle's worth of one grid.)"""
    families = {
        grid_family_of(source, variable_id) for variable_id in bundle_input_ids(source, bundle_id)
    }
    if len(families) > 1:
        named = sorted(family or "the primary file" for family in families)
        raise ConversionError(
            f"the {bundle_id} bundle of {source.manifest_model} reads {', '.join(named)}: "
            "every variable of a bundle must be on one grid"
        )
    return families.pop() if families else None


def discover_frames(input_path: Path | Sequence[Path], source: SourceSpec) -> list[Path]:
    """The frames a conversion reads, named by the primary file of each.

    A frame of a source with a grid family is more than one file under one
    run directory: the primary file, and a sibling of the same name under
    each family's own directory (:func:`~xuebuild.sources.family_frame_path`).
    Listing the run directory lists the primary files, as it always did.
    A build narrowed to one family's bundles fetches no primary file at
    all, so its frames are listed from that family's directory instead and
    named by the primary path they would have had; nothing reads that path
    unless a variable of the primary file is read."""
    try:
        return discover_inputs(input_path)
    except ConversionError:
        if not isinstance(input_path, Path) or not input_path.is_dir():
            raise
        for companion in source.grid_families:
            directory = input_path / companion.id
            if directory.is_dir():
                return [input_path / path.name for path in discover_inputs(directory)]
        raise


def analysis_optional_ids(source: SourceSpec, scalar_ids: tuple[str, ...]) -> tuple[str, ...]:
    """The published scalars of ``scalar_ids`` whose series has no analysis
    frame: the precipitation rate a source derives by de-accumulating or
    de-averaging, and any scalar read from a record the source lists under
    ``optional_at_analysis`` (which is how the interval-precipitation
    sources' rate, radiation and gust qualify — their inputs describe an
    interval that would precede the run)."""
    return tuple(
        variable_id
        for variable_id in scalar_ids
        if (variable_id == PRECIPITATION_RATE_BUNDLE_ID and precipitation_mode(source) in ("accumulated", "averaged"))
        or any(input_id in source.optional_at_analysis for input_id in bundle_input_ids(source, variable_id))
    )


def store_dimensions(source: SourceSpec, bundle_id: str) -> tuple[str, str, str]:
    """The dimension names a bundle's arrays carry in its run's shared store
    (docs/zarr-profile.md, "Coordinates"), and so the coordinate arrays the
    bundle uses there.

    The store's primary coordinates — ``time``, ``latitude``, ``longitude``
    — are the run's axis and the source's primary grid at the store's tier.
    A bundle takes a dimension of its own, ``<primary>_<suffix>``, where a
    rule of the source registry cuts it differently:

    * ``time_<bundle id>`` for a published scalar whose series has no
      analysis frame (:func:`analysis_optional_ids`: the de-accumulated or
      de-averaged rate, an interval maximum such as gust) and for a static
      field (its first variable's ``VariableSpec.static``: the terrain, one
      frame at the analysis) — the two rules ``convert_bin`` shortens a
      bundle's axis by;
    * ``latitude_<family>`` and ``longitude_<family>`` for a bundle on a grid
      family of its own, ``<family>`` being exactly the string
      :func:`bundle_grid_family` returns for it — the ``CompanionFile.id`` of
      the family its inputs are read from (``pgb``, the CFSv2 pressure
      levels on their 1-degree grid). Every bundle of one family shares the
      family's pair, as the bundles of the primary grid share ``latitude`` /
      ``longitude``. The id must match ``^[a-z][a-z0-9]*$``, so the name is a
      valid array name; one that does not is a registry bug, refused here.

    The decision is the rule, not a comparison of axes: a rate gets
    ``time_prate`` even on a build whose axis starts past the analysis and
    so happens to equal the run's. That makes it a pure function of the
    source and the bundle id, which a fan-out job building a subset of the
    bundles, a whole build and the native encoder all compute alike, so
    every job writes the same objects."""
    variable_ids = bundle_variable_ids(bundle_id)
    own_axis = (
        bundle_id in source.bundle_scalar_ids and bool(analysis_optional_ids(source, (bundle_id,)))
    ) or VARIABLES[variable_ids[0]].static
    family = bundle_grid_family(source, bundle_id)
    if family is not None and not _COORDINATE_SUFFIX.fullmatch(family):
        raise ConversionError(f"grid family {family!r} of {source.manifest_model} cannot name a coordinate array")
    return (
        f"time_{bundle_id}" if own_axis else "time",
        f"latitude_{family}" if family is not None else "latitude",
        f"longitude_{family}" if family is not None else "longitude",
    )


_COORDINATE_SUFFIX = re.compile(r"^[a-z][a-z0-9]*$")


def series_lead_seconds(frames: dict[str, SourceFrame]) -> int:
    """The lead time one file's frames all share, in seconds from the run."""
    return next(iter(frames.values())).lead_seconds


def published_bundle_ids(source: SourceSpec) -> tuple[str, ...]:
    """Every bundle a source can publish, in manifest order: its scalars,
    then each listed vector bundle whose inputs the source fetches, then
    each listed composite whose producer's channels it fetches, then each
    listed volume whose levels it fetches. A derived scalar counts the same
    way — listed, it ships only when its inputs are."""
    scalars = tuple(
        bundle_id
        for bundle_id in source.bundle_scalar_ids
        if all(variable_id in source.input_variable_ids for variable_id in bundle_input_ids(source, bundle_id))
    )
    vectors = tuple(
        bundle_id
        for bundle_id in source.bundle_vector_ids
        if all(variable_id in source.input_variable_ids for variable_id in vector_input_ids(bundle_id))
    )
    composites = tuple(
        bundle_id
        for bundle_id in source.bundle_composite_ids
        if all(variable_id in source.input_variable_ids for variable_id in bundle_input_ids(source, bundle_id))
    )
    volumes = tuple(
        bundle_id
        for bundle_id in source.bundle_volume_ids
        if all(variable_id in source.input_variable_ids for variable_id in bundle_input_ids(source, bundle_id))
    )
    return scalars + vectors + composites + volumes


def _check_frames(frames: list[SourceFrame], variable_id: str) -> None:
    leads = [frame.lead_seconds for frame in frames]
    if len(set(leads)) != len(leads):
        raise ConversionError(f"duplicate lead times for {variable_id}")
    if len({frame.run_time for frame in frames}) != 1:
        raise ConversionError(f"input files contain multiple GFS run times for {variable_id}")


def _check_reference_frames(
    fast_frames: dict[str, SourceFrame],
    reference_frames: dict[str, SourceFrame],
    variable_ids: tuple[str, ...],
) -> None:
    """Raise if the GRIB2 header index disagrees with gdalinfo on the
    per-run reference file. The header index locates the bands every
    gdal_translate extraction reads, so a mismatch must never pass silently;
    the raise drops the whole run into the gdalinfo fallback path."""
    for variable_id in variable_ids:
        fast = fast_frames.get(variable_id)
        reference = reference_frames.get(variable_id)
        if (fast is None) or (reference is None):
            if fast is not reference:
                raise ConversionError(
                    f"GRIB2 header index and gdalinfo disagree on the presence of {variable_id}"
                )
            continue
        if (
            fast.band != reference.band
            or fast.run_time != reference.run_time
            or fast.valid_time != reference.valid_time
            or fast.lead_seconds != reference.lead_seconds
            or raster_expression(variable_spec(variable_id), fast.unit)
            != raster_expression(variable_spec(variable_id), reference.unit)
        ):
            raise ConversionError(
                f"GRIB2 header index disagrees with gdalinfo for {variable_id} in {fast.path}: "
                f"{fast} != {reference}"
            )


def snap_observation_frames(
    per_file: list[dict[str, SourceFrame]], cadence_seconds: int
) -> list[dict[str, SourceFrame]]:
    """Re-key the frames of a fetched observation onto the window's axis.

    A GRIB observation names only its own time: each MRMS composite is its
    own reference time, some forty seconds past a two-minute mark, with a
    zero forecast time. The window is the axis: every observation time is
    snapped *down* to its ``cadence_seconds`` slot, the run time is the
    whole hour the first slot falls in (which is the hour the run id
    names), and a frame's lead is its slot's distance from it. Two files in
    one slot then collide on the lead, which the caller's uniqueness check
    refuses — the fetcher keeps one per slot."""
    snapped: list[dict[str, SourceFrame]] = []
    slots: list[int] = []
    for frames in per_file:
        # The products of one frame are stamped apart — the composite some
        # forty seconds past the mark, the rate on it — and agree on the slot.
        file_slots = {
            int(frame.valid_time.timestamp()) // cadence_seconds * cadence_seconds for frame in frames.values()
        }
        if len(file_slots) != 1:
            raise ConversionError(f"variables disagree on the observation slot in {next(iter(frames.values())).path}")
        slots.append(file_slots.pop())
    window_start = min(slots) // binformat.HOUR_SECONDS * binformat.HOUR_SECONDS
    run_time = datetime.fromtimestamp(window_start, tz=UTC)
    for frames, slot in zip(per_file, slots):
        snapped.append(
            {
                variable_id: replace(
                    frame,
                    run_time=run_time,
                    valid_time=datetime.fromtimestamp(slot, tz=UTC),
                    lead_seconds=slot - window_start,
                )
                for variable_id, frame in frames.items()
            }
        )
    return snapped


def prepare_frames_all(
    paths: list[Path],
    variable_ids: tuple[str, ...],
    optional_at_analysis: tuple[str, ...] = (),
    reference_frames: dict[str, SourceFrame] | None = None,
    cadence_seconds: int | None = None,
    families: Mapping[str, str | None] | None = None,
    static_ids: tuple[str, ...] = (),
) -> list[dict[str, SourceFrame]]:
    """Inspect every file once for all variables, in parallel across files.

    Variables in ``optional_at_analysis`` may be absent from the f000 file
    only (sflux carries no PRATE record at analysis time). Variables in
    ``static_ids`` may be absent from every file but the first: a static
    field is fetched and read at the analysis alone (ECMWF open data
    carries its orography at f000 and nowhere else), and the frame it is
    read from is the first. Inspection uses
    the GRIB2 header index; the first file is cross-checked against
    ``reference_frames`` (a real gdalinfo pass) and a run whose files the
    header index cannot parse falls back to gdalinfo inspection. With a
    ``cadence_seconds`` the files are a fetched observation, each stamped
    with its own time, and are re-keyed onto the window's axis
    (:func:`snap_observation_frames`).

    ``families`` maps a variable to the grid family it is read from, where a
    source has one: those variables are inspected in the frame's sibling
    file (:func:`~xuebuild.sources.family_frame_path`) instead of in the
    frame itself, and one frame's answers are merged into the one dictionary
    the rest of the converter reads. A frame whose family file is missing is
    refused before anything is inspected, since it is a fetch that did not
    finish rather than a file to fall back over."""
    by_family: dict[str | None, tuple[str, ...]] = {}
    for variable_id in variable_ids:
        family = families.get(variable_id) if families is not None else None
        by_family[family] = by_family.get(family, ()) + (variable_id,)
    for family in by_family:
        if family is None:
            # The primary file's absence is the inspector's own error.
            continue
        for path in paths:
            family_path = family_frame_path(path, family)
            if not family_path.is_file():
                raise ConversionError(
                    f"the {family} records of {path.name} are missing: {family_path} does not exist"
                )

    # A static record is expected only at the first frame, so it is optional
    # everywhere the inspector looks; its presence at the first frame is
    # required below.
    inspection_optional = optional_at_analysis + static_ids

    def inspect_all(inspect) -> list[dict[str, SourceFrame]]:
        def one(path: Path) -> dict[str, SourceFrame]:
            frames: dict[str, SourceFrame] = {}
            for family, family_ids in by_family.items():
                frames.update(inspect(family_frame_path(path, family), family_ids))
            return frames

        return list(executor.map(one, paths))

    with ThreadPoolExecutor(max_workers=_INSPECT_WORKERS) as executor:
        try:
            per_file = inspect_all(
                lambda path, ids: grib2.inspect_grib_fast(path, ids, optional_ids=inspection_optional)
            )
            if reference_frames is not None:
                _check_reference_frames(per_file[0], reference_frames, variable_ids)
        except ConversionError as exc:
            LOG.warning("GRIB2 header index unavailable (%s); falling back to gdalinfo inspection", exc)
            per_file = inspect_all(
                lambda path, ids: inspect_grib_multi(path, ids, optional_ids=inspection_optional)
            )
    if cadence_seconds is not None:
        per_file = snap_observation_frames(per_file, cadence_seconds)
    for frames in per_file:
        leads = {frame.lead_seconds for frame in frames.values()}
        if len(leads) > 1:
            raise ConversionError(f"variables disagree on the lead time in {next(iter(frames.values())).path}")
    per_file.sort(key=lambda frames: next(iter(frames.values())).lead_seconds)
    first_lead = next(iter(per_file[0].values())).lead_seconds
    for variable_id in variable_ids:
        present = [frames[variable_id] for frames in per_file if variable_id in frames]
        if variable_id in static_ids:
            if not present or present[0].lead_seconds != first_lead:
                raise ConversionError(
                    f"missing static {variable_id} record at the first frame"
                )
            _check_frames(present, variable_id)
            continue
        for frames in per_file:
            if variable_id in frames:
                continue
            lead = next(iter(frames.values())).lead_seconds
            if variable_id not in optional_at_analysis or lead != 0:
                raise ConversionError(
                    f"missing {variable_id} record at forecast hour {lead // binformat.HOUR_SECONDS}"
                )
        _check_frames(present, variable_id)
    return per_file
