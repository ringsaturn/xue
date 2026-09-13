"""Who says what about a storm: the agencies whose forecasts and best
tracks the product carries, the models whose objective tracks it carries,
and the basin vocabulary they share.

This table is mirrored by the frontend (``web/src/tc/agencies.ts``) and
pinned by ``tests/fixtures/tc-registry.json``, the way
``pressure-registry.json`` pins the codebooks: an id, a color or a basin
letter changed here without the fixture is a test failure, not a silent
drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgencySpec:
    """One issuing centre."""

    id: str
    """Product key: ``agencies.<id>`` in a storm file."""
    name: str
    """English name; the frontend's dictionary key is ``tc.agency.<id>``."""
    color: str
    """Fixed line color, Okabe–Ito so the set stays distinguishable under
    the common color vision deficiencies. Never derived from the theme."""
    basins: tuple[str, ...]
    """ATCF basins the centre has responsibility in (its forecasts are
    drawn there; a best track can reach anywhere)."""
    scale: str
    """Intensity scale its ``class`` strings come from; the frontend colors
    a class by the agency's own table and never converts across scales."""
    ibtracs: str | None
    """IBTrACS column prefix carrying its best track (``USA_``,
    ``TOKYO_``…), None when IBTrACS has no column set for it."""
    forecasts: bool
    """True when the product carries this centre's *forecast* — the
    centres phase 0 ingests. A best-track-only centre is False."""


@dataclass(frozen=True)
class ModelSpec:
    """One objective (model) track source."""

    id: str
    """Product key: ``models.<id>`` in a storm file."""
    name: str
    ensemble: bool
    """True for a member set (``Ensemble`` shape), False for one track."""
    techs: tuple[str, ...]
    """ATCF ``tech`` ids that carry it (``AVNO`` for GFS; ``AP01``…``AP30``
    plus ``AC00`` for the GEFS members; empty when the source is not ATCF
    text)."""
    color: str


# Okabe & Ito (2008), the eight-color set built for color vision deficiency.
_VERMILLION = "#d55e00"
_ORANGE = "#e69f00"
_BLUE = "#0072b2"
_GREEN = "#009e73"
_PURPLE = "#cc79a7"
_SKY = "#56b4e9"
_YELLOW = "#f0e442"
_GREY = "#8a8a8a"

AGENCIES: dict[str, AgencySpec] = {
    spec.id: spec
    for spec in (
        AgencySpec("nhc", "National Hurricane Center", _VERMILLION, ("AL", "EP", "CP"), "sshs", "USA", True),
        AgencySpec("jtwc", "Joint Typhoon Warning Center", _ORANGE, ("WP", "IO", "SH"), "sshs", "USA", True),
        AgencySpec("jma", "Japan Meteorological Agency", _BLUE, ("WP",), "jma", "TOKYO", False),
        AgencySpec("cma", "China Meteorological Administration", _GREEN, ("WP",), "cma", "CMA", False),
        AgencySpec("cwa", "Central Weather Administration", _PURPLE, ("WP",), "cwa", None, False),
        AgencySpec("hko", "Hong Kong Observatory", _SKY, ("WP",), "hko", "HKO", False),
        AgencySpec("kma", "Korea Meteorological Administration", _YELLOW, ("WP",), "kma", "KMA", False),
        AgencySpec("imd", "India Meteorological Department", _GREY, ("IO",), "imd", "NEWDELHI", False),
        AgencySpec("mfr", "Météo-France La Réunion", _GREY, ("SH",), "mfr", "REUNION", False),
        AgencySpec("bom", "Bureau of Meteorology", _GREY, ("SH",), "bom", "BOM", False),
        AgencySpec("fms", "Fiji Meteorological Service", _GREY, ("SH",), "fms", "NADI", False),
        AgencySpec("mnz", "MetService New Zealand", _GREY, ("SH",), "mnz", "WELLINGTON", False),
    )
}

MODELS: dict[str, ModelSpec] = {
    spec.id: spec
    for spec in (
        ModelSpec("gfs", "GFS", False, ("AVNO",), "#333333"),
        ModelSpec("gefs", "GEFS", True, ("AC00",) + tuple(f"AP{n:02d}" for n in range(1, 31)), "#333333"),
        ModelSpec("ecmwf", "ECMWF IFS", False, (), "#555555"),
        ModelSpec("ecmwfens", "ECMWF ENS", True, (), "#555555"),
    )
}

# ATCF basins. ``IO`` and ``SH`` are what JTWC and the NCEP tracker file
# under; ECMWF's storm identifiers end in a finer letter (below) that folds
# into these.
BASINS: dict[str, str] = {
    "AL": "North Atlantic",
    "EP": "Eastern North Pacific",
    "CP": "Central North Pacific",
    "WP": "Western North Pacific",
    "IO": "North Indian Ocean",
    "SH": "Southern Hemisphere",
}

# The suffix letter of an ATCF short id (``14E``) or an ECMWF
# ``stormIdentifier``, to the basin it files under. ``A``/``B`` are the
# Arabian Sea and the Bay of Bengal, ``S``/``P``/``U`` the South Indian,
# South Pacific and Australian regions; JTWC and NCEP keep the two-letter
# basin for all of them.
BASIN_LETTERS: dict[str, str] = {
    "L": "AL",
    "E": "EP",
    "C": "CP",
    "W": "WP",
    "A": "IO",
    "B": "IO",
    "S": "SH",
    "P": "SH",
    "U": "SH",
}

# ATCF cyclone numbers: 01–49 are the season's numbered systems, 90–99 the
# investigation areas (reused within a season), 80–89 training, 50–79
# test and internal. Only the first two reach the product.
NUMBERED_MAX = 49
INVEST_MIN = 90

# Fixed unit factors, written once so every parser converts the same way.
KNOT = 0.514444
"""m/s per knot."""
NAUTICAL_MILE = 1.852
"""km per nautical mile."""


def basin_of_letter(letter: str) -> str | None:
    return BASIN_LETTERS.get(letter.upper())


def registry_document() -> dict[str, Any]:
    """The table as ``tests/fixtures/tc-registry.json`` pins it."""
    return {
        "agencies": [
            {
                "id": spec.id,
                "name": spec.name,
                "color": spec.color,
                "basins": list(spec.basins),
                "scale": spec.scale,
                "ibtracs": spec.ibtracs,
                "forecasts": spec.forecasts,
            }
            for spec in AGENCIES.values()
        ],
        "models": [
            {
                "id": spec.id,
                "name": spec.name,
                "ensemble": spec.ensemble,
                "techs": list(spec.techs),
                "color": spec.color,
            }
            for spec in MODELS.values()
        ],
        "basins": dict(BASINS),
        "basinLetters": dict(BASIN_LETTERS),
        "numberedMax": NUMBERED_MAX,
        "investMin": INVEST_MIN,
    }
