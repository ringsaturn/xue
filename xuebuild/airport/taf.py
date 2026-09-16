"""``tafs.cache.xml`` → :class:`TafReport`.

The Aviation Weather Center decodes every current TAF into a ``<TAF>``
element with one ``<forecast>`` per period, so this module is a reader,
not a TAF parser: it takes the elements by name, converts to SI
(:mod:`.units`) and keeps the bulletin verbatim in ``raw``. It imports no
other parser.

The file lists a TAF's periods in no particular order — an amended
forecast's ``FM`` group can precede the period it amends — and the product
is byte-identical for the same input, so the periods are sorted by their
start and then their end. A period's ``change`` is the AWC's
``change_indicator`` (``FM``, ``BECMG``, ``TEMPO``, ``PROB``), absent on
the forecast's first, prevailing period; a ``PROB40 TEMPO`` group is
``change: "TEMPO"`` with ``prob: 40``. The ``time_becoming`` of a
``BECMG`` group (when inside the period the change completes) is not
carried in v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

from .schema import CHANGES, WIND_DIRECTION_RANGE, WIND_SPEED_RANGE
from .units import bounded, cloud_layers, visibility, wind_speed

AMENDED_REMARKS = frozenset({"AMD", "COR"})
"""The ``remarks`` words that mark a bulletin replacing an earlier one:
``AMD`` for an amendment, ``COR`` for a correction. Matched as whole
words — the plain remarks (``RMK NXT FCST BY 161800Z``) are prose."""


@dataclass(frozen=True)
class TafPeriod:
    start: datetime
    end: datetime
    change: str | None
    prob: int | None
    wd: int | None
    ws: float | None
    gust: float | None
    vis: int | None
    wx: str | None
    cloud: list[list[Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "from": _iso(self.start),
            "to": _iso(self.end),
            "change": self.change,
            "prob": self.prob,
            "wd": self.wd,
            "ws": self.ws,
            "gust": self.gust,
            "vis": self.vis,
            "wx": self.wx,
            "cloud": self.cloud,
        }


@dataclass(frozen=True)
class TafReport:
    icao: str
    issued: datetime
    start: datetime
    end: datetime
    raw: str
    amended: bool
    periods: list[TafPeriod]

    def to_json(self) -> dict[str, Any]:
        return {
            "issued": _iso(self.issued),
            "from": _iso(self.start),
            "to": _iso(self.end),
            "raw": self.raw,
            "amended": self.amended,
            "periods": [period.to_json() for period in self.periods],
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    if element is None:
        return None
    found = element.find(name)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _time(element: ElementTree.Element, name: str) -> datetime | None:
    value = _text(element, name)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(microsecond=0)


def _float(element: ElementTree.Element, name: str) -> float | None:
    value = _text(element, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(element: ElementTree.Element, name: str) -> int | None:
    value = _float(element, name)
    return None if value is None else int(round(value))


def _bounded_int(value: int | None, bounds: tuple[float | None, float | None]) -> int | None:
    """The value, or None when it is outside what the contract admits
    (:func:`~.units.bounded`)."""
    kept = bounded(value, bounds)
    return None if kept is None else int(kept)


def _attribute_float(element: ElementTree.Element, name: str) -> float | None:
    value = element.get(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_tafs(text: str) -> list[TafReport]:
    """Every ``<TAF>`` the document decodes into a forecast, in document
    order. One without a station id, an issue time or a validity is
    skipped."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"the TAF cache is not well-formed XML: {exc}") from exc
    reports = []
    for element in root.iter("TAF"):
        report = _report(element)
        if report is not None:
            reports.append(report)
    return reports


def _report(element: ElementTree.Element) -> TafReport | None:
    icao = _text(element, "station_id")
    issued = _time(element, "issue_time")
    start = _time(element, "valid_time_from")
    end = _time(element, "valid_time_to")
    raw = _text(element, "raw_text")
    if icao is None or issued is None or start is None or end is None or raw is None:
        return None
    remarks = (_text(element, "remarks") or "").upper()
    periods = [period for period in (_period(child) for child in element.findall("forecast")) if period is not None]
    periods.sort(key=lambda period: (period.start, period.end))
    return TafReport(
        icao=icao.upper(),
        issued=issued,
        start=start,
        end=end,
        raw=raw,
        amended=bool(AMENDED_REMARKS & set(remarks.split())),
        periods=periods,
    )


def _period(element: ElementTree.Element) -> TafPeriod | None:
    start = _time(element, "fcst_time_from")
    end = _time(element, "fcst_time_to")
    if start is None or end is None:
        return None
    # The contract admits the four indicators the service publishes; one
    # it has never written is read as the prevailing group rather than
    # costing the round. `raw` keeps the truth.
    change = (_text(element, "change_indicator") or "").upper()
    layers: list[tuple[str, float | None]] = []
    for condition in element.findall("sky_condition"):
        cover = condition.get("sky_cover")
        if cover is None or not cover.strip():
            continue
        layers.append((cover.strip().upper(), _attribute_float(condition, "cloud_base_ft_agl")))
    return TafPeriod(
        start=start,
        end=end,
        change=change if change in CHANGES else None,
        prob=_bounded_int(_int(element, "probability"), (0, 100)),
        wd=_bounded_int(_int(element, "wind_dir_degrees"), WIND_DIRECTION_RANGE),
        ws=bounded(wind_speed(_float(element, "wind_speed_kt")), WIND_SPEED_RANGE),
        gust=bounded(wind_speed(_float(element, "wind_gust_kt")), WIND_SPEED_RANGE),
        vis=visibility(_text(element, "visibility_statute_mi")),
        wx=_text(element, "wx_string"),
        cloud=cloud_layers(layers),
    )
