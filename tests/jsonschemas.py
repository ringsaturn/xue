"""The normative shapes of what Xue publishes, ``docs/schemas/*.schema.json``,
as the tests hold the writers to them: the point products' files, and the
grid contracts (the run manifest, bundle metadata, and the documents of a
Zarr store). The writers' own validators keep only what a schema cannot
say (row order, byte spans, sums, CRCs, the lowest version that fits);
everything else a file must be is here, checked with the reference
implementation of JSON Schema 2020-12 (``jsonschema``, a dev dependency
only). A schema may ``$ref`` another by its file name; every schema is
registered under its ``$id``, so those resolve without a network."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs" / "schemas"


@cache
def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


@cache
def _registry() -> Registry:
    resources = []
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        schema = _schema(path.name.removesuffix(".schema.json"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@cache
def validator(name: str) -> Draft202012Validator:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry())


def errors(name: str, payload: Any) -> list[str]:
    """Every way ``payload`` falls short of schema ``name``, by JSON path."""
    return [
        f"{error.json_path}: {error.message}"
        for error in sorted(validator(name).iter_errors(payload), key=lambda error: error.json_path)
    ]


def check(name: str, payload: Any, label: str = "") -> None:
    """Raise ``AssertionError`` naming the failures, if there are any."""
    found = errors(name, payload)
    if found:
        raise AssertionError(f"{label or name} does not match {name}.schema.json:\n  " + "\n  ".join(found[:20]))


def check_lines(name: str, path: Path) -> int:
    """Hold every line of a JSONL file to schema ``name``; returns the count."""
    lines = path.read_bytes().split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for number, line in enumerate(lines, 1):
        check(name, json.loads(line), f"{path.name}:{number}")
    return len(lines)
