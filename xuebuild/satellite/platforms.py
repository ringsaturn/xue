"""The platform registry: which spacecraft image from which orbital slot,
with which instrument, in which channels, and where their files land.

A satellite source is named by its **orbital role** (``himawari`` at
140.7°E, ``goeseast`` at 75.2°W), never by the spacecraft: the pointer, the
run directories and the manifest ``model`` string must outlive a handover
(Himawari-10 relieves Himawari-9 around 2029), so the spacecraft lives in
the file, as the ``band`` block on each variable (docs/format.md §"Band and
Producer"), and in this table. A channel is a bundle: its id is the
nominal wavelength, instrument-neutral (``ir104`` is AHI band 13 and ABI
channel 13 alike, both an infrared window at 10.4 µm), and the exact
central wave number is in the ``band`` block. Registration is not
publication: a source publishes the channels ``sources.py`` lists, the
table here carries every channel the instrument has so that adding one
is a source-table line.

Nothing here reads a file or a bucket; ``readers.py`` does, by the
``reader`` a platform names, and ``projector.py`` puts what it opened on
the regular grid the bundles carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SatelliteBand:
    """The spectral band a satellite image variable was measured in, in the
    fields of GRIB2 product definition template 4.31: the spacecraft and
    instrument by their WMO common code table numbers (C-5 and C-8) and the
    band's central wave number in m⁻¹ as a scaled value. Written verbatim as
    the variable's ``band`` block (docs/format.md §"Band and Producer")."""

    satellite_series: int
    satellite_number: int
    instrument_type: int
    central_wavenumber: int
    """``scaledValueOfCentralWaveNumber``; the scale factor written is 0,
    since a wave number in whole m⁻¹ places any imager band to a tenth of
    a nanometre."""

    def metadata(self) -> dict[str, int]:
        return {
            "satelliteSeries": self.satellite_series,
            "satelliteNumber": self.satellite_number,
            "instrumentType": self.instrument_type,
            "scaleFactorOfCentralWaveNumber": 0,
            "scaledValueOfCentralWaveNumber": self.central_wavenumber,
        }


@dataclass(frozen=True)
class Channel:
    """One channel of an imager, as the source publishes it."""

    id: str
    """The variable id, by nominal wavelength: ``ir104``, ``wv062``,
    ``vis064``."""
    band: int
    """The instrument's own channel number (AHI band 13, ABI channel 13)."""
    wavelength_um: float
    """The central wavelength as the agency publishes it, in µm; the
    ``band`` block carries it as a wave number."""
    kind: Literal["bt", "reflectance"]
    """Brightness temperature (the infrared and water vapour bands) or
    reflectance (the visible and near-infrared bands)."""
    resolution_km: float
    """The channel's native resolution at the sub-satellite point."""
    bit_depth: int
    """The bit depth the product files spell in their names (ISatSS:
    ``B11`` for most AHI channels, ``B12`` for 10–15, ``B14`` for 7)."""

    @property
    def central_wavenumber(self) -> int:
        """The band's central wave number in whole m⁻¹."""
        return round(1e6 / self.wavelength_um)

    @property
    def missing_below(self) -> float | None:
        """The value below which a cell is missing, not measured: ISatSS
        writes a scan segment the instrument never delivered as 0 K rather
        than as its fill value, and no brightness temperature a 10 µm channel
        measures is below 100 K. A reflectance can be 0 (the night side), so
        it has no such floor."""
        return 100.0 if self.kind == "bt" else None


@dataclass(frozen=True)
class Platform:
    """One spacecraft at one orbital slot: one row per satellite, and the
    source id is the slot's role, so a successor changes this row and
    nothing published."""

    role: str
    """The source id: ``himawari``, ``goeseast``, ``goeswest``."""
    spacecraft: str
    """``Himawari-9``: the human-readable name the shell shows."""
    satellite_number: int
    """WMO Common Code Table C-5."""
    instrument: str
    """``AHI``, ``ABI``."""
    instrument_type: int
    """WMO Common Code Table C-8."""
    sub_longitude: float
    """The sub-satellite longitude, degrees east."""
    sweep_axis: Literal["x", "y"]
    """The CF ``sweep_angle_axis`` of the geostationary projection: ``y``
    for AHI, ``x`` for ABI."""
    channels: tuple[Channel, ...]
    reader: str
    """Which :mod:`readers` implementation opens its files: ``isatss``."""
    bucket: str
    """The public bucket the files are on (``noaa-himawari9``)."""
    prefix: str
    """The product prefix inside it (``AHI-L2-FLDK-ISatSS``)."""
    tile_count: int
    """How many files one channel of one slot is cut into: a slot is
    complete when all of them have landed."""
    cadence_seconds: int
    """The scan cadence: 600 for a full disk."""
    typical_lag_seconds: int
    """How long after a scan starts its files usually land, for the log."""
    satellite_series: int = 0
    """Template 4.31's satellite series; 0 unless a table says otherwise."""
    reference_channel: str = "ir104"
    """The channel a listing walks to find the slots the bucket holds
    (a reader that lists per channel asks for this one): the 10.4 µm
    window, which every imager here has and every scan delivers."""

    def channel(self, channel_id: str) -> Channel:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise KeyError(f"{self.spacecraft} {self.instrument} has no channel {channel_id!r}")

    def band(self, channel_id: str) -> SatelliteBand:
        """The ``band`` block of one channel as this spacecraft measures it."""
        channel = self.channel(channel_id)
        return SatelliteBand(
            satellite_series=self.satellite_series,
            satellite_number=self.satellite_number,
            instrument_type=self.instrument_type,
            central_wavenumber=channel.central_wavenumber,
        )

    def bands(self, channel_ids: tuple[str, ...]) -> tuple[tuple[str, SatelliteBand], ...]:
        """``SourceSpec.bands`` for the channels a source publishes."""
        return tuple((channel_id, self.band(channel_id)) for channel_id in channel_ids)

    @property
    def region(self) -> tuple[float, float, float, float]:
        """The disk's useful extent on plate carrée as ``(west, south, east,
        north)``: 60° either side of the sub-satellite longitude and 60°
        of latitude, past which the viewing angle is too oblique to read.
        A disk that crosses the antimeridian is spelled in the grid's own
        copy of the world with the west edge inside −180 … 180 and the
        east edge past 180 (Himawari's 80.7 … 200.7, GOES-West's 163 …
        283): the one shape the encoders' ``crop_grid`` and the shell's
        viewport arithmetic take. GOES-East, −135.2 … −15.2, stays on
        negative longitudes like the regional radar grids."""
        west = self.sub_longitude - 60.0
        if west < -180.0:
            west += 360.0
        return (round(west, 6), -60.0, round(west + 120.0, 6), 60.0)


# Himawari-8/9 AHI: sixteen bands, central wavelengths as JMA publishes
# them for the AHI. Visible and near-infrared at 0.5–1 km, the rest at 2 km.
AHI_CHANNELS: tuple[Channel, ...] = (
    Channel("vis047", 1, 0.47063, "reflectance", 1.0, 11),
    Channel("vis051", 2, 0.51000, "reflectance", 1.0, 11),
    Channel("vis064", 3, 0.63914, "reflectance", 0.5, 11),
    Channel("nir086", 4, 0.85670, "reflectance", 1.0, 11),
    Channel("nir161", 5, 1.6101, "reflectance", 2.0, 11),
    Channel("nir226", 6, 2.2568, "reflectance", 2.0, 11),
    Channel("ir039", 7, 3.8853, "bt", 2.0, 14),
    Channel("wv062", 8, 6.2429, "bt", 2.0, 11),
    Channel("wv069", 9, 6.9410, "bt", 2.0, 11),
    Channel("wv073", 10, 7.3467, "bt", 2.0, 12),
    Channel("ir086", 11, 8.5926, "bt", 2.0, 12),
    Channel("ir096", 12, 9.6372, "bt", 2.0, 12),
    Channel("ir104", 13, 10.4073, "bt", 2.0, 12),
    Channel("ir112", 14, 11.2395, "bt", 2.0, 12),
    Channel("ir123", 15, 12.3806, "bt", 2.0, 12),
    Channel("ir133", 16, 13.2807, "bt", 2.0, 11),
)

# GOES-R ABI: sixteen channels, central wavelengths per the ABI bands
# table. Prefilled for the GOES platforms; nothing reads them yet, and the
# CMIPF files are one per channel, so the bit depth is not a name field
# (0).
ABI_CHANNELS: tuple[Channel, ...] = (
    Channel("vis047", 1, 0.47, "reflectance", 1.0, 0),
    Channel("vis064", 2, 0.64, "reflectance", 0.5, 0),
    Channel("nir086", 3, 0.865, "reflectance", 1.0, 0),
    Channel("nir137", 4, 1.378, "reflectance", 2.0, 0),
    Channel("nir161", 5, 1.61, "reflectance", 1.0, 0),
    Channel("nir226", 6, 2.25, "reflectance", 2.0, 0),
    Channel("ir039", 7, 3.90, "bt", 2.0, 0),
    Channel("wv062", 8, 6.19, "bt", 2.0, 0),
    Channel("wv069", 9, 6.95, "bt", 2.0, 0),
    Channel("wv073", 10, 7.34, "bt", 2.0, 0),
    Channel("ir086", 11, 8.50, "bt", 2.0, 0),
    Channel("ir096", 12, 9.61, "bt", 2.0, 0),
    Channel("ir104", 13, 10.35, "bt", 2.0, 0),
    Channel("ir112", 14, 11.2, "bt", 2.0, 0),
    Channel("ir123", 15, 12.3, "bt", 2.0, 0),
    Channel("ir133", 16, 13.3, "bt", 2.0, 0),
)

# Himawari-9 at 140.7°E, the JMA imager NOAA redistributes on its own bucket
# (2026-09-17 measurements: the ISatSS tiles of a slot are generated about
# eight minutes after the scan starts and listed some fifteen minutes after
# it). WMO C-5 174, C-8 297 (wmo-im/CCT).
HIMAWARI = Platform(
    role="himawari",
    spacecraft="Himawari-9",
    satellite_number=174,
    instrument="AHI",
    instrument_type=297,
    sub_longitude=140.7,
    sweep_axis="y",
    channels=AHI_CHANNELS,
    reader="isatss",
    bucket="noaa-himawari9",
    prefix="AHI-L2-FLDK-ISatSS",
    tile_count=88,
    cadence_seconds=600,
    typical_lag_seconds=15 * 60,
)

# The GOES-R platforms, registered so a second satellite is a source-table
# entry and a reader (`CMIPFReader`, one file per channel per slot) rather
# than a design: GOES-19 (C-5 273) east at 75.2°W, GOES-18 (C-5 272) west
# at 137.0°W, ABI C-8 617. Neither is published yet.
GOES_EAST = Platform(
    role="goeseast",
    spacecraft="GOES-19",
    satellite_number=273,
    instrument="ABI",
    instrument_type=617,
    sub_longitude=-75.2,
    sweep_axis="x",
    channels=ABI_CHANNELS,
    reader="cmipf",
    bucket="noaa-goes19",
    prefix="ABI-L2-CMIPF",
    tile_count=1,
    cadence_seconds=600,
    typical_lag_seconds=5 * 60,
)
GOES_WEST = Platform(
    role="goeswest",
    spacecraft="GOES-18",
    satellite_number=272,
    instrument="ABI",
    instrument_type=617,
    sub_longitude=-137.0,
    sweep_axis="x",
    channels=ABI_CHANNELS,
    reader="cmipf",
    bucket="noaa-goes18",
    prefix="ABI-L2-CMIPF",
    tile_count=1,
    cadence_seconds=600,
    typical_lag_seconds=5 * 60,
)

PLATFORMS: dict[str, Platform] = {platform.role: platform for platform in (HIMAWARI, GOES_EAST, GOES_WEST)}


def platform(role: str) -> Platform:
    try:
        return PLATFORMS[role]
    except KeyError as exc:
        raise KeyError(f"no satellite platform is registered under {role!r}") from exc
