"""The conversions the contract fixes (``docs/airport.md`` §2).

The Aviation Weather Center decodes in aviation units — knots, statute
miles, feet, inches of mercury — and the product publishes SI. The
arithmetic lives here rather than in either parser so that a metre in a
METAR and a metre in a TAF are the same metre, and so that the rounding,
which is part of the file's bytes, is written once.
"""

from __future__ import annotations

from .schema import COVER

KNOT_MS = 0.514444
"""One knot in metres per second."""

STATUTE_MILE_M = 1609.344
FOOT_M = 0.3048
INHG_HPA = 33.8639
"""One inch of mercury in hectopascals."""

VISIBILITY_UNLIMITED = 10000
"""What ``6+`` / ``10+`` / ``P6SM`` mean: ten kilometres or more. The
number is the convention, not a measurement (§2)."""

VISIBILITY_STEP_M = 100
CLOUD_BASE_STEP_M = 10


def round_to(value: float | None, digits: int) -> float | None:
    """Round to a fixed number of decimals, without a negative zero — the
    bytes of the file are the product's identity, so ``-0.0`` and ``0.0``
    must not both be reachable."""
    if value is None:
        return None
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def _step(value: float, step: int) -> int:
    return int(round(value / step)) * step


def bounded(value: float | None, bounds: tuple[float | None, float | None]) -> float | None:
    """The value, or None when it is outside what the contract admits.

    The service codes some unknowns as numbers — ``-99.99`` for a position
    it does not have — and a report is a station's word, not a
    measurement this pipeline can check. A value out of range is therefore
    dropped rather than published or raised on: one bad cell must not cost
    a round (``docs/airport.md`` §2)."""
    if value is None:
        return None
    minimum, maximum = bounds
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def wind_speed(knots: float | None) -> float | None:
    """Knots → m/s, to a tenth."""
    return None if knots is None else round_to(knots * KNOT_MS, 1)


def visibility(text: str | None) -> int | None:
    """The AWC's ``visibility_statute_mi`` → metres, to a hundred.

    ``6+``, ``10+`` and ``>6`` are not distances but the coded "ten
    kilometres or more", and all become :data:`VISIBILITY_UNLIMITED`.
    Anything else is a reported distance in statute miles."""
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.endswith("+") or cleaned.startswith((">", "P")):
        return VISIBILITY_UNLIMITED
    try:
        miles = float(cleaned)
    except ValueError:
        return None
    if miles < 0:
        return None
    return _step(miles * STATUTE_MILE_M, VISIBILITY_STEP_M)


def cloud_base(feet: float | None) -> int | None:
    """Feet above ground → metres above ground, to ten."""
    if feet is None or feet < 0:
        return None
    return _step(feet * FOOT_M, CLOUD_BASE_STEP_M)


def altimeter(inches_hg: float | None) -> float | None:
    """Inches of mercury → hPa, to a tenth."""
    return None if inches_hg is None else round_to(inches_hg * INHG_HPA, 1)


def cloud_layers(layers: list[tuple[str, float | None]]) -> list[list[object]]:
    """The reported cover/base pairs as the ``cloud`` field: ``[[cover,
    base_m], …]`` in the order reported. ``CLR``, ``SKC``, ``CAVOK`` and
    ``NSC`` report no base and keep ``null``; a cover that is not a cover
    code is dropped, so one odd cell cannot cost a round."""
    return [[cover, cloud_base(feet)] for cover, feet in layers if COVER.match(cover)]
