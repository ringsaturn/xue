"""The point products' normative shapes, ``docs/schemas/*.schema.json``, as
the tests hold the writers to them. The writers' own validators keep only
what a schema cannot say (row order, byte spans, sums, CRCs); everything
else a file must be is here, checked with the reference implementation of
JSON Schema 2020-12 (``jsonschema``, a dev dependency only)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "docs" / "schemas"


@cache
def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


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
