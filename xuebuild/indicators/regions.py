"""The regions the indicators are aggregated over (``docs/indicators.md`` §2).

Three country composites and thirteen state or province sub-regions. A
region's weights are the soybean physical area on each grid cell inside its
admin-1 polygons (the weights file, :mod:`.weights`); this module only names
them. The local day a frame falls on is taken at a fixed UTC offset per
region, never a time-zone table: daylight saving would move the day boundary
by an hour twice a year, and a fixed boundary keeps every run comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    country: str
    """ISO 3166-1 alpha-2."""
    subdivisions: tuple[str, ...]
    """The ISO 3166-2 codes of the admin-1 units the region is made of."""
    utc_offset_hours: int
    """The fixed offset local days are taken at."""
    parent: str | None = None
    """The composite a sub-region belongs to; None on a composite."""

    @property
    def composite(self) -> bool:
        return self.parent is None

    def index_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "country": self.country,
            "subdivisions": list(self.subdivisions),
            "utcOffsetHours": self.utc_offset_hours,
            "parent": self.parent,
        }


REGIONS: tuple[Region, ...] = (
    Region(
        "us-soy",
        "United States soybean belt",
        "US",
        (
            "US-IA", "US-IL", "US-MN", "US-IN", "US-NE", "US-OH", "US-MO",
            "US-SD", "US-ND", "US-KS", "US-WI", "US-MI", "US-AR",
        ),
        -6,
    ),
    Region("us-ia", "Iowa", "US", ("US-IA",), -6, "us-soy"),
    Region("us-il", "Illinois", "US", ("US-IL",), -6, "us-soy"),
    Region("us-mn", "Minnesota", "US", ("US-MN",), -6, "us-soy"),
    Region("us-in", "Indiana", "US", ("US-IN",), -6, "us-soy"),
    Region("us-ne", "Nebraska", "US", ("US-NE",), -6, "us-soy"),
    Region(
        "br-soy",
        "Brazil soybean states",
        "BR",
        ("BR-MT", "BR-PR", "BR-RS", "BR-GO", "BR-MS", "BR-MG", "BR-BA", "BR-MA", "BR-TO", "BR-PI"),
        -3,
    ),
    Region("br-mt", "Mato Grosso", "BR", ("BR-MT",), -3, "br-soy"),
    Region("br-pr", "Paraná", "BR", ("BR-PR",), -3, "br-soy"),
    Region("br-rs", "Rio Grande do Sul", "BR", ("BR-RS",), -3, "br-soy"),
    Region("br-go", "Goiás", "BR", ("BR-GO",), -3, "br-soy"),
    Region("br-ms", "Mato Grosso do Sul", "BR", ("BR-MS",), -3, "br-soy"),
    Region(
        "ar-soy",
        "Argentina soybean provinces",
        "AR",
        ("AR-B", "AR-X", "AR-S", "AR-E", "AR-L"),
        -3,
    ),
    Region("ar-ba", "Buenos Aires", "AR", ("AR-B",), -3, "ar-soy"),
    Region("ar-cb", "Córdoba", "AR", ("AR-X",), -3, "ar-soy"),
    Region("ar-sf", "Santa Fe", "AR", ("AR-S",), -3, "ar-soy"),
)

REGION_IDS: tuple[str, ...] = tuple(region.id for region in REGIONS)
_BY_ID = {region.id: region for region in REGIONS}


def region(region_id: str) -> Region:
    try:
        return _BY_ID[region_id]
    except KeyError:
        raise KeyError(f"unknown indicators region {region_id!r}") from None


def regions_index() -> list[dict[str, Any]]:
    """The index's ``regions[]``: every region, in table order."""
    return [entry.index_entry() for entry in REGIONS]
