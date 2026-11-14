"""The rule vocabulary of the registry document (docs/registry.md).

A variable entry names, out of closed sets, every per-variable rule the
encoders apply: how a record's values reach the published unit and which
spellings of a unit a record may carry (:class:`Conversion`), how its
record is recognised among a GRIB's bands (:class:`GdalRule`), its
predictor, its decimation and its companions. The tables in
``variables.py`` author them; this module is the vocabulary and, for the
unit rules, the implementation: the spellings each rule accepts, the
raster expression the two inspection paths compare, and the arithmetic
the converter applies to a plane. The GDAL rules are evaluated in
``gdal.py``, against a band's metadata.

Adding a name to a closed set is a behaviour change that needs every
reader released; using an existing name on a new entry is a table change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import ConversionError

#: Standard gravity, the g in q·V/g and the divisor that takes a
#: geopotential to metres.
STANDARD_GRAVITY = 9.80665

CONVERSION_RULES = frozenset(
    {
        "none",
        "celsius",
        "soil-temperature",
        "precipitation-rate",
        "precipitation-accumulation",
        "pascal-to-hectopascal",
        "metre-to-kilometre",
        "mass-ratio-to-g-per-kg",
        "proportion-to-percent",
        "cloud-cover",
        "degrees-true",
        "geopotential-to-metres",
        "mass-density-to-micrograms",
        "code-table",
    }
)
MATCH_RULES = frozenset(
    {
        "surface",
        "height",
        "entire-atmosphere",
        "mean-sea-level",
        "isobaric",
        "cloud-layer",
        "template",
        "template-aerosol",
        "template-ensemble",
        "template-rank",
        "template-mrms",
        "total-precipitation",
        "local-parameter",
        "phrase",
        "geopotential-surface",
    }
)
PREDICTORS = frozenset({"delta", "raw"})
DECIMATIONS = frozenset({"sample", "peak"})
COMPANIONS = frozenset({"poster", "video"})


# ---------------------------------------------------------------------------
# Unit rules


@dataclass(frozen=True)
class Conversion:
    """The unit rule of one variable: the arithmetic that takes a record's
    values to the published unit, and the spellings a record may carry.
    A ``none`` rule with ``accepted_units`` is identity under exactly those
    spellings (compacted); without, under any. ``table`` is the code table
    a ``code-table`` rule names."""

    rule: str
    accepted_units: tuple[str, ...] | None = None
    table: str | None = None

    def __post_init__(self) -> None:
        if self.rule not in CONVERSION_RULES:
            raise ValueError(f"unknown conversion rule: {self.rule}")
        if self.accepted_units is not None and self.rule != "none":
            raise ValueError(f"the {self.rule} rule names its own spellings")
        if (self.table is None) == (self.rule == "code-table"):
            raise ValueError("a code-table rule names its table and no other rule does")

    def document(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"rule": self.rule}
        if self.accepted_units is not None:
            entry["acceptedUnits"] = list(self.accepted_units)
        if self.table is not None:
            entry["table"] = self.table
        return entry


def as_is(*units: str) -> Conversion:
    """Identity under exactly these (compacted) spellings."""
    return Conversion("none", accepted_units=units)


def code_table(table: str) -> Conversion:
    return Conversion("code-table", table=table)


NONE = Conversion("none")
CELSIUS = Conversion("celsius")
SOIL_TEMPERATURE = Conversion("soil-temperature")
PRECIPITATION_RATE = Conversion("precipitation-rate")
PRECIPITATION_ACCUMULATION = Conversion("precipitation-accumulation")
PASCAL_TO_HECTOPASCAL = Conversion("pascal-to-hectopascal")
METRE_TO_KILOMETRE = Conversion("metre-to-kilometre")
MASS_RATIO_TO_G_PER_KG = Conversion("mass-ratio-to-g-per-kg")
PROPORTION_TO_PERCENT = Conversion("proportion-to-percent")
CLOUD_COVER = Conversion("cloud-cover")
DEGREES_TRUE = Conversion("degrees-true")
GEOPOTENTIAL_TO_METRES = Conversion("geopotential-to-metres")
MASS_DENSITY_TO_MICROGRAMS = Conversion("mass-density-to-micrograms")

# The identity rules, named by what GDAL spells the quantity in. A list is
# every spelling the GRIB driver, eccodes or udunits has been seen to use
# for the one unit; a record under any other spelling is refused rather
# than quantized in the wrong unit.
MASS_RATE = as_is("kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1")
RADIATIVE_FLUX = as_is("w/m^2", "w/m2", "wm^-2", "wm-2")
SPEED = as_is("m/s", "m/sec", "ms-1", "ms^-1", "mps")
ENERGY_PER_MASS = as_is("j/kg", "jkg-1", "jkg^-1")
COLUMN_MASS = as_is("kg/m^2", "kg/m2", "kgm^-2", "kgm-2", "kgm**-2", "mm", "kgs-1m-1")
METRES = as_is("m", "metre", "meter", "metres", "meters")
SECONDS = as_is("s", "sec", "second", "seconds")
REFLECTIVITY = as_is("db", "dbz")
VERTICAL_VELOCITY = as_is("pa/s", "pas-1", "pas^-1")
GEOPOTENTIAL_METRES = as_is("gpm", "m")
PERCENT = as_is("%")
DIMENSIONLESS = as_is("numeric", "1", "-", "")
# An index MRMS publishes without a unit; its GRIB table spells it
# "[non-dim]".
NON_DIMENSIONAL = as_is("non-dim")
PARTICULATE = as_is("10^-6g/m^3", "10^-6g/m3", "ug/m^3", "ug/m3", "µg/m^3", "µg/m3", "µg/m³")
SOIL_MOISTURE = as_is("fraction", "proportion", "m^3/m^3", "m3/m3", "m³/m³", "m^3m^-3", "m3m-3", "1", "-", "")

# The spellings of each named rule, compacted. Where a rule takes two
# arithmetics, the spellings of the one that is not identity are named
# apart so the converter can tell them from the rest.
_TEMPERATURE_SPELLINGS: dict[str, str] = {
    "k": "K",
    "kelvin": "K",
    "c": "C",
    "celsius": "C",
    "degc": "C",
    "f": "F",
    "fahrenheit": "F",
    "degf": "F",
}
# How GDAL spells a rate already in millimetres per hour: the MRMS
# radar-derived precipitation rate arrives that way, and takes no scaling.
_MM_PER_HOUR = ("mm/h", "mm/hr")
_KG_PER_M2_S = ("kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1")
# A precipitation accumulation in metres: ECMWF open data's tp, whose
# ECMWF-local parameter GDAL reports under "-".
_METRES_ACCUMULATION = ("-", "m", "")
# One already in millimetres (kg/m²): AIFS writes its run total that way,
# under the WMO 0/1/52 whose GDAL table entry is a rate, so the unit GDAL
# reports carries a per-second the values do not have.
_MM_ACCUMULATION = ("kg/m^2", "kg/m2", "kgm^-2", "kgm-2", "mm", "kg/m^2s", "kg/m^2*s", "kg/m2s", "kg/m2*s")
_PASCALS = ("pa",)
_METRES = ("m", "metre", "meter", "metres", "meters")
_MASS_RATIO = ("kg/kg",)
_PROPORTION = ("proportion", "fraction", "1", "-", "")
_CLOUD_FRACTION = ("-", "1", "fraction", "proportion", "0-1")
_DEGREES_TRUE = ("degreetrue", "degtrue", "degrees", "degree", "deg", "degreestrue")
# The geopotential height NCEP writes (gpm, numerically metres) and the
# geopotential itself (m² s⁻²) ECMWF open data writes, in whichever
# punctuation the driver reports it ("(m^2)/(s^2)" from the GRIB driver,
# "m**2 s**-2" from the header index): the rule's compaction drops ``^``
# too, so both come out one way.
_GEOPOTENTIAL_METRES = ("gpm", "m")
_GEOPOTENTIAL = ("m2/s2", "m2s-2")
_MASS_DENSITY = ("kg/m^3", "kg/m3", "kgm^-3", "kgm-3")

_ACCEPTED: dict[str, tuple[str, ...]] = {
    "celsius": tuple(_TEMPERATURE_SPELLINGS),
    "soil-temperature": tuple(_TEMPERATURE_SPELLINGS),
    "precipitation-rate": _KG_PER_M2_S + _MM_PER_HOUR,
    "precipitation-accumulation": _METRES_ACCUMULATION + _MM_ACCUMULATION,
    "pascal-to-hectopascal": _PASCALS,
    "metre-to-kilometre": _METRES,
    "mass-ratio-to-g-per-kg": _MASS_RATIO,
    "proportion-to-percent": _PROPORTION,
    "cloud-cover": ("%",) + _CLOUD_FRACTION,
    "degrees-true": _DEGREES_TRUE,
    "geopotential-to-metres": _GEOPOTENTIAL_METRES + _GEOPOTENTIAL,
    "mass-density-to-micrograms": _MASS_DENSITY,
}

# What a refusal calls the quantity.
_NOUNS: dict[str, str] = {
    "celsius": "temperature",
    "soil-temperature": "soil temperature",
    "precipitation-rate": "precipitation rate",
    "precipitation-accumulation": "precipitation accumulation",
    "pascal-to-hectopascal": "pressure",
    "metre-to-kilometre": "distance",
    "mass-ratio-to-g-per-kg": "specific humidity",
    "proportion-to-percent": "proportion",
    "cloud-cover": "cloud cover",
    "degrees-true": "direction",
    "geopotential-to-metres": "geopotential height",
    "mass-density-to-micrograms": "mass density",
    "code-table": "code table",
}


def compact_unit(unit: str) -> str:
    """A unit spelling as the rules compare it: lower-cased, whitespace and
    ``*()[]`` removed, ``²`` written ``^2``."""
    return re.sub(r"[\s*()\[\]]", "", unit.strip().lower()).replace("²", "^2")


def _compact_for(rule: str, unit: str) -> str:
    compact = compact_unit(unit)
    if rule in ("celsius", "soil-temperature"):
        return compact.replace("°", "").replace("degrees", "").replace("degree", "")
    if rule == "geopotential-to-metres":
        return compact.replace("^", "")
    if rule == "code-table":
        return compact.replace("codetable", "")
    return compact


def temperature_unit(unit: str) -> str:
    """One of ``K``, ``C``, ``F`` from whichever spelling a temperature
    record reports."""
    compact = _compact_for("celsius", unit)
    if compact not in _TEMPERATURE_SPELLINGS:
        raise ConversionError(f"unsupported temperature unit: {unit or '<missing>'}")
    return _TEMPERATURE_SPELLINGS[compact]


def accepted_units(conversion: Conversion) -> tuple[str, ...] | None:
    """The compacted spellings the rule accepts a record under, or None for
    a ``none`` rule that accepts any. A ``code-table`` rule also accepts
    any spelling that contains its table or ``0=no``."""
    if conversion.rule == "none":
        return conversion.accepted_units
    if conversion.rule == "code-table":
        assert conversion.table is not None
        return ("1", "-", "", conversion.table)
    return _ACCEPTED[conversion.rule]


def accepts(conversion: Conversion, unit: str) -> bool:
    """Whether a record under ``unit`` is one this rule reads."""
    accepted = accepted_units(conversion)
    if accepted is None:
        return True
    compact = _compact_for(conversion.rule, unit)
    if compact in accepted:
        return True
    if conversion.rule == "code-table":
        assert conversion.table is not None
        return conversion.table in compact or "0=no" in compact
    return False


def _refuse(conversion: Conversion, unit: str) -> ConversionError:
    noun = _NOUNS.get(conversion.rule)
    if noun is None:
        return ConversionError(
            f"unsupported unit: {unit or '<missing>'} (expected one of {', '.join(accepted_units(conversion) or ())})"
        )
    return ConversionError(f"unsupported {noun} unit: {unit or '<missing>'}")


def _scaled(conversion: Conversion, unit: str) -> bool:
    """Whether the rule's arithmetic applies under ``unit`` — as opposed to
    the identity the rule's other spellings (or, on a series, the published
    unit itself) take."""
    rule = conversion.rule
    compact = _compact_for(rule, unit)
    if rule == "precipitation-rate":
        return compact not in _MM_PER_HOUR
    if rule == "precipitation-accumulation":
        return compact in _METRES_ACCUMULATION
    if rule == "pascal-to-hectopascal":
        return compact in _PASCALS
    if rule == "metre-to-kilometre":
        return compact in _METRES
    if rule == "mass-ratio-to-g-per-kg":
        return compact in _MASS_RATIO
    if rule == "proportion-to-percent":
        return compact in _PROPORTION
    if rule == "cloud-cover":
        return compact in _CLOUD_FRACTION
    if rule == "geopotential-to-metres":
        return compact in _GEOPOTENTIAL
    if rule == "mass-density-to-micrograms":
        return compact in _MASS_DENSITY
    return False


def expression(conversion: Conversion, unit: str) -> str:
    """The raster expression one record takes under ``unit``: the rule's
    arithmetic alone, no clamp, in GDAL's ``A`` spelling. The two
    inspection paths (``gdal.py``, the native ``inspect.rs``) compare it
    for identity; nothing writes it. Raises for a spelling the rule does
    not accept."""
    if not accepts(conversion, unit):
        raise _refuse(conversion, unit)
    rule = conversion.rule
    if rule in ("celsius", "soil-temperature"):
        normalized = temperature_unit(unit)
        if normalized == "K":
            return "A-273.15"
        if normalized == "F":
            return "(A-32)*5/9"
        return "A"
    if not _scaled(conversion, unit):
        return "A"
    if rule == "precipitation-rate":
        return "A*3600"
    if rule in ("precipitation-accumulation", "mass-ratio-to-g-per-kg"):
        return "A*1000"
    if rule == "pascal-to-hectopascal":
        return "A/100"
    if rule == "metre-to-kilometre":
        return "A/1000"
    if rule in ("proportion-to-percent", "cloud-cover"):
        return "A*100"
    if rule == "mass-density-to-micrograms":
        return "A*1000000000"
    assert rule == "geopotential-to-metres"
    return f"A/{STANDARD_GRAVITY:g}"


def convert(conversion: Conversion, unit: str, values: np.ndarray) -> np.ndarray:
    """One plane's values taken to the published unit, by the rule's
    arithmetic under ``unit``. The operations and their order are part of
    the output and mirrored one for one by the native encoder: a
    temperature's unit is read through :func:`temperature_unit` (the
    soil read-through is the converter's, before this); a direction is
    reduced modulo 360 so a record's 360 lands on the codebook's 0."""
    rule = conversion.rule
    if rule in ("celsius", "soil-temperature"):
        normalized = temperature_unit(unit)
        if normalized == "K":
            values -= 273.15
        elif normalized == "F":
            values = (values - 32.0) * 5.0 / 9.0
        return values
    if rule == "degrees-true":
        return np.mod(values, 360.0)
    if not _scaled(conversion, unit):
        return values
    if rule == "precipitation-rate":
        # kg m⁻² s⁻¹ (a millimetre per second) to mm/h.
        values *= 3600.0
    elif rule == "precipitation-accumulation":
        # Metres of accumulated water to millimetres; the de-accumulation
        # into a rate is the converter's, against the previous frame.
        values *= 1000.0
    elif rule == "pascal-to-hectopascal":
        values /= 100.0
    elif rule == "metre-to-kilometre":
        values /= 1000.0
    elif rule == "mass-ratio-to-g-per-kg":
        values *= 1000.0
    elif rule in ("proportion-to-percent", "cloud-cover"):
        values *= 100.0
    elif rule == "mass-density-to-micrograms":
        values *= 1e9
    else:
        assert rule == "geopotential-to-metres"
        values /= STANDARD_GRAVITY
    return values


# ---------------------------------------------------------------------------
# Ensemble products


@dataclass(frozen=True)
class EnsembleIdentity:
    """Which statistic of an ensemble a field is: the derived forecast of
    every member (code table 4.7: 0 the unweighted mean, 4 the spread), a
    percentile of the members' distribution, or the share of members past
    a threshold (code table 4.9 type and its limits) — what GRIB2 product
    definition templates 4.2 / 4.12, 4.6 / 4.10 and 4.5 / 4.9 carry beyond
    the parameter, and what the metadata's ``ensemble`` block writes
    (docs/bundle.md §"Ensemble"). Exactly one of the three shapes is set;
    which one says which templates a record of it is written in.

    A limit is a ``(scaleFactor, scaledValue)`` pair, ``scaledValue ×
    10^−scaleFactor`` in the GRIB2 unit of the parameter, or None. Which
    limits a probability carries is fixed by its type
    (:meth:`probability`), whatever the record wrote in the octets the type
    does not use: CMA writes the threshold into both."""

    derived_forecast: int | None = None
    percentile: int | None = None
    probability_type: int | None = None
    lower_limit: tuple[int, int] | None = None
    upper_limit: tuple[int, int] | None = None

    #: Code table 4.9 types that read the lower limit, the upper, or both.
    LOWER_TYPES = (0, 3, 5)
    UPPER_TYPES = (1, 4)
    BOTH_TYPES = (2,)

    @classmethod
    def derived(cls, code: int) -> EnsembleIdentity:
        return cls(derived_forecast=code)

    @classmethod
    def percentile_of(cls, value: int) -> EnsembleIdentity:
        return cls(percentile=value)

    @classmethod
    def probability(
        cls, probability_type: int, lower: tuple[int, int] | None, upper: tuple[int, int] | None
    ) -> EnsembleIdentity:
        """A probability with the limits its type does not use dropped."""
        uses_lower = probability_type in cls.LOWER_TYPES + cls.BOTH_TYPES
        uses_upper = probability_type in cls.UPPER_TYPES + cls.BOTH_TYPES
        return cls(
            probability_type=probability_type,
            lower_limit=lower if uses_lower else None,
            upper_limit=upper if uses_upper else None,
        )

    @property
    def kind(self) -> str:
        """``derived``, ``percentile`` or ``probability``."""
        if self.derived_forecast is not None:
            return "derived"
        if self.percentile is not None:
            return "percentile"
        return "probability"

    @property
    def templates(self) -> tuple[int, int]:
        """The product definition templates of the shape: instantaneous,
        then over a time interval."""
        return ENSEMBLE_TEMPLATES[self.kind]

    def validate(self) -> None:
        """Refuse an identity no metadata block can carry: not exactly one
        shape, a value out of its range, a limit the type needs missing."""
        shapes = (
            self.derived_forecast is not None,
            self.percentile is not None,
            self.probability_type is not None,
        )
        if sum(shapes) != 1:
            raise ValueError("an ensemble identity has exactly one shape")
        if self.derived_forecast is not None and not 0 <= self.derived_forecast <= 255:
            raise ValueError("derivedForecast is out of range")
        if self.percentile is not None and not 0 <= self.percentile <= 100:
            raise ValueError("percentileValue is out of range")
        if self.probability_type is None:
            if self.lower_limit is not None or self.upper_limit is not None:
                raise ValueError("only a probability carries limits")
            return
        if not 0 <= self.probability_type <= 5:
            raise ValueError("probabilityType is out of range")
        uses_lower = self.probability_type in self.LOWER_TYPES + self.BOTH_TYPES
        uses_upper = self.probability_type in self.UPPER_TYPES + self.BOTH_TYPES
        for limit, used in ((self.lower_limit, uses_lower), (self.upper_limit, uses_upper)):
            if (limit is not None) != used:
                raise ValueError("a probability carries exactly the limits its type uses")
            if limit is not None and not (-127 <= limit[0] <= 127 and 0 <= limit[1] <= 0xFFFFFFFE):
                raise ValueError("a probability limit is out of range")

    def metadata(self) -> dict[str, Any]:
        """The schema v3 ``ensemble`` block, in its key order."""
        if self.derived_forecast is not None:
            return {"derivedForecast": self.derived_forecast}
        if self.percentile is not None:
            return {"percentileValue": self.percentile}
        lower = self.lower_limit or (None, None)
        upper = self.upper_limit or (None, None)
        return {
            "probabilityType": self.probability_type,
            "scaleFactorOfLowerLimit": lower[0],
            "scaledValueOfLowerLimit": lower[1],
            "scaleFactorOfUpperLimit": upper[0],
            "scaledValueOfUpperLimit": upper[1],
        }


#: Product definition templates of each ensemble shape, instantaneous then
#: over a time interval.
ENSEMBLE_TEMPLATES: dict[str, tuple[int, int]] = {"derived": (2, 12), "percentile": (6, 10), "probability": (5, 9)}
#: Every template that carries an ensemble identity: a record in one is
#: accepted by a ``template-ensemble`` rule alone.
ENSEMBLE_TEMPLATE_NUMBERS = frozenset(number for pair in ENSEMBLE_TEMPLATES.values() for number in pair)
ENSEMBLE_INTERVAL_TEMPLATES = frozenset(pair[1] for pair in ENSEMBLE_TEMPLATES.values())


@dataclass(frozen=True)
class EnsembleRecord:
    """What a ``template-ensemble`` rule reads of one record, however it
    was read (GDAL's assembled template, :mod:`xuebuild.gdal`; the header
    index, :mod:`xuebuild.grib2`). ``interval_hours`` and ``statistical``
    are the one time range of an interval template, in hours, and its code
    table 4.10 process; ``interval_hours`` is None where the record has
    another number of ranges or another unit."""

    template: int
    discipline: int
    category: int
    number: int
    level_type: int
    level_value: float | None
    ensemble: EnsembleIdentity
    interval_hours: int | None = None
    statistical: int | None = None


def ensemble_rule_accepts(rule: GdalRule, record: EnsembleRecord) -> bool:
    """Whether a ``template-ensemble`` rule accepts a record
    (docs/registry.md §match): the template of the rule's shape, its
    interval form exactly when the rule names ``hours``; the parameter,
    the surface (a None ``level_value`` accepting any) and the ensemble
    identity; and of an interval, one range in hours of the rule's length
    and process."""
    assert rule.rule == "template-ensemble" and rule.ensemble is not None
    instant, interval = rule.ensemble.templates
    if record.template != (instant if rule.hours is None else interval):
        return False
    if (record.discipline, record.category, record.number, record.level_type) != (
        rule.discipline,
        rule.category,
        rule.number,
        rule.level_type,
    ):
        return False
    if rule.level_value is not None and record.level_value != rule.level_value:
        return False
    if record.ensemble != rule.ensemble:
        return False
    if rule.hours is not None:
        return record.interval_hours == rule.hours and record.statistical == rule.statistical
    return True


# ---------------------------------------------------------------------------
# GDAL band-matching rules


@dataclass(frozen=True)
class GdalRule:
    """One way a variable's record is recognised among a GRIB's bands as
    GDAL describes them (docs/registry.md §match). The parameters a rule
    takes are its own; the rest stay at their defaults and are not
    written. Evaluated by ``gdal._band_matches``."""

    rule: str
    elements: tuple[str, ...] = ()
    element: str = ""
    metres: int | None = None
    hpa: int | None = None
    short_name: str = ""
    phrases: tuple[str, ...] = ()
    category: int | None = None
    number: int | None = None
    discipline: int | None = None
    level_type: int | None = None
    level_value: float | None = None
    ensemble: EnsembleIdentity | None = None
    hours: int | None = None
    statistical: int | None = None

    def __post_init__(self) -> None:
        if self.rule not in MATCH_RULES:
            raise ValueError(f"unknown match rule: {self.rule}")
        if self.rule == "template-ensemble":
            if self.ensemble is None or None in (self.discipline, self.category, self.number, self.level_type):
                raise ValueError("a template-ensemble rule names its parameter, surface and ensemble identity")
            self.ensemble.validate()
            if (self.hours is None) != (self.statistical is None):
                raise ValueError("a template-ensemble rule names an interval's hours and process together")
            if self.hours is not None and self.hours <= 0:
                raise ValueError("a template-ensemble rule's interval is at least an hour")

    def document(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"rule": self.rule}
        if self.rule in ("surface", "mean-sea-level", "geopotential-surface"):
            entry["elements"] = list(self.elements)
        elif self.rule == "height":
            entry["element"] = self.element
            entry["metres"] = self.metres
        elif self.rule == "entire-atmosphere":
            entry["element"] = self.element
        elif self.rule == "isobaric":
            entry["element"] = self.element
            entry["hpa"] = self.hpa
        elif self.rule == "cloud-layer":
            entry["element"] = self.element
            entry["shortName"] = self.short_name
            entry["phrases"] = list(self.phrases)
        elif self.rule == "local-parameter":
            entry["category"] = self.category
            entry["number"] = self.number
        elif self.rule == "phrase":
            entry["element"] = self.element
            entry["phrases"] = list(self.phrases)
        elif self.rule == "template-ensemble":
            assert self.ensemble is not None
            entry["discipline"] = self.discipline
            entry["category"] = self.category
            entry["number"] = self.number
            entry["levelType"] = self.level_type
            entry["levelValue"] = self.level_value
            entry["ensemble"] = self.ensemble.metadata()
            entry["hours"] = self.hours
            entry["statistical"] = self.statistical
        return entry


def surface(*elements: str) -> GdalRule:
    """One of ``elements`` on the ground or water surface."""
    return GdalRule("surface", elements=elements)


def height(element: str, metres: int) -> GdalRule:
    """The element on the ``metres`` m above-ground surface."""
    return GdalRule("height", element=element, metres=metres)


def entire_atmosphere(element: str) -> GdalRule:
    return GdalRule("entire-atmosphere", element=element)


def mean_sea_level(*elements: str) -> GdalRule:
    return GdalRule("mean-sea-level", elements=elements)


def isobaric(element: str, hpa: int) -> GdalRule:
    return GdalRule("isobaric", element=element, hpa=hpa)


def cloud_layer(element: str, short_name: str, *phrases: str) -> GdalRule:
    return GdalRule("cloud-layer", element=element, short_name=short_name, phrases=phrases)


def template() -> GdalRule:
    """Template 4.0's assembled values against the entry's identity or an
    alternate."""
    return GdalRule("template")


def template_aerosol() -> GdalRule:
    return GdalRule("template-aerosol")


def template_ensemble(
    category: int,
    number: int,
    level_type: int,
    level_value: float | None,
    ensemble: EnsembleIdentity,
    *,
    discipline: int = 0,
    hours: int | None = None,
    statistical: int | None = None,
) -> GdalRule:
    """One ensemble product: the parameter on its surface (a None
    ``level_value`` accepting any, a missing one included), the ensemble
    identity, and for a statistic over an interval its length in hours and
    code table 4.10 process."""
    return GdalRule(
        "template-ensemble",
        discipline=discipline,
        category=category,
        number=number,
        level_type=level_type,
        level_value=level_value,
        ensemble=ensemble,
        hours=hours,
        statistical=statistical,
    )


def template_rank() -> GdalRule:
    return GdalRule("template-rank")


def template_mrms() -> GdalRule:
    return GdalRule("template-mrms")


def total_precipitation() -> GdalRule:
    return GdalRule("total-precipitation")


def local_parameter(category: int, number: int) -> GdalRule:
    """A centre-local parameter GDAL's tables do not know, named by the
    ``cat <category>, subcat <number>`` of its comment on the ground
    surface."""
    return GdalRule("local-parameter", category=category, number=number)


def phrase(element: str, *phrases: str) -> GdalRule:
    return GdalRule("phrase", element=element, phrases=phrases)


def geopotential_surface(*elements: str) -> GdalRule:
    return GdalRule("geopotential-surface", elements=elements)
