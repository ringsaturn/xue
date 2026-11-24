"""Which arrays a source's column store holds (docs/zarr-profile.md, "Column
store"): :class:`ColumnArraySpec`, the plain data a
``SourceSpec.column_arrays`` is made of, and :func:`column_arrays`, which
derives it from what the source publishes. Quantities are recognized by their
GRIB2 identity, never by an id; the registry tables are the only imports, so
``sources.py`` may call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bundles import bundle, bundle_variable_ids
from .variables import VARIABLES, VariableSpec

if TYPE_CHECKING:
    from .sources import SourceSpec

ISOBARIC_SURFACE = 100
"""GRIB2 code table 4.5: an isobaric surface, its value in Pa."""

PASCALS_PER_HPA = 100

LEVELLED_QUANTITIES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("tmp", (0, 0, 0)),
    ("rh", (0, 1, 1)),
    ("hgt", (0, 3, 5)),
    ("ugrd", (0, 2, 2)),
    ("vgrd", (0, 2, 3)),
)
"""``(array id, (discipline, category, number))`` of each levelled array: what
a profile is drawn from on every isobaric surface, in array order."""

SURFACE_QUANTITIES: tuple[tuple[int, int, int, int, float | None], ...] = (
    (0, 0, 0, 103, 2.0),  # 2 m temperature
    (0, 0, 6, 103, 2.0),  # 2 m dew point
    (0, 3, 1, 101, None),  # mean sea level pressure
    (0, 3, 5, 1, 0.0),  # terrain height
    (0, 2, 2, 103, 10.0),  # 10 m wind
    (0, 2, 3, 103, 10.0),
    (0, 7, 6, 1, 0.0),  # CAPE
    (0, 7, 7, 1, 0.0),  # convective inhibition
)
"""``(discipline, category, number, fixed surface type, value)`` of each field
the column stands on, in array order. The array's id is its variable's."""


@dataclass(frozen=True)
class ColumnArraySpec:
    """One array of a source's column store: its id and its
    ``(bundle id, variable id)`` members. Members on isobaric surfaces make a
    levelled array, highest pressure first; any other array is one member's."""

    id: str
    members: tuple[tuple[str, str], ...]


def _is_plain(spec: VariableSpec) -> bool:
    # A statistic of an ensemble, a composite's gun or an aerosol species
    # carries its quantity's parameter but is not the quantity itself.
    return spec.grib2_ensemble is None and spec.grib2_aerosol is None and spec.producer_id is None


def column_arrays(source: SourceSpec) -> tuple[ColumnArraySpec, ...]:
    """The column arrays of everything ``source`` publishes: one levelled
    array per quantity of :data:`LEVELLED_QUANTITIES` it ships on at least
    one isobaric surface, then one surface array per field of
    :data:`SURFACE_QUANTITIES` it ships."""
    ensembles = {variable_id for variable_id, _ in source.ensembles}
    bands = {variable_id for variable_id, _ in source.bands}
    levelled: dict[tuple[int, int, int], dict[float, tuple[str, str]]] = {}
    surface: dict[tuple[int, int, int, int, float | None], tuple[str, str]] = {}
    for bundle_id in (*source.bundle_scalar_ids, *source.bundle_vector_ids):
        registered = bundle(bundle_id)
        if registered is not None and registered.derivation is not None:
            continue
        variable_ids = bundle_variable_ids(bundle_id)
        if not all(variable_id in source.input_variable_ids for variable_id in variable_ids):
            continue
        for variable_id in variable_ids:
            spec = VARIABLES[variable_id]
            if variable_id in ensembles or variable_id in bands or not _is_plain(spec):
                continue
            parameter = (spec.grib2_discipline, spec.grib2_category, spec.grib2_number)
            member = (bundle_id, variable_id)
            if spec.grib2_level_type == ISOBARIC_SURFACE and spec.grib2_level_value is not None:
                levelled.setdefault(parameter, {})[spec.grib2_level_value] = member
            else:
                surface[(*parameter, spec.grib2_level_type, spec.grib2_level_value)] = member
    arrays = [
        ColumnArraySpec(array_id, tuple(levelled[parameter][pascals] for pascals in sorted(levelled[parameter], reverse=True)))
        for array_id, parameter in LEVELLED_QUANTITIES
        if parameter in levelled
    ]
    arrays += [ColumnArraySpec(surface[key][1], (surface[key],)) for key in SURFACE_QUANTITIES if key in surface]
    return tuple(arrays)


def column_bundle_ids(source: SourceSpec) -> frozenset[str]:
    """The bundles a source's column store is cut from."""
    return frozenset(bundle_id for array in source.column_arrays for bundle_id, _ in array.members)
