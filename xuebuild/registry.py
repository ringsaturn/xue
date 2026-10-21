"""The registry document: the variables, bundles and sources an encoder
builds, as one JSON document (docs/registry.md).

The Python tables (``variables.py``, ``bundles.py``, ``quantize.py`` and
``sources.py``) are the authors; this module is their
one serialization. The native encoder takes the document in place of a
table of its own, so a change to a table is a change to the document and
nothing has to be released for it; the committed snapshot
``rust/xue/registry.json`` is held to :func:`document` by a test.

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

from .bundles import BUNDLES
from .quantize import PROFILES
from .sources import SOURCES
from .variables import VARIABLES, VariableSpec

#: The document's own schema. A reader refuses a document whose schema it
#: does not implement; a field added without changing what an existing
#: reader must do keeps the number.
REGISTRY_SCHEMA = 1


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
        "gdal": [rule.document() for rule in spec.gdal_rules or ()],
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
            "predictor": spec.predictor,
            "decimation": spec.decimation,
            "companions": list(spec.companions),
            "conversion": spec.conversion.document(),
            "match": _match(spec),
        }
    )
    return entry


def _bundles() -> dict[str, Any]:
    return {bundle_id: spec.document() for bundle_id, spec in sorted(BUNDLES.items())}


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
