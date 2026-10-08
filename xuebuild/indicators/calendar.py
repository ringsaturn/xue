"""Crop calendar windows (``docs/indicators.md`` §3).

Each region has a **season** window, over which the in-season totals
accumulate, and a **flowering** window inside it (flowering and pod set), over
which the heat and dry statistics are also kept separately. Windows are
month-day ranges, inclusive at both ends; a range whose end comes before its
start in the calendar runs over the new year, and its season is named by both
years (``2026-27``); a season inside one calendar year is named by that year
(``2027``).

Sources: USDA NASS Agricultural Handbook 628 (United States planting and
harvest dates), CONAB crop progress (Brazil, by state), Bolsa de Comercio de
Rosario and Bolsa de Cereales de Buenos Aires (Argentina). The flowering
windows are working values pending a check against the published progress
percentiles. Changing any window changes :data:`VERSION`; every feature line
and season file records the version it was computed under.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .regions import REGIONS

VERSION = "1"
SOURCE = "USDA NASS Handbook 628; CONAB crop progress; BCR / BCBA"

MonthDay = tuple[int, int]


@dataclass(frozen=True)
class Window:
    start: MonthDay
    end: MonthDay

    @property
    def crosses_year(self) -> bool:
        return self.end < self.start

    def contains(self, day: date) -> bool:
        key = (day.month, day.day)
        if self.crosses_year:
            return key >= self.start or key <= self.end
        return self.start <= key <= self.end

    def spelled(self) -> list[str]:
        return [f"{self.start[0]:02d}-{self.start[1]:02d}", f"{self.end[0]:02d}-{self.end[1]:02d}"]


@dataclass(frozen=True)
class CropCalendar:
    season: Window
    flowering: Window


_US = CropCalendar(Window((5, 1), (9, 30)), Window((7, 1), (8, 31)))
_BR_SEASON = Window((10, 1), (3, 31))
_AR = CropCalendar(Window((10, 15), (4, 30)), Window((1, 1), (2, 28)))

CALENDARS: dict[str, CropCalendar] = {
    "us-soy": _US,
    "us-ia": _US,
    "us-il": _US,
    "us-mn": _US,
    "us-in": _US,
    "us-ne": _US,
    # The composite's flowering window spans its states' windows, from the
    # earliest (Mato Grosso) to the latest (Rio Grande do Sul).
    "br-soy": CropCalendar(_BR_SEASON, Window((11, 15), (2, 28))),
    "br-mt": CropCalendar(_BR_SEASON, Window((11, 15), (1, 31))),
    "br-pr": CropCalendar(_BR_SEASON, Window((12, 1), (2, 15))),
    "br-rs": CropCalendar(_BR_SEASON, Window((12, 15), (2, 28))),
    "br-go": CropCalendar(_BR_SEASON, Window((12, 1), (2, 15))),
    "br-ms": CropCalendar(_BR_SEASON, Window((12, 1), (2, 15))),
    "ar-soy": _AR,
    "ar-ba": _AR,
    "ar-cb": _AR,
    "ar-sf": _AR,
}

WINDOWS = ("season", "flowering")


def _calendar(region_id: str) -> CropCalendar:
    try:
        return CALENDARS[region_id]
    except KeyError:
        raise KeyError(f"no crop calendar for region {region_id!r}") from None


def season_of(region_id: str, day: date) -> str | None:
    """The id of the season ``day`` lies in for ``region_id``, or None
    outside every season: ``2027`` for a season inside one year,
    ``2026-27`` for one that runs over the new year."""
    window = _calendar(region_id).season
    if not window.contains(day):
        return None
    if not window.crosses_year:
        return f"{day.year}"
    first = day.year if (day.month, day.day) >= window.start else day.year - 1
    return f"{first}-{(first + 1) % 100:02d}"


def in_window(region_id: str, day: date, window: str = "flowering") -> bool:
    """Whether ``day`` lies in the region's ``season`` or ``flowering``
    window (inclusive at both ends)."""
    calendar = _calendar(region_id)
    if window == "season":
        return calendar.season.contains(day)
    if window == "flowering":
        # The flowering window is a sub-window: a day outside the season is
        # never in it, whatever its month and day say.
        return calendar.season.contains(day) and calendar.flowering.contains(day)
    raise ValueError(f"unknown calendar window {window!r}")


def season_bounds(region_id: str, season: str) -> tuple[date, date]:
    """The first and last day of a named season."""
    window = _calendar(region_id).season
    first_year = int(season.split("-")[0])
    last_year = first_year + 1 if window.crosses_year else first_year
    return date(first_year, *window.start), date(last_year, *window.end)


def calendar_index() -> dict[str, Any]:
    """The index's ``calendar`` block, published as the code holds it."""
    return {
        "version": VERSION,
        "source": SOURCE,
        "regions": {
            region.id: {name: getattr(_calendar(region.id), name).spelled() for name in WINDOWS}
            for region in REGIONS
        },
    }
