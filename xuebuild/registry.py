"""The registry document: the variables, bundles and sources an encoder
builds, as one JSON document (docs/registry.md).

The Python tables (``variables.py``, ``quantize.py``, ``sources.py`` and the
bundle tables of ``binconvert.py``) are the authors; this module is their
one serialization. The native encoder takes the document in place of a
table of its own, so a change to a table is a change to the document and
nothing has to be released for it; the committed snapshot
``tests/fixtures/registry.json`` is held to :func:`document` by a test.

The half of a variable entry that describes the published field (``label``,
``unit``, ``parameter``, ``producer``, ``aerosol``, ``quantization``) is the
variable block a store's ``attributes.xue`` carries, key for key: the
encoders copy it, the readers read it. The rest is how to obtain the field
and what to do with it, and never reaches a store.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from .binconvert import (
    COMPOSITE_BUNDLES,
    DERIVED_SCALARS,
    DERIVED_VECTORS,
    PEAK_DECIMATION_IDS,
    PRECIPITATION_INPUT_IDS,
    PRESSURE_BUNDLE_IDS,
    RAW_VARIABLE_IDS,
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    VOLUME_BUNDLES,
    WAVE_VECTOR_BUNDLE_IDS,
    cat_level,
    theta_e_level,
)
from .errors import ConversionError
from .quantize import PROFILES
from .satellite.platforms import PLATFORMS
from .satellite.producers import PRODUCERS
from .sources import SOURCES
from .variables import (
    AEROSOL_VARIABLE_IDS,
    SOIL_MOISTURE_IDS,
    SOIL_TEMPERATURE_IDS,
    VARIABLES,
    VariableSpec,
    isobaric_variable,
    reflectivity_level,
)

#: The document's own schema. A reader refuses a document whose schema it
#: does not implement; a field added without changing what an existing
#: reader must do keeps the number.
REGISTRY_SCHEMA = 1

# ---------------------------------------------------------------------------
# Rule names. Each value below is a name out of a closed set the encoders
# implement (docs/registry.md §"Closed sets"); a document naming anything
# else is refused before any input is read.

_RATE_UNITS = ["kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1"]
_FLUX_UNITS = ["w/m^2", "w/m2", "wm^-2", "wm-2"]
_SPEED_UNITS = ["m/s", "m/sec", "ms-1", "ms^-1", "mps"]
_ENERGY_UNITS = ["j/kg", "jkg-1", "jkg^-1"]
_COLUMN_MASS_UNITS = ["kg/m^2", "kg/m2", "kgm^-2", "kgm-2", "kgm**-2", "mm", "kgs-1m-1"]
_METRE_UNITS = ["m", "metre", "meter", "metres", "meters"]
_SECOND_UNITS = ["s", "sec", "second", "seconds"]
_REFLECTIVITY_UNITS = ["db", "dbz"]
_VERTICAL_VELOCITY_UNITS = ["pa/s", "pas-1", "pas^-1"]
_GEOPOTENTIAL_METRE_UNITS = ["gpm", "m"]
_PERCENT_UNITS = ["%"]
_DIMENSIONLESS_UNITS = ["numeric", "1", "-", ""]
_PARTICULATE_UNITS = ["10^-6g/m^3", "10^-6g/m3", "ug/m^3", "ug/m3", "µg/m^3", "µg/m3", "µg/m³"]
_SOIL_MOISTURE_UNITS = ["fraction", "proportion", "m^3/m^3", "m3/m3", "m³/m³", "m^3m^-3", "m3m-3", "1", "-", ""]


def _conversion(spec: VariableSpec) -> dict[str, Any]:
    """The unit rule of one variable: the arithmetic that takes the record's
    values to ``unit``, and the unit spellings a record may carry."""
    variable_id = spec.id
    isobaric = isobaric_variable(variable_id)
    family = isobaric[0] if isobaric is not None else None

    if variable_id in SOIL_TEMPERATURE_IDS:
        return {"rule": "soil-temperature"}
    if variable_id in ("tmp2m", "dpt2m", "aptmp2m", "tmpsfc") or family == "tmp":
        return {"rule": "celsius"}
    if variable_id == "prate":
        return {"rule": "precipitation-rate"}
    if variable_id == "tp":
        return {"rule": "precipitation-accumulation"}
    if variable_id == "prmsl":
        return {"rule": "pascal-to-hectopascal"}
    if variable_id == "vis":
        return {"rule": "metre-to-kilometre"}
    if family == "spfh":
        return {"rule": "mass-ratio-to-g-per-kg"}
    if variable_id == "icec":
        return {"rule": "proportion-to-percent"}
    if variable_id in ("tcdc", "lcdc", "mcdc", "hcdc"):
        return {"rule": "cloud-cover"}
    if variable_id in ("dirpw", "wvdir", "swdir"):
        return {"rule": "degrees-true"}
    if variable_id == "orog":
        return {"rule": "geopotential-to-metres"}
    if variable_id in ("crain", "cfrzr", "cicep", "csnow"):
        return {"rule": "code-table", "table": "4.222"}
    if variable_id == "ptype":
        return {"rule": "code-table", "table": "4.201"}

    accepted: list[str] | None = None
    if variable_id == "prate_ave":
        accepted = _RATE_UNITS
    elif variable_id == "dswrf":
        accepted = _FLUX_UNITS
    elif variable_id in ("ugrd10m", "vgrd10m", "ugrd80m", "vgrd80m", "ugrd100m", "vgrd100m", "gust") or family in (
        "ugrd",
        "vgrd",
    ):
        accepted = _SPEED_UNITS
    elif variable_id in ("cape", "cin"):
        accepted = _ENERGY_UNITS
    elif variable_id == "pwat":
        accepted = _COLUMN_MASS_UNITS
    elif variable_id in ("hpbl", "icetk", "htsgw", "wvhgt", "swhgt"):
        accepted = _METRE_UNITS
    elif variable_id in ("perpw", "wvper", "swper"):
        accepted = _SECOND_UNITS
    elif variable_id == "cref" or reflectivity_level(variable_id) is not None:
        accepted = _REFLECTIVITY_UNITS
    elif family == "vvel":
        accepted = _VERTICAL_VELOCITY_UNITS
    elif family == "hgt":
        accepted = _GEOPOTENTIAL_METRE_UNITS
    elif family == "rh":
        accepted = _PERCENT_UNITS
    elif variable_id in AEROSOL_VARIABLE_IDS:
        accepted = _DIMENSIONLESS_UNITS if variable_id.startswith("aod") else _PARTICULATE_UNITS
    elif variable_id in SOIL_MOISTURE_IDS:
        accepted = _SOIL_MOISTURE_UNITS
    rule: dict[str, Any] = {"rule": "none"}
    if accepted is not None:
        rule["acceptedUnits"] = list(accepted)
    return rule


# The elements GDAL names another centre's spelling of a surface quantity
# by, accepted beside the registry's own (`gdal._SURFACE_ELEMENT_ALIASES`).
_SURFACE_ELEMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "TMP": ("SKINT",),
    "PERPW": ("PWPER",),
    "DIRPW": ("WWSDIR",),
}

_CLOUD_LAYERS: dict[str, tuple[str, list[str]]] = {
    "lcdc": ("0-LCY", ["low cloud"]),
    "mcdc": ("0-MCY", ["middle cloud", "medium cloud"]),
    "hcdc": ("0-HCY", ["high cloud"]),
}


def _gdal_rules(spec: VariableSpec) -> list[dict[str, Any]]:
    """How one variable's record is recognised among a GRIB's bands as GDAL
    describes them (``gdal._band_matches``): a list of rules, any one of
    which accepts the band. Empty for a field no GRIB band ever carries
    (a derived field, a series-file variable)."""
    variable_id = spec.id
    element = spec.grib_element

    def surface(elements: tuple[str, ...] | None = None) -> dict[str, Any]:
        names = elements or (element, *_SURFACE_ELEMENT_ALIASES.get(element, ()))
        return {"rule": "surface", "elements": list(names)}

    def height(metres: int) -> dict[str, Any]:
        return {"rule": "height", "element": element, "metres": metres}

    if variable_id in AEROSOL_VARIABLE_IDS:
        return [{"rule": "template-aerosol"}]
    if variable_id in ("tmp2m", "dpt2m", "aptmp2m"):
        return [height(2)]
    if variable_id == "prate":
        return [surface(("PRATE",)), {"rule": "template-mrms"}]
    if variable_id == "prate_ave":
        return [surface(("PRATE",))]
    if variable_id == "tp":
        return [{"rule": "total-precipitation"}]
    if variable_id == "gust":
        return [surface(), height(10)]
    if variable_id == "cape":
        return [surface(), {"rule": "phrase", "element": "CAPE", "phrases": ["most unstable", "mudl"]}]
    if variable_id == "pwat":
        return [{"rule": "entire-atmosphere", "element": element}]
    if variable_id in SOIL_TEMPERATURE_IDS or variable_id in SOIL_MOISTURE_IDS:
        return [{"rule": "template"}]
    if variable_id in (
        "dswrf",
        "vis",
        "tmpsfc",
        "icec",
        "icetk",
        "htsgw",
        "perpw",
        "dirpw",
        "wvhgt",
        "wvper",
        "wvdir",
        "cin",
        "hpbl",
        "crain",
        "cfrzr",
        "cicep",
        "csnow",
    ):
        return [surface()]
    if variable_id in ("swhgt", "swper", "swdir"):
        return [{"rule": "template-rank"}]
    if variable_id == "tcdc":
        return [
            {"rule": "entire-atmosphere", "element": element},
            {"rule": "local-parameter", "category": 6, "number": 192},
            surface(("TCDC",)),
        ]
    if variable_id == "cref":
        return [{"rule": "entire-atmosphere", "element": element}, {"rule": "template-mrms"}]
    if reflectivity_level(variable_id) is not None:
        return [{"rule": "template-mrms"}]
    if variable_id in _CLOUD_LAYERS:
        short_name, phrases = _CLOUD_LAYERS[variable_id]
        return [{"rule": "cloud-layer", "element": element, "shortName": short_name, "phrases": phrases}]
    if variable_id in ("ugrd10m", "vgrd10m"):
        return [height(10)]
    if variable_id in ("ugrd80m", "vgrd80m"):
        return [height(80)]
    if variable_id in ("ugrd100m", "vgrd100m"):
        return [height(100)]
    if variable_id == "prmsl":
        return [{"rule": "mean-sea-level", "elements": ["PRMSL", "PRES", "MSLMA"]}]
    if variable_id == "orog":
        return [surface(("HGT",)), {"rule": "geopotential-surface", "elements": ["GP", "Z"]}]
    isobaric = isobaric_variable(variable_id)
    if isobaric is not None and element:
        return [{"rule": "isobaric", "element": element, "hpa": isobaric[1]}]
    return []


def _companions(variable_id: str) -> list[str]:
    if variable_id in PRESSURE_BUNDLE_IDS:
        return []
    return ["poster", "video"] if variable_id in VIDEO_VARIABLE_IDS else ["poster"]


def _match(spec: VariableSpec) -> dict[str, Any]:
    return {
        "gribElement": spec.grib_element,
        "indexField": spec.index_field,
        "excludedIndexPhrases": list(spec.excluded_index_phrases),
        "indexQualifier": spec.index_qualifier,
        "alternateIndexFields": list(spec.alternate_index_fields),
        "ecmwfParam": spec.ecmwf_param,
        "ecmwfAlternateParams": list(spec.ecmwf_alternate_params),
        "openMeteo": spec.open_meteo,
        "mrmsProduct": spec.mrms_product,
        "gdalUnit": spec.gdal_unit,
        "statistical": spec.grib2_statistical,
        "aliases": [list(triple) for triple in spec.grib2_aliases],
        "alternates": [
            {
                "discipline": alternate.discipline,
                "category": alternate.category,
                "number": alternate.number,
                "levelType": alternate.level_type,
                "levelValue": alternate.level_value,
                "statistical": alternate.statistical,
                "gdalUnit": alternate.gdal_unit,
            }
            for alternate in spec.grib2_alternates
        ],
        "gdal": _gdal_rules(spec),
    }


def variable_entry(spec: VariableSpec) -> dict[str, Any]:
    """One variable as the document describes it."""
    entry: dict[str, Any] = {
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
    }
    if spec.producer_id is not None:
        entry["producer"] = {"id": spec.producer_id}
    if spec.grib2_aerosol is not None:
        entry["aerosol"] = spec.grib2_aerosol.metadata()
    quantization = {
        profile: codebooks[spec.id].metadata() for profile, codebooks in PROFILES.items() if spec.id in codebooks
    }
    if quantization:
        entry["quantization"] = quantization
    entry.update(
        {
            "valueRange": list(spec.value_range),
            "fillValues": list(spec.fill_values),
            "static": spec.static,
            "predictor": "raw" if spec.id in RAW_VARIABLE_IDS else "delta",
            "decimation": "peak" if spec.id in PEAK_DECIMATION_IDS else "sample",
            "companions": _companions(spec.id),
            "conversion": _conversion(spec),
            "match": _match(spec),
        }
    )
    return entry


def _bundles() -> dict[str, Any]:
    bundles: dict[str, Any] = {}
    for bundle_id, members in VECTOR_BUNDLES.items():
        entry: dict[str, Any] = {"kind": "vector", "members": list(members)}
        if bundle_id in DERIVED_VECTORS:
            entry["derivation"] = {
                "kind": "wave-vector" if bundle_id in WAVE_VECTOR_BUNDLE_IDS else "vapour-flux",
                "inputs": list(DERIVED_VECTORS[bundle_id]),
            }
        bundles[bundle_id] = entry
    for bundle_id, members in COMPOSITE_BUNDLES.items():
        producer = PRODUCERS[bundle_id]
        # An input the recipe has a stand-in for on an imager that lacks
        # it: read when the source fetches it, left out when it does not.
        # A platform the recipe cannot run on at all drops nothing here.
        optional: set[str] = set()
        for platform in PLATFORMS.values():
            try:
                optional.update(set(producer.inputs) - set(producer.inputs_for(platform)))
            except ConversionError:
                continue
        bundles[bundle_id] = {
            "kind": "composite",
            "members": list(members),
            "producer": producer.id,
            "inputs": list(producer.inputs),
            "optionalInputs": [input_id for input_id in producer.inputs if input_id in optional],
        }
    for bundle_id, members in VOLUME_BUNDLES.items():
        bundles[bundle_id] = {"kind": "volume", "members": list(members)}
    for bundle_id, inputs in DERIVED_SCALARS.items():
        derivation: dict[str, Any] = {"inputs": list(inputs)}
        if theta_e_level(bundle_id) is not None:
            derivation["kind"] = "theta-e"
            derivation["levelHpa"] = theta_e_level(bundle_id)
        elif cat_level(bundle_id) is not None:
            derivation["kind"] = "clear-air-turbulence"
            derivation["levelHpa"] = cat_level(bundle_id)
        else:
            derivation["kind"] = "precipitation-type"
        bundles[bundle_id] = {"kind": "scalar", "derivation": derivation}
    bundles["prate"] = {
        "kind": "scalar",
        "derivation": {"kind": "precipitation-rate", "inputs": list(PRECIPITATION_INPUT_IDS)},
    }
    return dict(sorted(bundles.items()))


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _camel_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel(key): _camel_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_camel_keys(item) for item in value]
    return value


def _sources() -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for source_id, spec in SOURCES.items():
        assert is_dataclass(spec)
        entry = _camel_keys(asdict(spec))
        entry["bands"] = [[variable_id, band] for variable_id, band in entry["bands"]]
        sources[source_id] = entry
    return sources


def document() -> dict[str, Any]:
    """The whole registry document."""
    return {
        "registrySchema": REGISTRY_SCHEMA,
        "variables": {variable_id: variable_entry(spec) for variable_id, spec in VARIABLES.items()},
        "bundles": _bundles(),
        "sources": _sources(),
    }


def document_json() -> str:
    """The document as the snapshot and the native encoder take it."""
    return json.dumps(document(), indent=2, ensure_ascii=False) + "\n"
