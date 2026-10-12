"""The FIRMS files the ``fire`` product reads and the sensors it publishes,
in the order the index lists them. Fixed tuples rather than a registry:
adding a sensor is a reviewed change to this file, ``docs/fire.md`` §3 and
the fixture — not the schema and not the shell."""

from __future__ import annotations

from dataclasses import dataclass

BASE_URL = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"
ATTRIBUTION_VIIRS = "NASA FIRMS (LANCE NRT), VIIRS 375 m C2"
ATTRIBUTION_MODIS = "NASA FIRMS (LANCE NRT), MODIS C6.1"
LICENSE = "NASA-open-data"
URL = "https://firms.modaps.eosdis.nasa.gov/"


@dataclass(frozen=True)
class Sensor:
    id: str
    name: str
    resolution: int
    """Nominal pixel size at nadir, metres."""
    attribution: str

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "resolution": self.resolution,
            "attribution": self.attribution,
            "license": LICENSE,
            "url": URL,
        }


@dataclass(frozen=True)
class Source:
    id: str
    product: str
    """FIRMS's directory under :data:`BASE_URL`."""
    filename: str
    satellites: dict[str, str]
    """The CSV's ``satellite`` column → the sensor id that row belongs to.
    MODIS puts Terra and Aqua in one file."""
    modis: bool
    """MODIS names its columns ``brightness`` and gives ``confidence`` as a
    percentage; VIIRS names the fire channel ``bright_ti4`` and gives a
    class."""

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.product}/csv/{self.filename}"

    @property
    def raw_name(self) -> str:
        """The file's name in a round's raw directory."""
        return f"{self.product}.csv"


SENSORS: tuple[Sensor, ...] = (
    Sensor("viirs-snpp", "VIIRS S-NPP", 375, ATTRIBUTION_VIIRS),
    Sensor("viirs-noaa20", "VIIRS NOAA-20", 375, ATTRIBUTION_VIIRS),
    Sensor("viirs-noaa21", "VIIRS NOAA-21", 375, ATTRIBUTION_VIIRS),
    Sensor("modis-terra", "MODIS Terra", 1000, ATTRIBUTION_MODIS),
    Sensor("modis-aqua", "MODIS Aqua", 1000, ATTRIBUTION_MODIS),
)

SOURCES: tuple[Source, ...] = (
    Source("firms-suomi-npp-viirs-c2", "suomi-npp-viirs-c2", "SUOMI_VIIRS_C2_Global_24h.csv", {"N": "viirs-snpp"}, False),
    Source("firms-noaa-20-viirs-c2", "noaa-20-viirs-c2", "J1_VIIRS_C2_Global_24h.csv", {"N20": "viirs-noaa20"}, False),
    Source("firms-noaa-21-viirs-c2", "noaa-21-viirs-c2", "J2_VIIRS_C2_Global_24h.csv", {"N21": "viirs-noaa21"}, False),
    Source(
        "firms-modis-c6-1",
        "modis-c6.1",
        "MODIS_C6_1_Global_24h.csv",
        {"T": "modis-terra", "A": "modis-aqua"},
        True,
    ),
)


def sources_of(sensor_id: str) -> tuple[Source, ...]:
    return tuple(source for source in SOURCES if sensor_id in source.satellites.values())


__all__ = ["SENSORS", "SOURCES", "Sensor", "Source", "sources_of"]
