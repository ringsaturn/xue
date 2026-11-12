"""A bundle's metadata document (docs/format.md, schema v3), mirrored in
``encode/metadata.rs``: the time axis on its coarsest unit, the grid, and
one descriptor per variable naming its GRIB2 identity and quantization.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import binformat
from .errors import ConversionError
from .grid import GridInfo
from .manifest import iso_z
from .quantize import PROFILES
from .sources import SourceSpec, source_spec
from .variables import variable_spec


METADATA_SCHEMA_VERSION = 3
"""Bundle metadata schema this encoder writes: every variable descriptor
carries its GRIB2 parameter identity (docs/format.md). Earlier versions
remain readable; nothing new is written at them."""

PROJECTED_METADATA_SCHEMA_VERSION = 4
"""The one version a grid can raise it to: a bundle on a map projection, whose
``grid`` block has no regular fields for an older reader to place it by. Every
other bundle stays at :data:`METADATA_SCHEMA_VERSION`, the lowest that fits."""


def axis_unit_seconds(lead_seconds: Sequence[int]) -> int:
    """The coarsest unit that expresses every frame's lead time exactly.

    An hour for every forecast source, and whatever the file carries for an
    observation series — six minutes for the radar mosaic. Capping at an hour
    keeps a whole-hour axis indexed by its forecast hours, exactly as before
    (docs/format.md)."""
    return math.gcd(binformat.HOUR_SECONDS, *lead_seconds)


def lead_hours(offset: int, unit_seconds: int) -> int:
    """``offset`` as a whole number of hours, rounded up — how far a run
    reaches, for the manifest's coarse ``forecastHours``."""
    return -(-offset * unit_seconds // binformat.HOUR_SECONDS)


def time_metadata(offsets: list[int], unit_seconds: int) -> dict[str, Any]:
    """The metadata ``time`` block: offsets on a declared unit, uniform ones
    declaring a ``frameStep`` and the rest listing their offsets outright
    (docs/format.md). A forecast run lists them because its source changes
    cadence partway; an observation series lists them wherever a publication
    was missed."""
    steps = {after - before for before, after in zip(offsets, offsets[1:])}
    block: dict[str, Any] = {
        "unitSeconds": unit_seconds,
        "firstFrameOffset": offsets[0],
        "frameCount": len(offsets),
    }
    if len(steps) <= 1:
        block["frameStep"] = steps.pop() if steps else 1
    else:
        block["frameOffsets"] = list(offsets)
    return block


def _variable_metadata(
    variable_id: str,
    numeric_id: int,
    source: SourceSpec,
    profile: str,
    producers: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """One schema v3 variable descriptor: what the field is (GRIB2 parameter
    and fixed surface), what its values mean, and how they are quantized.

    ``numeric_id`` is the file-local ``variableId`` handle that ties this
    descriptor to the index (docs/format.md): the variable's 1-based position
    in the bundle's variable list, assigned by :func:`build_metadata`.
    ``producers`` is what the observation series was stamped with, by
    variable id: a produced variable's ``producer`` block is its registered
    id and that version (docs/format.md §"Band, Producer and Aerosol")."""
    spec = variable_spec(variable_id)
    parameter = spec.parameter_metadata()
    for statistical_id, process in source.statistical_processes:
        if statistical_id == variable_id:
            # A statistic over the step, not the instantaneous field GFS
            # pgrb2 carries under the same parameter: the rate derived from
            # a run-total accumulation (ECMWF) or a window average (sflux)
            # is a mean, ECMWF's gust a maximum.
            parameter["typeOfStatisticalProcessing"] = process
    block = {
        "numericId": numeric_id,
        "id": variable_id,
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": parameter,
    }
    for band_id, band in source.bands:
        if band_id == variable_id:
            # A satellite channel: the brightness temperature parameter is
            # the same for every infrared band, so the band block beside it
            # says which channel of which instrument this one is.
            block["band"] = band.metadata()
    if spec.producer_id is not None:
        # A composite's gun: the algorithm that derived it, whose version is
        # the series' stamp (the fetch stage ran it; the converter did not).
        stamp = (producers or {}).get(variable_id)
        if stamp is None or stamp[0] != spec.producer_id:
            raise ConversionError(f"{variable_id} must be stamped by producer {spec.producer_id!r} to be written")
        block["producer"] = {"id": stamp[0], "version": stamp[1]}
    if spec.grib2_aerosol is not None:
        # An aerosol product: the parameter names the quantity, the block
        # beside it the species and the intervals it was computed over —
        # the rest of the identity a template 4.48 record carries.
        block["aerosol"] = spec.grib2_aerosol.metadata()
    ensemble = dict(source.ensembles).get(variable_id, spec.grib2_ensemble)
    if ensemble is not None:
        # A statistic of an ensemble: the parameter names the quantity, the
        # block which statistic of the members this field is — the mean or
        # median standing in for the deterministic field included.
        block["ensemble"] = ensemble.metadata()
    block["quantization"] = PROFILES[profile][variable_id].metadata()
    return block


def build_metadata(
    run_time: datetime,
    offsets: list[int],
    grid: GridInfo,
    profile: str,
    variable_ids: tuple[str, ...] = ("tmp2m", "prate"),
    *,
    source: SourceSpec | None = None,
    unit_seconds: int = binformat.HOUR_SECONDS,
    producers: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    resolved = source or source_spec("gfs")
    return {
        "schemaVersion": PROJECTED_METADATA_SCHEMA_VERSION if grid.projection is not None else METADATA_SCHEMA_VERSION,
        "model": resolved.manifest_model,
        "product": resolved.product,
        "runTime": iso_z(run_time),
        "profile": profile,
        "time": time_metadata(offsets, unit_seconds),
        "grid": grid.metadata(),
        # variableId is a file-local handle: 1..n by position in the bundle's
        # variable list, which is the same list that fixes chunk order in
        # _bundle_chunks. Nothing outside one file reads these numbers — a
        # variable's identity is its GRIB2 parameter block.
        "variables": [
            _variable_metadata(variable_id, numeric_id, resolved, profile, producers)
            for numeric_id, variable_id in enumerate(variable_ids, start=1)
        ],
    }
