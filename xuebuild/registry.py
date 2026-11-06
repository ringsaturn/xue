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
from functools import lru_cache
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
        "dwdParam": spec.dwd_param,
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


#: Source fields the fetch alone reads. The native encoder never fetches,
#: so they stay out of the document.
FETCH_ONLY_SOURCE_FIELDS = ("feed", "packing")


def _companion_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # Schema 1 spells a companion family's ``repack`` switch, which the
    # native encoder still parses. Repacking is the source's ``packing``
    # now and never a family's, so the key is always false.
    head = {key: entry[key] for key in ("id", "variableIds")}
    return {**head, "repack": False, **{key: value for key, value in entry.items() if key not in head}}


def _sources() -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for source_id, spec in SOURCES.items():
        assert is_dataclass(spec)
        fields = {key: value for key, value in asdict(spec).items() if key not in FETCH_ONLY_SOURCE_FIELDS}
        entry = _camel_keys(fields)
        entry["bands"] = [[variable_id, band] for variable_id, band in entry["bands"]]
        entry["companionFiles"] = [_companion_entry(companion) for companion in entry["companionFiles"]]
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


def _source_slice(whole: dict[str, Any], source_id: str) -> dict[str, Any]:
    """The part of a document one source reaches: the source, the bundles it
    publishes and every variable those read."""
    source = whole["sources"][source_id]
    bundles = whole["bundles"]
    pending = [
        id_
        for key in (
            "inputVariableIds",
            "bundleScalarIds",
            "bundleVectorIds",
            "bundleCompositeIds",
            "bundleVolumeIds",
            "coreBundleIds",
            "seriesBundleIds",
        )
        for id_ in source[key]
    ]
    reached: set[str] = set()
    while pending:
        id_ = pending.pop()
        if id_ in reached:
            continue
        reached.add(id_)
        bundle = bundles.get(id_)
        if bundle is not None:
            pending += bundle.get("members", [])
            pending += bundle.get("inputs", [])
            pending += bundle.get("optionalInputs", [])
            pending += bundle.get("derivation", {}).get("inputs", [])
    return {
        "registrySchema": whole["registrySchema"],
        "variables": {id_: entry for id_, entry in whole["variables"].items() if id_ in reached},
        "bundles": {id_: entry for id_, entry in bundles.items() if id_ in reached},
        "sources": {source_id: source},
    }


@lru_cache(maxsize=None)
def source_document_json(source_id: str) -> str:
    """The document a build of one source hands the wheel. A wheel refuses a
    document whole when it names a rule the wheel lacks, so a build carries
    only its own source's part: a rule one source needs before a wheel
    implements it stops that source and leaves the others building."""
    return json.dumps(_source_slice(document(), source_id), indent=2, ensure_ascii=False) + "\n"


_SHELL_HEADER = (
    "// The registry as the shell reads it (docs/registry.md, \"The shell's copy\").\n"
    "// Generated by `xue registry --shell web/src/registry.ts`; do not edit.\n"
)


def _shell_variable(entry: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {"unit": entry["unit"], "parameter": entry["parameter"]}
    for key in ("producer", "aerosol"):
        if key in entry:
            trimmed[key] = entry[key]
    trimmed["quality"] = entry["quantization"]["quality"]
    return trimmed


def shell_document() -> dict[str, Any]:
    """What the shell needs of the document: identities, the one codebook
    profile its chart ranges are drawn over, bundle members and source
    identities, plus the family lists its rail groups tiles by, the contour
    intervals the pressure codebooks were placed around and the storm
    registry. No fetch-side rules and no prose."""
    from . import quantize  # noqa: PLC0415
    from . import variables as tables  # noqa: PLC0415
    from .tc.registry import registry_document as tc_document  # noqa: PLC0415

    whole = document()
    return {
        "registrySchema": whole["registrySchema"],
        "isobaricLevels": list(tables.ISOBARIC_LEVELS_HPA),
        "catLevels": list(tables.CAT_LEVELS_HPA),
        "families": {
            "surface": list(quantize.SURFACE_VARIABLE_IDS),
            "ocean": list(tables.OCEAN_VARIABLE_IDS + tables.WAVE_PARTITION_IDS),
            "soil": list(tables.SOIL_VARIABLE_IDS),
            "aerosol": list(tables.AEROSOL_VARIABLE_IDS),
            "smokeDust": list(tables.SMOKE_DUST_VARIABLE_IDS),
            "river": list(tables.RIVER_VARIABLE_IDS),
            "pressure": list(quantize.PRESSURE_VARIABLE_IDS),
        },
        "contours": {
            variable_id: {
                "interval": quantize.CONTOUR_INTERVALS[variable_id],
                **(
                    {"emphasisInterval": quantize.EMPHASIS_INTERVALS[variable_id]}
                    if variable_id in quantize.EMPHASIS_INTERVALS
                    else {}
                ),
                **(
                    {"emphasisContours": list(quantize.EMPHASIS_CONTOURS[variable_id])}
                    if variable_id in quantize.EMPHASIS_CONTOURS
                    else {}
                ),
            }
            for variable_id in quantize.PRESSURE_VARIABLE_IDS
        },
        "variables": {
            variable_id: _shell_variable(entry)
            for variable_id, entry in whole["variables"].items()
            if "quantization" in entry
        },
        "bundles": {
            bundle_id: {"kind": entry["kind"], "members": entry["members"]}
            for bundle_id, entry in whole["bundles"].items()
            if entry.get("members")
        },
        "sources": {
            source_id: {
                "label": entry["manifestModel"],
                "product": entry["product"],
                "latestFilename": entry["latestFilename"],
                "observation": entry["observation"],
                "coreBundleIds": entry["coreBundleIds"],
                "cadenceSeconds": entry["cadenceSeconds"],
            }
            for source_id, entry in whole["sources"].items()
        },
        "tc": tc_document(),
    }


def _shell_value(value: Any, depth: int) -> str:
    # Two levels open, then one entry per line: a moved codebook is one
    # line of the diff.
    if depth < 2 and isinstance(value, dict) and value:
        pad = "  " * (depth + 1)
        items = ",\n".join(
            f"{pad}{json.dumps(key, ensure_ascii=False)}: {_shell_value(item, depth + 1)}" for key, item in value.items()
        )
        return "{\n" + items + ",\n" + "  " * depth + "}"
    return json.dumps(value, ensure_ascii=False)


def shell_module() -> str:
    """The shell's copy, committed as ``web/src/registry.ts``: a TypeScript
    module rather than JSON, so the ids reach the type checker as literal
    unions (a JSON import widens every value to ``string``)."""
    return _SHELL_HEADER + "export const REGISTRY = " + _shell_value(shell_document(), 0) + " as const;\n"
