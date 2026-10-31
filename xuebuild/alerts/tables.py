"""The category and level mappings, read from ``tables.json`` beside this
module. They are data, not code: a new NWS event name, ECCC subject or
JMA kind code is a line in that file and a row in ``docs/alerts.md`` §3,
and nothing else changes."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from .schema import CATEGORIES, LEVELS, AlertsProductError

TABLES_PATH = Path(__file__).with_name("tables.json")


@cache
def tables() -> dict[str, Any]:
    payload = json.loads(TABLES_PATH.read_text(encoding="utf-8"))
    for network in ("nws", "jma", "eccc"):
        if network not in payload:
            raise AlertsProductError(f"tables.json has no {network} table")
    categories = (
        list(payload["nws"]["categories"].values())
        + [kind["category"] for kind in payload["jma"]["kinds"].values()]
        + list(payload["eccc"]["subjects"].values())
    )
    unknown = sorted(set(categories) - set(CATEGORIES))
    if unknown:
        raise AlertsProductError(f"tables.json files events under unknown categories {unknown}")
    levels = (
        list(payload["nws"]["levelBySuffix"].values())
        + list(payload["nws"]["levelByEvent"].values())
        + [kind["level"] for kind in payload["jma"]["kinds"].values()]
        + list(payload["eccc"]["levelByColour"].values())
        + list(payload["eccc"]["levelByType"].values())
    )
    if not set(levels) <= set(LEVELS):
        raise AlertsProductError("tables.json uses a level outside 1–5")
    return payload


# --- nws ---------------------------------------------------------------


def nws_category(event: str) -> str:
    return tables()["nws"]["categories"].get(event, "other")


def nws_level(event: str, severity: str | None) -> int:
    """By event name where the table names it, else by its last word
    (Statement 1, Advisory and Watch 2, Warning 3); a Warning the sender
    marks CAP severity Extreme — a tornado emergency, a PDS warning — is
    raised to 4."""
    table = tables()["nws"]
    level = table["levelByEvent"].get(event)
    if level is None:
        suffix = event.rsplit(" ", 1)[-1] if event else ""
        level = table["levelBySuffix"].get(suffix, table["defaultLevel"])
    if severity == "Extreme" and event.endswith("Warning"):
        level = max(level, table["extremeWarningLevel"])
    return int(level)


# --- jma ---------------------------------------------------------------


def jma_kind(code: str) -> dict[str, Any] | None:
    """A kind code of the r8 warning map → its element, level (2–5),
    category and names, or None for a code this table does not know."""
    return tables()["jma"]["kinds"].get(code)


def jma_kind_of(element: str, level: int) -> str | None:
    """The kind code of an element at a level: the 危険警報 a downgrade
    to 注意報 lifts."""
    for code, kind in tables()["jma"]["kinds"].items():
        if kind["element"] == element and kind["level"] == level:
            return code
    return None


# --- eccc --------------------------------------------------------------

_PARENTHESIS = re.compile(r"\s*\([^)]*\)\s*$")


def eccc_subject(short_name: str | None) -> str:
    """``Fog (advisory)`` → ``fog``: the subject the table files."""
    return _PARENTHESIS.sub("", short_name or "").strip().lower()


def eccc_category(short_name: str | None) -> str:
    return tables()["eccc"]["subjects"].get(eccc_subject(short_name), "other")


def eccc_level(colour: str | None, alert_type: str | None) -> int:
    """By the risk colour where there is one (yellow 2, orange 3, red 4),
    else by the alert type (statement 1, advisory and watch 2, warning
    3)."""
    table = tables()["eccc"]
    level = table["levelByColour"].get((colour or "").lower())
    if level is None:
        level = table["levelByType"].get((alert_type or "").lower(), table["defaultLevel"])
    return int(level)
