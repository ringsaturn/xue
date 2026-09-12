"""Per-model source registry: where a model's data comes from and how its
run directory, manifest identity, and time axis are named.

The models share one output contract: whatever the source, the bundles carry
the same data variable ids (tmp2m, prate, ugrd10m/vgrd10m, on sflux also
dswrf, on GFS and ECMWF the pressure family and the upper-air fills, and on
GFS alone — for now — the surface diagnostics, the vertical velocity, the
850 hPa equivalent potential temperature and the ocean fields; on HRRR the
forecast composite reflectivity under the radar mosaic's ``cref``) so the
decoder and frontend never care which model produced them. A source may read
more than one file family of a cycle (GFS pgrb2 plus GFS-Wave,
:class:`CompanionFile`); the frame the converter sees is still one GRIB. A
source may be computed on a map projection (HRRR, Lambert conformal): its
``regrid`` says so, and the converter resamples every plane onto the regular
grid the format describes (:mod:`xuebuild.reproject`).
Not every source is a forecast: an ``observation`` source (the CMA radar
mosaic) is a local file holding a series of observed analyses, with no cycle
to fetch, no live pointer, and an axis that is whatever times the file
carries.
ECMWF has no native rate field; its accumulated ``tp`` input is de-accumulated
into prate by the converter. GFS sflux has only interval-averaged PRATE (the
averaging window resets every 6 hours); the converter de-averages consecutive
frames into hourly rates.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DownloadError
from .reproject import Regrid


@dataclass(frozen=True)
class CompanionFile:
    """A second file family of the same cycle some of a source's inputs are
    read from. GFS publishes its wave model beside the atmosphere — one
    ``gfswave.tHHz.global.0p25.fFFF.grib2`` per forecast hour under
    ``wave/gridded/``, on the pgrb2 grid and axis — and the fetcher appends
    the records it needs from it to the frame's GRIB after the primary
    file's, so downstream of the download a frame is still one file.
    :mod:`xuebuild.fetch` knows each family's object name by ``id``."""

    id: str
    """The family: ``wave`` for GFS-Wave."""
    variable_ids: tuple[str, ...]
    """Which of the source's :attr:`SourceSpec.input_variable_ids` come
    from this family rather than the primary file, in assembly order."""
    repack: bool = False
    """True when the family's records must be repacked to ``grid_simple``
    with eccodes at fetch time, the way the CCSDS-packed ECMWF files are,
    because some GDAL this pipeline converts through cannot decode their
    packing. A build job of a bundle from such a family installs
    ``grib_set`` (``assemble.group_needs_eccodes``). No family needs it
    now: WAVEWATCH III writes JPEG 2000 packing (DRS template 5.40), and
    the GDAL the ``xuepy`` wheel carries decodes it since the wheel took
    OpenJPEG on board (``scripts/build-gdal-minimal.sh``); before that the
    wave family was repacked here, and the switch stays for the next
    packing a wheel cannot read."""


@dataclass(frozen=True)
class SourceSpec:
    id: str
    """CLI / URL / directory id: "gfs", "ecmwf", "sflux", "hrrr" or "radar"."""
    manifest_model: str
    """The manifest and bundle-metadata ``model`` string."""
    product: str
    """The manifest ``product`` string."""
    latest_filename: str | None
    """Per-model mutable live pointer at the data root. GFS uses the bare
    ``latest.json``; the other models use ``latest-<model>.json``. None for a
    source with no live feed — an observation dataset arrives as whole files
    after the fact, so there is no cycle to point at."""
    steps: tuple[tuple[int, int], ...]
    """The published time axis as ``(last_hour, step_hours)`` segments: the
    series runs at ``step_hours`` up to and including ``last_hour``, then the
    next segment takes over. No source publishes one cadence all the way to
    240 hours, so an axis that crosses a segment boundary is mixed-step and
    its bundles carry metadata schemaVersion 2 (docs/format.md)."""
    input_variable_ids: tuple[str, ...]
    """Variables fetched from the source, in GRIB assembly order: the primary
    file's records first, then each companion family's."""
    accumulated_precipitation: bool
    """True when precipitation arrives as a run-total accumulation (ECMWF
    ``tp``, metres) and must be de-accumulated into a rate."""
    companion_files: tuple[CompanionFile, ...] = ()
    """Further file families of the same cycle some inputs come from. A run
    is complete only when every family has its first and last frame, and a
    frame's download is one range set per family."""
    averaged_precipitation: bool = False
    """True when precipitation arrives as an interval-averaged rate whose
    averaging window resets every :attr:`average_window_hours` (GFS sflux
    ``PRATE ave``) and must be de-averaged into per-step mean rates."""
    average_window_hours: int = 6
    """Length of the averaging-window reset cycle for averaged precipitation."""
    optional_at_analysis: tuple[str, ...] = ()
    """Input variables absent from the analysis (f000) file — sflux carries no
    PRATE record at f000, so the derived prate series starts at the first
    real step (mirroring the ECMWF de-accumulated prate axis)."""
    bundle_scalar_ids: tuple[str, ...] = ("tmp2m", "prate")
    """Scalar variables published as single-variable bundles, in manifest
    order."""
    bundle_vector_ids: tuple[str, ...] = ()
    """Two-variable bundles published, in manifest order: ``wind10m`` for the
    10 m pair, ``wind<level>`` for an isobaric pair, ``qflux<level>`` for the
    water vapour flux the converter derives on that surface, ``wave`` for
    the wave vector it derives from the height and direction. Listing one is
    not enough on its own — it ships only when every input it is built from
    (:func:`xuebuild.binconvert.vector_input_ids`) is in
    :attr:`input_variable_ids`, and a run whose files turn out to lack them
    builds without it and says so."""
    production_grid: tuple[int, int] = (1440, 721)
    """Grid a complete (``require_complete``) build must arrive on."""
    tile: tuple[int, int] = (48, 52)
    """Container v2 tile size as ``(width, height)`` in grid cells.

    48 x 52 cuts the 0.25-degree global grid into 30 x 14 = 420 tiles of
    about 12 x 13 degrees: small enough that one cell's whole series costs
    ~100 KB rather than the megabytes a coarser tile would, and large enough
    that every published variable still compresses at or below what the
    plane-major container achieved. The last row is clipped (721 = 13 x 52 +
    45), which the format allows precisely because no tidy power of two
    divides 721. A half-resolution variant halves it (``ceil``), so a tile
    with the same number covers the same ground in both tiers.

    Changing this is a data format change, not a runtime knob: the golden
    fixtures and the encoder-parity test are regenerated with it."""
    fetch_concurrency: int = 4
    """Frames fetched in parallel. Each frame costs several fresh HTTPS
    round-trips, so sequential fetching is latency-bound. NOAA's bucket and
    Google's copy of the ECMWF open data take a few parallel streams
    happily; the mirrors that answer bursts with 503 Slow Down are paced
    per request (:mod:`xuebuild.fetch`), one shared clock across the
    threads, so falling back to one of them costs the same as it always
    did rather than a burst."""
    observation: bool = False
    """True for a source that is not a forecast at all: one local file
    holding a series of observed analyses, read through
    :mod:`xue.observation` instead of fetched frame by frame. Its axis is
    whatever times the file carries — including gaps where a publication was
    missed — so it has no published cadence to validate against."""
    cycle_hours: int = 6
    """Hours between the source's cycles: a run starts on a multiple of this
    (00/06/12/18 UTC for the global models, every hour for HRRR)."""
    regrid: Regrid | None = None
    """Set when the source's records are on a map projection rather than a
    regular latitude/longitude grid, and must be resampled onto one of this
    step before anything else reads them (:mod:`xuebuild.reproject`). The
    grid the bundles carry is then the regular one; ``production_grid`` and
    ``tile`` describe it, not the projected source."""

    @property
    def live(self) -> bool:
        """Whether the source has a live feed to fetch and point at."""
        return self.latest_filename is not None

    @property
    def horizon_hours(self) -> int:
        """The last forecast hour the source publishes — what a live run
        carries, and what ``--hours`` defaults to."""
        if not self.steps:
            raise DownloadError(f"{self.manifest_model} publishes no forecast axis")
        return self.steps[-1][0]

    def companion_of(self, variable_id: str) -> CompanionFile | None:
        """The companion family ``variable_id`` is read from, or None for an
        input of the primary file."""
        for companion in self.companion_files:
            if variable_id in companion.variable_ids:
                return companion
        return None

    def primary_input_ids(self) -> tuple[str, ...]:
        """The inputs read from the primary file, in assembly order."""
        return tuple(variable_id for variable_id in self.input_variable_ids if self.companion_of(variable_id) is None)

    def forecast_hours(self, last_hour: int) -> list[int]:
        """The published axis from the analysis through ``last_hour``.

        ``last_hour`` must itself lie on the axis — a cap that lands between
        steps (or beyond the published range) has no complete final frame to
        fetch and is rejected outright."""
        if self.observation:
            raise DownloadError(f"{self.manifest_model} is an observation source and publishes no forecast axis")
        hours = [0]
        for boundary, step in self.steps:
            while hours[-1] < min(boundary, last_hour):
                hours.append(hours[-1] + step)
            if hours[-1] >= last_hour:
                break
        if hours[-1] != last_hour:
            published = ", then ".join(f"{step}-hourly to f{boundary:03d}" for boundary, step in self.steps)
            raise DownloadError(
                f"forecast hour {last_hour} is not on the {self.manifest_model} axis ({published})"
            )
        return hours


SOURCES: dict[str, SourceSpec] = {
    "gfs": SourceSpec(
        id="gfs",
        manifest_model="GFS",
        product="pgrb2.0p25",
        latest_filename="latest.json",
        # Hourly through f120, then three-hourly through f240.
        steps=((120, 1), (240, 3)),
        # The pressure family ships mean sea level pressure and the four
        # isobaric levels a synoptic chart is read on (850 / 700 / 500 / 250).
        # The upper-air fills are the surfaces those charts carry: 925, 850
        # and 500 hPa temperature, 850, 700 and 500 hPa relative humidity,
        # the 925 and 850 hPa winds with the water vapour flux derived from
        # the 850 hPa one and the specific humidity there (so spfh850 is
        # fetched as an input only — it also feeds the 850 hPa equivalent
        # potential temperature the converter derives), and the 250 hPa wind
        # for the jet. Then the surface diagnostics — gust, the total and
        # the three cloud layers, CAPE, visibility, dew point and apparent
        # temperature — and the vertical velocity on the three surfaces a
        # rainfall chart reads ascent on. Then the ocean: the surface (skin)
        # temperature, which is the SST over water, and the two sea ice
        # fields from pgrb2, and the significant wave height, primary wave
        # period and direction from the cycle's GFS-Wave file — the
        # direction as an input only, like spfh850: it ships inside the
        # wave vector the converter derives from it and the height, not as
        # a scalar of its own. Every other registered level stays
        # unpublished, which keeps the fetch at thirty-seven pgrb2 records
        # and three wave records per frame.
        input_variable_ids=(
            "tmp2m",
            "prate",
            "ugrd10m",
            "vgrd10m",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "hgt250",
            "tmp925",
            "tmp850",
            "tmp500",
            "rh850",
            "rh700",
            "rh500",
            "spfh850",
            "ugrd925",
            "vgrd925",
            "ugrd850",
            "vgrd850",
            "ugrd250",
            "vgrd250",
            "gust",
            "tcdc",
            "lcdc",
            "mcdc",
            "hcdc",
            "cape",
            "vis",
            "dpt2m",
            "aptmp2m",
            "vvel850",
            "vvel700",
            "vvel500",
            "tmpsfc",
            "icec",
            "icetk",
            "htsgw",
            "perpw",
            "dirpw",
        ),
        accumulated_precipitation=False,
        companion_files=(CompanionFile(id="wave", variable_ids=("htsgw", "perpw", "dirpw")),),
        bundle_scalar_ids=(
            "tmp2m",
            "prate",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "hgt250",
            "tmp925",
            "tmp850",
            "tmp500",
            "rh850",
            "rh700",
            "rh500",
            "gust",
            "tcdc",
            "lcdc",
            "mcdc",
            "hcdc",
            "cape",
            "vis",
            "dpt2m",
            "aptmp2m",
            "vvel850",
            "vvel700",
            "vvel500",
            "thetae850",
            "tmpsfc",
            "icec",
            "icetk",
            "htsgw",
            "perpw",
        ),
        bundle_vector_ids=("wind10m", "wind925", "wind850", "wind250", "qflux850", "wave"),
    ),
    "ecmwf": SourceSpec(
        id="ecmwf",
        manifest_model="ECMWF",
        product="ifs-0p25",
        latest_filename="latest-ecmwf.json",
        # Three-hourly through 144 hours, then six-hourly through 240.
        steps=((144, 3), (240, 6)),
        # The same pressure family and upper-air fills as GFS, from the
        # open data pressure-level records (``levtype`` ``pl``), so the two
        # models offer one set of layers and switching between them never
        # loses one. ECMWF ``msl`` is matched through the registry's 0/3/0
        # alias.
        input_variable_ids=(
            "tmp2m",
            "tp",
            "ugrd10m",
            "vgrd10m",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "hgt250",
            "tmp925",
            "tmp850",
            "tmp500",
            "rh850",
            "rh700",
            "rh500",
            "spfh850",
            "ugrd925",
            "vgrd925",
            "ugrd850",
            "vgrd850",
            "ugrd250",
            "vgrd250",
        ),
        accumulated_precipitation=True,
        bundle_scalar_ids=(
            "tmp2m",
            "prate",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "hgt250",
            "tmp925",
            "tmp850",
            "tmp500",
            "rh850",
            "rh700",
            "rh500",
        ),
        bundle_vector_ids=("wind10m", "wind925", "wind850", "wind250", "qflux850"),
    ),
    # GFS surface flux files on the native ~13 km T1534 Gaussian grid
    # (3072x1536; GDAL reports a uniform geoTransform whose tiny latitude
    # deviation from the true Gaussian latitudes is far below a pixel).
    # Adds the dswrf solar-radiation layer; prate is de-averaged from the
    # window-cumulative PRATE averages.
    "sflux": SourceSpec(
        id="sflux",
        manifest_model="GFS-SFLUX",
        product="sfluxgrb",
        latest_filename="latest-sflux.json",
        # Same cadence as pgrb2: hourly through f120, three-hourly to f240.
        steps=((120, 1), (240, 3)),
        input_variable_ids=("tmp2m", "prate_ave", "ugrd10m", "vgrd10m", "dswrf"),
        accumulated_precipitation=False,
        averaged_precipitation=True,
        optional_at_analysis=("prate_ave",),
        bundle_scalar_ids=("tmp2m", "prate", "dswrf"),
        bundle_vector_ids=("wind10m",),
        production_grid=(3072, 1536),
        # 3072 x 1536 divides exactly into 32 x 16 = 512 tiles with no
        # clipped edge, at about the same 11-degree ground scale as the
        # 0p25 grid's 48 x 52.
        tile=(96, 96),
    ),
    # NOAA HRRR: the 3 km convection-allowing model over the contiguous
    # United States, a new cycle every hour, hourly to F18 (the four
    # synoptic cycles run to F48, which is not published here so every
    # cycle reads the same). Computed on a Lambert conformal conic grid
    # (1799 x 1059), so the encoder resamples every plane onto a regular
    # 0.03° grid over the domain's footprint — about 3.3 km north-south and
    # 2.6 km east-west at the domain's middle latitude — and the bundles
    # carry that grid (xuebuild/reproject.py). The records come from the
    # 2-D surface file (``wrfsfcf``), which carries the surface set and a
    # handful of pressure levels: no 250 hPa height, no isobaric humidity
    # (dew point instead), no apparent temperature. Its sea level pressure
    # is the MAPS reduction (``MSLMA``, matched through the registry's
    # 0/3/198 alias), and the composite reflectivity the model forecasts is
    # published under the radar mosaic's ``cref`` — the same quantity in
    # the same unit, so the shell draws the forecast the way it draws the
    # observation.
    "hrrr": SourceSpec(
        id="hrrr",
        manifest_model="HRRR",
        product="wrfsfc",
        latest_filename="latest-hrrr.json",
        steps=((18, 1),),
        input_variable_ids=(
            "tmp2m",
            "prate",
            "ugrd10m",
            "vgrd10m",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "tmp925",
            "tmp850",
            "tmp500",
            "ugrd925",
            "vgrd925",
            "ugrd850",
            "vgrd850",
            "ugrd250",
            "vgrd250",
            "gust",
            "tcdc",
            "lcdc",
            "mcdc",
            "hcdc",
            "cape",
            "vis",
            "dpt2m",
            "cref",
        ),
        accumulated_precipitation=False,
        bundle_scalar_ids=(
            "tmp2m",
            "prate",
            "prmsl",
            "hgt850",
            "hgt700",
            "hgt500",
            "tmp925",
            "tmp850",
            "tmp500",
            "gust",
            "tcdc",
            "lcdc",
            "mcdc",
            "hcdc",
            "cape",
            "vis",
            "dpt2m",
            "cref",
        ),
        bundle_vector_ids=("wind10m", "wind925", "wind850", "wind250"),
        # The 0.03° grid over the footprint of the 1799 x 1059 domain: the
        # north-west corner is at 134.10 W, 52.62 N and the grid runs to
        # 60.90 W, 21.12 N (reproject.build_resampler snaps the footprint
        # outwards to whole steps).
        production_grid=(2441, 1051),
        # 64 x 64 cells is about 1.9° at this step — 39 x 17 = 663 tiles,
        # each a series of nineteen 4 KB planes.
        tile=(64, 64),
        cycle_hours=1,
        regrid=Regrid(step=0.03),
    ),
    # CMA weather radar level-3 mosaic composite reflectivity, decoded from
    # the published BIN tiles into a NetCDF series by the radar-l3-mst
    # tool. An observation source: one local file per event rather than a
    # cycle on a bucket, no live pointer, and no cron job — the archive is
    # not (yet) a dependable operational feed, so it reaches Xue only as
    # showcase cases someone builds by hand.
    "radar": SourceSpec(
        id="radar",
        manifest_model="CMA-RADAR",
        product="l3-mst-cref",
        latest_filename=None,
        steps=(),
        input_variable_ids=("cref",),
        accumulated_precipitation=False,
        bundle_scalar_ids=("cref",),
        # Tile-grid dependent: the file says what it covers, and nothing here
        # is ever built with require_complete.
        production_grid=(0, 0),
        # The mosaic arrives on a 256 * 2^z tile grid, which any power of two
        # divides; 64 keeps a chunk in the same tens-of-KB range as the
        # forecast sources whatever z the event carries.
        tile=(64, 64),
        observation=True,
    ),
}

MODEL_PRODUCTS: dict[str, str] = {spec.manifest_model: spec.product for spec in SOURCES.values()}


def source_spec(model: str) -> SourceSpec:
    try:
        return SOURCES[model]
    except KeyError as exc:
        raise DownloadError(f"unsupported model: {model}") from exc
