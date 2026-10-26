"""Producers: composite products derived from a slot's channels.

A producer's contract is the fetch stage's frames (:mod:`assemble`): the
channels of one slot, already warped onto the regular grid, as float
planes in the channel's unit with NaN where the frame carries no data,
in — plus whatever ancillary objects it names — and one or more new
planes on the same grid out, each written as a frame of its own and
cached beside the channels', so a round computes the slot it warped and
reads the rest back. It never touches quantization, the container, the
store or the manifest: its outputs are stacked into the window's series
like channels (:func:`assemble.write_series`), and the converter reads
them as more variables — the components of one composite bundle
(:data:`xuebuild.binconvert.COMPOSITE_BUNDLES`). A produced variable's
identity is a local-use GRIB2 parameter with the ``producer`` block beside
it (docs/format.md §"Band, Producer and Aerosol"): the id is registered on the
variable (:attr:`xuebuild.variables.VariableSpec.producer_id`), the
version is stamped on the series by the producer that ran, and both
converters read it off the series.

Two kinds share the interface: an in-process NumPy function with a fixed
operation order, which can be held to a golden, and an external one that
runs elsewhere and writes to the shared bucket, which the pipeline reads
back the way it reads the CMA archive. The producers here are all the
first kind. Two run through the ``shachen`` package: the classic Dust
RGB, which reads the slot's infrared windows and nothing else, and the
dust confidences — DEBRA's and ZHOUYE's, two bundles of one variable each
from one pass over the same chain — which also read two ancillary fields
(:mod:`ancillary`) the fetch stage puts on disk for the slot before the
producer runs (:meth:`Producer.ancillary_for`). The third is this
pipeline's own: the true colour composite, one picture by day and by
night — the slot's visible and near-infrared reflectances where the sun
is up, white cloud from two infrared windows where it is down, blended
across the terminator by the sun's position. A producer's outputs are
the components of one or more composite bundles
(:attr:`Producer.bundle_ids`); the fetch stage runs it once for all of
them and caches every output frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC as _UTC, datetime
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from ..errors import ConversionError
from . import ancillary as _ancillary
from .platforms import Platform
from .projector import TargetGrid


class Producer(Protocol):
    id: str
    """The ``producer.id`` written into the metadata: ``shachen``."""
    bundle_id: str
    """The first of :attr:`bundle_ids`: what the fetch stage names the
    producer by in its logs and records."""
    bundle_ids: tuple[str, ...]
    """The composite bundles the outputs are the components of, in
    output order — one for most producers; the dust confidences share a
    pass, so theirs are two. :data:`PRODUCERS` is keyed by every one."""
    inputs: tuple[str, ...]
    """The channel ids the recipe reads on an imager that has them all,
    in the order :meth:`run` takes them; :meth:`inputs_for` is what one
    platform must carry."""
    outputs: tuple[str, ...]
    """The variable ids it adds, in bundle order."""
    ancillaries: tuple[str, ...]
    """What the recipe reads beside the channels, by name — the staged
    emissivity climatology, a skin temperature — each a file or a
    directory under the fetch's ancillary root that :meth:`ancillary_for`
    resolves (and fetches, when it is fetched per slot) before
    :meth:`run`. Empty for a recipe of the channels alone."""

    @property
    def version(self) -> str:
        """The ``producer.version`` written into the metadata: the
        algorithm's own, as installed."""
        ...

    def inputs_for(self, platform: Platform) -> tuple[str, ...]:
        """The channel ids the recipe reads on this platform — ``inputs``
        unless the imager lacks one and the recipe has a stand-in."""
        ...

    def ancillary_for(self, platform: Platform, slot: datetime, root: Path | None) -> dict[str, Path]:
        """The paths of :attr:`ancillaries` for one slot under ``root``
        (``<raw root>/ancillary``), fetched or located: a slot's skin
        temperature record, the month's staged emissivity. Raises when
        ``root`` is None and the recipe needs one."""
        ...

    def run(
        self,
        platform: Platform,
        inputs: dict[str, np.ndarray],
        ancillary: dict[str, Path],
        *,
        slot: datetime,
        grid: TargetGrid,
    ) -> dict[str, np.ndarray]:
        """Derive the outputs of one slot from its input planes (float64,
        the channel's unit, NaN where no data) on ``grid``, one plane per
        output id, NaN where any input was missing. ``slot`` is the
        scan's start, for a recipe that reads the sun's position."""
        ...


@dataclass(frozen=True)
class DustRGBProducer:
    """The classic Dust RGB (Lensky and Rosenfeld 2008, as EUMeTrain's
    recipe compilation and the GOES-R Quick Guide fix it): red the
    12.3 − 10.4 µm split window, green 11.2 − 8.6 µm with a gamma, blue
    the 10.4 µm window, each stretched to 0–1 — the three guns of the
    ``dustrgb`` bundle, computed by :func:`shachen.dustrgb.dust_rgb`.

    The stretches are per imager (the scheme was tuned for SEVIRI and
    re-tuned for ABI): ``shachen`` picks them from a satpy reader name,
    which the fetch stage has none of, so the choice is made here by the
    platform's instrument — the Quick Guide's values for ABI, the SEVIRI
    ones for AHI and FCI, the same rule as
    ``shachen.constants.DUST_RGB_BY_READER``. The green gun's minuend is
    per imager too: the 11.2 µm window on AHI and ABI, and on an imager
    without one (FCI, SEVIRI) the 10.4 µm window stands in, which is the
    original SEVIRI recipe's ``IR10.8 − IR8.7`` and what satpy's generic
    composite does by nearest wavelength. ``shachen`` is the ``satellite``
    dependency group, imported here and nowhere else."""

    id: str = "shachen"
    bundle_id: str = "dustrgb"
    bundle_ids: tuple[str, ...] = ("dustrgb",)
    inputs: tuple[str, ...] = ("ir086", "ir104", "ir112", "ir123")
    outputs: tuple[str, ...] = ("dustr", "dustg", "dustb")
    ancillaries: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        try:
            return distribution_version("shachen")
        except PackageNotFoundError as exc:
            raise ConversionError("the Dust RGB producer needs the shachen package: uv sync --group satellite") from exc

    #: The 11.2 µm window's stand-in on an imager without one.
    GREEN_STAND_IN: str = "ir104"

    def inputs_for(self, platform: Platform) -> tuple[str, ...]:
        """The four windows on AHI and ABI; three on FCI, whose green gun
        reads the 10.4 µm window in place of the 11.2 µm one."""
        channel_ids = {channel.id for channel in platform.channels}
        missing = [channel_id for channel_id in self.inputs if channel_id not in channel_ids]
        if missing == ["ir112"]:
            return tuple(channel_id for channel_id in self.inputs if channel_id != "ir112")
        if missing:
            raise ConversionError(f"{platform.spacecraft} {platform.instrument} has no {missing} for the Dust RGB")
        return self.inputs

    def ancillary_for(self, platform: Platform, slot: datetime, root: Path | None) -> dict[str, Path]:
        return {}

    def run(
        self,
        platform: Platform,
        inputs: dict[str, np.ndarray],
        ancillary: dict[str, Path],
        *,
        slot: datetime,
        grid: TargetGrid,
    ) -> dict[str, np.ndarray]:
        try:
            import xarray as xr  # noqa: PLC0415 - the satellite dependency group
            from shachen.constants import DUST_RGB, DUST_RGB_ABI  # noqa: PLC0415
            from shachen.dustrgb import dust_rgb  # noqa: PLC0415
        except ImportError as exc:
            raise ConversionError("the Dust RGB producer needs the shachen package: uv sync --group satellite") from exc
        needed = self.inputs_for(platform)
        missing = [channel_id for channel_id in needed if channel_id not in inputs]
        if missing:
            raise ConversionError(f"the Dust RGB producer needs {list(needed)}; missing {missing}")
        shape = inputs[needed[0]].shape
        if any(inputs[channel_id].shape != shape for channel_id in needed):
            raise ConversionError("the Dust RGB producer's inputs are not on one grid")
        green_minuend = "ir112" if "ir112" in needed else self.GREEN_STAND_IN
        scene = xr.Dataset(
            {
                "bt_tir_86": xr.DataArray(np.asarray(inputs["ir086"], dtype=np.float64), dims=("y", "x")),
                "bt_tir_104": xr.DataArray(np.asarray(inputs["ir104"], dtype=np.float64), dims=("y", "x")),
                "bt_tir_112": xr.DataArray(np.asarray(inputs[green_minuend], dtype=np.float64), dims=("y", "x")),
                "bt_tir_123": xr.DataArray(np.asarray(inputs["ir123"], dtype=np.float64), dims=("y", "x")),
            }
        )
        constants = DUST_RGB_ABI if platform.instrument == "ABI" else DUST_RGB
        composite = dust_rgb(scene, constants)
        guns = np.asarray(composite.values, dtype=np.float64)
        if guns.shape != shape + (3,):
            raise ConversionError(f"the Dust RGB producer returned {guns.shape}, not {shape + (3,)}")
        # A cell any input lacked is no data in every gun: shachen lets NaN
        # through per gun, and a gun whose own inputs happen to be present
        # must not colour a cell the others cannot.
        missing_cells = np.zeros(shape, dtype=bool)
        for channel_id in needed:
            missing_cells |= ~np.isfinite(inputs[channel_id])
        planes: dict[str, np.ndarray] = {}
        for index, output_id in enumerate(self.outputs):
            plane = guns[..., index].copy()
            plane[missing_cells] = np.nan
            planes[output_id] = plane
        return planes


def _naive_utc(slot: datetime) -> datetime:
    """pyorbital takes a naive UTC time."""
    return slot.astimezone(_UTC).replace(tzinfo=None) if slot.tzinfo is not None else slot


def split_window_gate(confidence: np.ndarray, dt1: np.ndarray, dt2: np.ndarray) -> np.ndarray:
    """``confidence`` with 0 where neither split-window test responded.

    The product-layer gate: a cell whose ``dt1`` and ``dt2`` (Eqs. 13–14
    as DEBRA produced them) are both at or below 0 reads 0, since cloud
    has no split-window signal and dust nearly always does — the one
    failure it removes is the mid-level cloud too warm for CM1 and too
    thin for CM3, which Eq. 15's thermal-contrast test alone reads as
    dust. It works around the clock and it removes without reordering;
    what it costs, on 42 dust days of East Asian station data, is a
    daytime detection rate at confidence 0.1 of 0.411 → 0.373 for a
    false-alarm ratio of 0.422 → 0.23. Not part of DEBRA (Eqs. 1–29) and
    not in ``shachen``. ``dustcf`` applies it to the blended ``cf_comb``;
    ``zhouye`` to the normalized day factor alone, since ZHOUYE applies
    the same corroboration to the terminator and night sums itself with
    its own DT2 reading. A NaN test counts as "did not respond"; a NaN
    confidence stays NaN."""
    d1 = np.nan_to_num(np.asarray(dt1, dtype=np.float64), nan=0.0)
    d2 = np.nan_to_num(np.asarray(dt2, dtype=np.float64), nan=0.0)
    cf = np.asarray(confidence, dtype=np.float64)
    return np.where((d1 <= 0.0) & (d2 <= 0.0) & np.isfinite(cf), 0.0, cf)


@dataclass(frozen=True)
class DebraProducer:
    """The two dust confidences on DEBRA's chain, from one pass. DEBRA
    (Miller et al. 2017, doi:10.1002/2017JD027365, the Dynamic
    Enhancement Background Reduction Algorithm for dust) is Eqs. 1–22 as
    the ``shachen`` package implements them, run per slot on five
    infrared windows — the 3.9 µm window and the 6.2 µm water vapour band
    for the cloud mask, the 8.6, 10.4 and 12.3 µm windows for the mask,
    the split-window dust tests and the thermal contrast — against a
    semianalytic clear-sky background (the CAMEL emissivity times a Planck
    curve at the GFS skin temperature, :mod:`ancillary`), with the
    ``shachen`` ABI retune (the Eq. 19 daytime floor at 0.40 rather than
    the printed 0.25, and the night branch on its own interval) on every
    imager. ``dustcf`` is its combined confidence factor ``cf_comb`` in
    0–1 with the :func:`split_window_gate` applied.

    ``zhouye`` is ZHOUYE (昼夜, "day and night",
    ``shachen.constants.ZHOUYE``), the diurnally consistent scheme built
    on the same chain: the same background, cloud mask and tests, with
    three departures that act on the terminator and night sums
    (Eqs. 17–18) only — DT2 read on a fixed (−0.5, +1.5) K interval
    instead of Eq. 14, the thermal-contrast test needing corroboration
    from a split-window test, and Eq. 19 intervals fitted so that lit
    dust reads at night as it does by day. The daytime sum and its
    interval are the ABI retune's, so below a solar zenith of 75° the
    two confidences agree bit for bit; the gate is applied to ZHOUYE's
    normalized day factor alone (the same corroboration it applies to
    the other two sums itself) and the Eq. 22 blend redone through
    ``shachen``'s own ``blend_confidence``. The two are separate
    products: ``dustcf`` fades at dusk as DEBRA does, ``zhouye`` does
    not, and the night false-alarm rate ZHOUYE pays for that is its own.

    The chain is composed here from ``shachen``'s per-equation modules
    rather than through ``shachen.pipeline.run_debra`` / ``run_zhouye``,
    because those entry points take a pyresample area and regrid, find
    the sun and mask land themselves; on the plate carrée grid the
    bundles carry, whose longitudes may run past 180°, those three are
    this module's (:func:`ancillary.regrid`, pyorbital's zenith on the
    grid's own coordinates, the land mask on wrapped ones). The
    arithmetic of every equation is ``shachen``'s, in its order; only the
    re-blend after ZHOUYE's day gate is this module's, through
    ``shachen``'s Eq. 22. The background, mask and tests are computed
    once and both confidences read them, which is why one producer
    serves two bundles.

    Where the confidence is defined: a cell is computed wherever every
    input channel has data and the ground is either water (DEBRA takes
    the water surface's emissivity as unity and needs no climatology
    there) or inside the extent of a staged CAMEL file; land no file
    reaches is no data, so staging a region is what extends the product
    over it, and nothing is computed against an emissivity that was
    never read."""

    id: str = "shachen"
    bundle_id: str = "dustcf"
    bundle_ids: tuple[str, ...] = ("dustcf", "zhouye")
    inputs: tuple[str, ...] = ("ir039", "wv062", "ir086", "ir104", "ir123")
    outputs: tuple[str, ...] = ("dustcf", "zhouye")
    ancillaries: tuple[str, ...] = ("camel", "skin")
    #: How the skin temperature is fetched; None takes the network
    #: through ``xuebuild.fetch``. Injected by the tests.
    fetch_text: Callable[[str], str] | None = field(default=None, compare=False, repr=False)
    fetch_range: Callable[[str, object], bytes] | None = field(default=None, compare=False, repr=False)
    exists: Callable[[str], bool] | None = field(default=None, compare=False, repr=False)

    @property
    def version(self) -> str:
        try:
            return distribution_version("shachen")
        except PackageNotFoundError as exc:
            raise ConversionError("the DEBRA producer needs the shachen package: uv sync --group satellite") from exc

    def inputs_for(self, platform: Platform) -> tuple[str, ...]:
        """The five windows, every one required: DEBRA has no stand-in
        for a missing channel."""
        channel_ids = {channel.id for channel in platform.channels}
        missing = [channel_id for channel_id in self.inputs if channel_id not in channel_ids]
        if missing:
            raise ConversionError(f"{platform.spacecraft} {platform.instrument} has no {missing} for DEBRA")
        return self.inputs

    def ancillary_for(self, platform: Platform, slot: datetime, root: Path | None) -> dict[str, Path]:
        """The staged emissivity directory and the slot's skin temperature
        record, the latter fetched from the GFS bucket when it is not
        cached under ``root/gfs``."""
        if root is None:
            raise ConversionError("the DEBRA producer needs an ancillary root (the staged CAMEL months and a GFS cache)")
        camel = _ancillary.camel_directory(root)
        if not _ancillary.staged_months(camel):
            raise ConversionError(f"no CAMEL emissivity is staged under {camel}; run `make pull-r2-ancillary`")
        skin = _ancillary.fetch_skin_temperature(
            slot,
            _ancillary.gfs_directory(root),
            fetch_text=self.fetch_text,
            fetch_range=self.fetch_range,
            exists=self.exists,
        )
        return {"camel": camel, "skin": skin.path}

    def run(
        self,
        platform: Platform,
        inputs: dict[str, np.ndarray],
        ancillary: dict[str, Path],
        *,
        slot: datetime,
        grid: TargetGrid,
    ) -> dict[str, np.ndarray]:
        try:
            import xarray as xr  # noqa: PLC0415 - the satellite dependency group
            from global_land_mask import globe  # noqa: PLC0415
            from pyorbital.astronomy import cos_zen  # noqa: PLC0415
            from shachen.background import background_signals  # noqa: PLC0415
            from shachen.cloudmask import cloud_mask  # noqa: PLC0415
            from shachen.confidence import blend_confidence, confidence  # noqa: PLC0415
            from shachen.constants import ABI_TUNED, ZHOUYE  # noqa: PLC0415
            from shachen.dust_tests import dust_tests  # noqa: PLC0415
        except ImportError as exc:
            raise ConversionError("the DEBRA producer needs the shachen package: uv sync --group satellite") from exc
        needed = self.inputs_for(platform)
        missing = [channel_id for channel_id in needed if channel_id not in inputs]
        if missing:
            raise ConversionError(f"the DEBRA producer needs {list(needed)}; missing {missing}")
        shape = (grid.height, grid.width)
        if any(inputs[channel_id].shape != shape for channel_id in needed):
            raise ConversionError(f"the DEBRA producer's inputs are not on the {grid.width} x {grid.height} grid")
        for name in self.ancillaries:
            if name not in ancillary:
                raise ConversionError(f"the DEBRA producer needs the {name!r} ancillary")

        # The two ancillary fields on the grid, and where the emissivity
        # is staged.
        skin = _ancillary.regrid(_ancillary.read_skin_temperature(ancillary["skin"]), grid)
        regions = _ancillary.staged_emissivity(ancillary["camel"], slot)
        emissivity, staged = _ancillary.emissivity_on_grid(regions, grid)

        # The sun and the ground, on the grid's own coordinates: the
        # zenith from pyorbital (periodic in longitude, so a longitude
        # past 180 needs no wrapping), the land mask on wrapped ones.
        lons = grid.first_longitude + np.arange(grid.width, dtype=np.float64) * grid.step
        lats = grid.first_latitude - np.arange(grid.height, dtype=np.float64) * grid.step
        lon2d, lat2d = np.meshgrid(lons, lats)
        with np.errstate(invalid="ignore"):
            zenith = np.degrees(np.arccos(np.clip(cos_zen(_naive_utc(slot), lon2d, lat2d), -1.0, 1.0)))
        is_land = globe.is_land(np.clip(lat2d, -90.0, 90.0), (lon2d + 180.0) % 360.0 - 180.0)

        dims = ("y", "x")
        scene = xr.Dataset(
            {
                "bt_swir_39": xr.DataArray(np.asarray(inputs["ir039"], dtype=np.float64), dims=dims),
                "bt_wv_62": xr.DataArray(np.asarray(inputs["wv062"], dtype=np.float64), dims=dims),
                "bt_tir_86": xr.DataArray(np.asarray(inputs["ir086"], dtype=np.float64), dims=dims),
                "bt_tir_104": xr.DataArray(np.asarray(inputs["ir104"], dtype=np.float64), dims=dims),
                "bt_tir_123": xr.DataArray(np.asarray(inputs["ir123"], dtype=np.float64), dims=dims),
            }
        )
        skin_da = xr.DataArray(skin, dims=dims)
        emissivity_ds = xr.Dataset({name: xr.DataArray(plane, dims=dims) for name, plane in emissivity.items()})
        zenith_da = xr.DataArray(zenith, dims=dims)
        # One pass over what the two schemes share: the ZHOUYE constants
        # differ from the ABI retune's in the dust tests only by the
        # second DT2 reading they add, and in the cloud mask not at all.
        background = background_signals(skin_da, emissivity_ds)
        mask = cloud_mask(scene, skin_da, ZHOUYE.cloud_mask)
        tests = dust_tests(scene, background, skin_da, xr.DataArray(is_land, dims=dims), ZHOUYE.dust_tests)
        dt1 = np.asarray(tests["dt1"].values)
        dt2 = np.asarray(tests["dt2"].values)
        # DEBRA reads Eq. 14's DT2 in every sum, so the fixed reading is
        # kept from it; gated whole.
        debra = confidence(tests.drop_vars("dt2_fixed"), mask, zenith_da, ABI_TUNED.confidence)
        dustcf = split_window_gate(np.asarray(debra["cf_comb"].values), dt1, dt2)
        # ZHOUYE: the day branch gated on Eq. 14's DT2 (the reading Eq. 16
        # sums), then Eq. 22 again — Eq. 19 maps a zero sum to 0 on the
        # day interval, so gating the normalized factor is gating the sum.
        combined = confidence(tests, mask, zenith_da, ZHOUYE.confidence)
        cf_day = split_window_gate(np.asarray(combined["cf_day"].values), dt1, dt2)
        zhouye = np.asarray(
            blend_confidence(
                xr.DataArray(cf_day, dims=dims),
                combined["cf_trm"],
                combined["cf_ngt"],
                combined["b_ngt_trm"],
                combined["b_trm_day"],
            ).values
        )

        # Defined where every input has data and the ground is water or
        # staged; NaN everywhere else, which the frame packs as no data.
        defined = staged | ~is_land
        for channel_id in needed:
            defined &= np.isfinite(inputs[channel_id])
        defined &= np.isfinite(skin)
        return {
            "dustcf": np.clip(np.where(defined, dustcf, np.nan), 0.0, 1.0),
            "zhouye": np.clip(np.where(defined, zhouye, np.nan), 0.0, 1.0),
        }


#: The true colour recipe's own version: what the producer stamps on its
#: frames and the converters carry into the ``producer`` block. A cached
#: slot is recomposed when it changes, so it changes exactly when a
#: constant below does, never with the package.
TRUE_COLOR_VERSION = "5"
#: The solar zenith past which the sun-angle normalisation stops growing:
#: a reflectance is divided by ``max(cos θ, cos 85°)``, so the last
#: degrees of a day fade instead of blowing up.
TRUE_COLOR_ZENITH_LIMIT_DEG = 85.0
#: The brightness curve every gun goes through, as reflectance in → gun
#: out, interpolated linearly between the knots and clamped to [0, 1]:
#: NASA Worldview's corrected-reflectance stretch (reflectance 0–1.1
#: spread over 0–255, knots 0, 30, 60, 120, 190, 255 → 0, 110, 160, 210,
#: 240, 255). Its steep foot is what a Rayleigh-corrected picture needs:
#: with the sky's own blue taken out, vegetation and sea sit at a few
#: per cent and a city at ten to fifteen, which a cloud-oriented curve
#: leaves near black.
TRUE_COLOR_CURVE_IN: tuple[float, ...] = tuple(1.1 * knot / 255.0 for knot in (0.0, 30.0, 60.0, 120.0, 190.0, 255.0))
TRUE_COLOR_CURVE_OUT: tuple[float, ...] = tuple(knot / 255.0 for knot in (0.0, 110.0, 160.0, 210.0, 240.0, 255.0))
#: The geostationary altitude above the equator, in kilometres, from
#: which the satellite's zenith and azimuth on every cell are computed.
GEOSTATIONARY_ALTITUDE_KM = 35786.0
#: Where the Rayleigh correction fades out, as the larger of the solar and
#: the satellite zenith in degrees: whole at or below the first, none at or
#: beyond the second, linear between. Single scattering overestimates the
#: sky's path reflectance on long slant paths, so near the terminator and
#: the limb the picture falls back to the uncorrected reflectance instead
#: of going black.
TRUE_COLOR_RAYLEIGH_FADE_DEG: tuple[float, float] = (70.0, 88.0)
#: The sun-normalised 0.64 µm reflectances between which the air the
#: Rayleigh correction assumes above a cell shrinks from the whole column
#: to none: a bright cell is cloud, and a cloud top hides the air beneath
#: it. Taking the whole column's sky out of a cloud strips its blue and
#: tints it yellow to green (pyspectral's red-band reduction, the same two
#: knots).
TRUE_COLOR_RAYLEIGH_CLOUD_REFLECTANCE: tuple[float, float] = (0.2, 1.0)
#: The multiple-scattering lift on the single-scattering path reflectance,
#: as ``1 + k τ m``: single scattering alone leaves the light scattered
#: more than once in the picture, a blue haze over the whole disk that
#: grows towards the limb. 0.25 keeps the sea navy and the cloud white from
#: the sub-satellite point to the terminator on Meteosat frames inverted
#: to reflectance (0.35 tints the limb teal, 0.5 olive).
TRUE_COLOR_RAYLEIGH_MULTIPLE_SCATTERING = 0.25
#: The hybrid green on an imager with a 0.51 µm band (AHI, FCI): the band
#: with a little of the 0.86 µm reflectance, which lifts vegetation to the
#: green the eye expects (Miller et al. 2016, the Himawari correction).
TRUE_COLOR_HYBRID_GREEN: tuple[float, float] = (0.93, 0.07)
#: The synthetic green on an imager without one (ABI): the CIMSS blend of
#: blue, red and the 0.86 µm band, the fractions satpy's ``abi`` green
#: uses.
TRUE_COLOR_SYNTHETIC_GREEN: tuple[float, float, float] = (0.45, 0.45, 0.10)
#: The terminator, as solar zenith in degrees: the day picture alone at or
#: below the first, the night picture alone at or beyond the second (the
#: sun at the horizon, nothing left to reflect), linear between — about
#: the span GeoColor fades its day and night layers over.
TRUE_COLOR_TERMINATOR_DEG: tuple[float, float] = (80.0, 90.0)
#: The night side's ground, red, green and blue: near black with a faint
#: cool cast, so a clear night reads as night and not as missing data
#: (code 0) and cloud stands out on it. The same on every imager, so the
#: disks meet without a seam.
TRUE_COLOR_NIGHT_GROUND: tuple[float, float, float] = (0.03, 0.035, 0.05)
#: The 10.4 µm brightness temperatures, in kelvin, at which a cloud's
#: whiteness is 0 and 1: warmer than 280 K is the ground or the warmest low
#: cloud, colder than 220 K a deep or high cloud top — about the range
#: GeoColor's night-side infrared cloud layer spans.
TRUE_COLOR_CLOUD_TEMPERATURE_K: tuple[float, float] = (280.0, 220.0)
#: The exponent on the infrared ramp: below 1, so a mid-level cloud is
#: already a light grey and high cloud reads white rather than a flat
#: linear grey.
TRUE_COLOR_CLOUD_EXPONENT = 0.75
#: The 10.4 − 3.9 µm differences, in kelvin, at which low cloud's
#: whiteness is 0 and 1: small water droplets emit poorly at 3.9 µm, so
#: fog and stratus — as warm as the ground and invisible at 10.4 µm alone —
#: read several kelvin colder there, while clear sea and vegetated land
#: stay under 2 K (the fog products' threshold).
TRUE_COLOR_LOW_CLOUD_DIFFERENCE_K: tuple[float, float] = (2.0, 7.0)
#: How white low cloud gets at most: a softer grey than high cloud, so the
#: two layers stay apart as they do on GeoColor's night side.
TRUE_COLOR_LOW_CLOUD_WHITE = 0.7


def rayleigh_optical_depth(wavelength_um: float) -> float:
    """The molecular atmosphere's optical depth at a wavelength in µm, at
    standard surface pressure (Hansen and Travis 1974, Eq. 2.29): 0.19 at
    0.47 µm, 0.05 at 0.64 µm, 0.02 at 0.86 µm."""
    w = float(wavelength_um)
    return 0.008569 * w**-4 * (1.0 + 0.0113 * w**-2 + 0.00013 * w**-4)


@dataclass(frozen=True)
class ViewingGeometry:
    """The sun and the satellite seen from every cell of a grid, in
    degrees: zeniths from the vertical, azimuths clockwise from north; and
    the cosine of the solar zenith as pyorbital gives it."""

    sun_cosine: np.ndarray
    sun_zenith: np.ndarray
    sun_azimuth: np.ndarray
    satellite_zenith: np.ndarray
    satellite_azimuth: np.ndarray


def viewing_geometry(platform: Platform, slot: datetime, grid: TargetGrid) -> ViewingGeometry:
    """The sun at the slot's start and the geostationary satellite over
    its sub-satellite longitude, on the grid's own coordinates (pyorbital,
    the satellite dependency group)."""
    try:
        from pyorbital.astronomy import cos_zen, get_alt_az  # noqa: PLC0415 - the satellite dependency group
        from pyorbital.orbital import get_observer_look  # noqa: PLC0415
    except ImportError as exc:
        raise ConversionError("the true colour producer needs pyorbital: uv sync --group satellite") from exc
    lons = grid.first_longitude + np.arange(grid.width, dtype=np.float64) * grid.step
    lats = grid.first_latitude - np.arange(grid.height, dtype=np.float64) * grid.step
    lon2d, lat2d = np.meshgrid(lons, lats)
    when = _naive_utc(slot)
    cosine = np.asarray(cos_zen(when, lon2d, lat2d), dtype=np.float64)
    _, sun_azimuth = get_alt_az(when, lon2d, lat2d)
    satellite_azimuth, elevation = get_observer_look(
        np.array([platform.sub_longitude]), np.array([0.0]), np.array([GEOSTATIONARY_ALTITUDE_KM]), when, lon2d, lat2d, np.zeros_like(lon2d)
    )
    return ViewingGeometry(
        sun_cosine=cosine,
        sun_zenith=np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))),
        sun_azimuth=np.degrees(np.asarray(sun_azimuth, dtype=np.float64)) % 360.0,
        satellite_zenith=90.0 - np.asarray(elevation, dtype=np.float64),
        satellite_azimuth=np.asarray(satellite_azimuth, dtype=np.float64) % 360.0,
    )


def rayleigh_column_fraction(red: np.ndarray) -> np.ndarray:
    """The share of the air column the Rayleigh correction takes out above
    a cell, from its sun-normalised 0.64 µm reflectance: 1 up to
    ``TRUE_COLOR_RAYLEIGH_CLOUD_REFLECTANCE[0]``, 0 from the second,
    linear between; a NaN reads as the whole column."""
    low, high = TRUE_COLOR_RAYLEIGH_CLOUD_REFLECTANCE
    red = np.nan_to_num(np.asarray(red, dtype=np.float64), nan=low)
    return np.clip((high - red) / (high - low), 0.0, 1.0)


def rayleigh_corrected(
    reflectance: np.ndarray, wavelength_um: float, geometry: ViewingGeometry, column: np.ndarray | float = 1.0
) -> np.ndarray:
    """A reflectance already divided by the cosine of the solar zenith,
    with the molecular sky taken out: ``(ρ − ρ_R) / T`` where the path
    reflectance is single scattering over a black surface,
    lifted for the light scattered more than once,
    ``ρ_R = 0.75 (1 + cos² Θ) / (4 (μs + μv)) × (1 − exp(−τ m)) × (1 + k τ m)``
    with the air mass ``m = 1/μs + 1/μv`` and ``k``
    ``TRUE_COLOR_RAYLEIGH_MULTIPLE_SCATTERING``, Θ the scattering angle
    (``cos Θ = −μs μv − sin θs sin θv cos(φs − φv)``) and the two-way
    transmittance ``T = exp(−τ m / 2)`` (half the scattered
    light goes forward and still arrives), both cosines floored at
    ``TRUE_COLOR_ZENITH_LIMIT_DEG``, and τ the band's optical depth times
    the share of the column above the cell (``column``,
    :func:`rayleigh_column_fraction`). The attenuated form saturates where
    the slant path is optically thick; its thin limit ``τ P / (4 μs μv)``
    takes out several times the sky near the terminator and the limb.
    Faded by ``TRUE_COLOR_RAYLEIGH_FADE_DEG`` on the larger zenith, so a
    long slant path keeps the reflectance it had."""
    limit = TRUE_COLOR_ZENITH_LIMIT_DEG
    sun = np.radians(np.minimum(geometry.sun_zenith, limit))
    view = np.radians(np.minimum(geometry.satellite_zenith, limit))
    mu_sun, mu_view = np.cos(sun), np.cos(view)
    relative = np.radians(geometry.sun_azimuth - geometry.satellite_azimuth)
    scattering = -mu_sun * mu_view - np.sin(sun) * np.sin(view) * np.cos(relative)
    tau = rayleigh_optical_depth(wavelength_um) * np.asarray(column, dtype=np.float64)
    airmass = 1.0 / mu_sun + 1.0 / mu_view
    depth = tau * airmass
    path = 0.75 * (1.0 + scattering**2) / (4.0 * (mu_sun + mu_view)) * -np.expm1(-depth) * (1.0 + TRUE_COLOR_RAYLEIGH_MULTIPLE_SCATTERING * depth)
    transmittance = np.exp(-0.5 * tau * airmass)
    start, end = TRUE_COLOR_RAYLEIGH_FADE_DEG
    strength = np.clip((end - np.maximum(geometry.sun_zenith, geometry.satellite_zenith)) / (end - start), 0.0, 1.0)
    return (1.0 - strength) * reflectance + strength * (reflectance - path) / transmittance


def true_color_day_weight(zenith_deg: np.ndarray) -> np.ndarray:
    """The day picture's weight across the terminator: 1 at or below
    ``TRUE_COLOR_TERMINATOR_DEG[0]``, 0 at or beyond the second, linear
    between."""
    start, end = TRUE_COLOR_TERMINATOR_DEG
    return np.clip((end - np.asarray(zenith_deg, dtype=np.float64)) / (end - start), 0.0, 1.0)


def true_color_night_whiteness(ir104: np.ndarray, ir039: np.ndarray) -> np.ndarray:
    """The night side's cloud whiteness, 0–1: the brighter of the
    infrared cloud (``TRUE_COLOR_CLOUD_TEMPERATURE_K``, raised to
    ``TRUE_COLOR_CLOUD_EXPONENT``) and the low cloud the 10.4 − 3.9 µm
    difference shows (``TRUE_COLOR_LOW_CLOUD_DIFFERENCE_K``, scaled to
    ``TRUE_COLOR_LOW_CLOUD_WHITE``). A NaN input reads as no cloud."""
    warm, cold = TRUE_COLOR_CLOUD_TEMPERATURE_K
    ir104 = np.asarray(ir104, dtype=np.float64)
    ir039 = np.asarray(ir039, dtype=np.float64)
    high = np.clip((warm - np.nan_to_num(ir104, nan=warm)) / (warm - cold), 0.0, 1.0) ** TRUE_COLOR_CLOUD_EXPONENT
    low_start, low_end = TRUE_COLOR_LOW_CLOUD_DIFFERENCE_K
    difference = np.nan_to_num(ir104 - ir039, nan=low_start)
    low = TRUE_COLOR_LOW_CLOUD_WHITE * np.clip((difference - low_start) / (low_end - low_start), 0.0, 1.0)
    return np.maximum(high, low)


@dataclass(frozen=True)
class TrueColorProducer:
    """The true colour composite: the three guns of the ``truecolor``
    bundle, one picture by day and by night — the visible picture from a
    slot's 0.47, 0.51 (where the imager has it), 0.64 and 0.86 µm
    reflectances where the sun is up, white cloud from its 10.4 and
    3.9 µm brightness temperatures over a dark ground where it is down —
    this pipeline's own recipe in a fixed order:

    1. every reflectance clipped to 0 from below (the products carry
       small negative reflectances in the dark);
    2. the cosine of the solar zenith on every cell at the slot's start
       (pyorbital on the grid's own coordinates, periodic in longitude),
       and the zenith θ from it;
    3. each reflectance divided by ``max(cos θ, cos TRUE_COLOR_ZENITH_LIMIT_DEG)``;
    3a. each reflectance with the molecular sky taken out at its band's
       wavelength (:func:`rayleigh_corrected`, the satellite's zenith and
       azimuth from the platform's sub-satellite longitude), over the share
       of the air column the cell's 0.64 µm reflectance leaves above it
       (:func:`rayleigh_column_fraction`), faded out on long slant paths;
    4. the green: ``0.93 × 0.51 µm + 0.07 × 0.86 µm`` on AHI and FCI, or
       ``0.45 × 0.47 µm + 0.45 × 0.64 µm + 0.10 × 0.86 µm`` on ABI;
    5. the day guns: red (0.64 µm), green and blue (0.47 µm) through the
       brightness curve, clamped to [0, 1];
    6. the night guns: ``ground + (1 − ground) × w`` on each, ``ground``
       from ``TRUE_COLOR_NIGHT_GROUND`` and ``w`` the cloud whiteness
       (:func:`true_color_night_whiteness`), the same on every imager;
    7. ``a × day + (1 − a) × night`` with ``a`` the day weight
       (:func:`true_color_day_weight`: 1 to 80°, 0 from 90°), clamped to
       [0, 1];
    8. no data in all three guns wherever a side that carries weight
       lacked an input: a reflectance where ``a > 0``, the 10.4 or 3.9 µm
       window where ``a < 1``. Full day keeps its picture without the
       infrared, full night without the reflectances.

    The Rayleigh correction is analytic single scattering, not a lookup
    table: no package and no downloaded table behind it, and within the
    fade it agrees with the tabulated corrections to a few thousandths of
    reflectance, far below what the brightness curve resolves. The id is
    ``xue`` — the recipe is this pipeline's and no package's — and the
    version the recipe's."""

    id: str = "xue"
    bundle_id: str = "truecolor"
    bundle_ids: tuple[str, ...] = ("truecolor",)
    inputs: tuple[str, ...] = ("vis047", "vis051", "vis064", "nir086", "ir039", "ir104")
    outputs: tuple[str, ...] = ("truer", "trueg", "trueb")
    ancillaries: tuple[str, ...] = ()
    night_inputs: tuple[str, ...] = ("ir039", "ir104")
    """The inputs the night side reads; the rest are the day side's
    reflectances (step 8's no-data rule)."""

    @property
    def version(self) -> str:
        return TRUE_COLOR_VERSION

    def inputs_for(self, platform: Platform) -> tuple[str, ...]:
        """The four solar bands and the two infrared windows on an imager
        with a green band; three solar bands on the ABI, whose green is
        synthesized from the other three."""
        channel_ids = {channel.id for channel in platform.channels}
        missing = [channel_id for channel_id in self.inputs if channel_id not in channel_ids]
        if missing == ["vis051"]:
            return tuple(channel_id for channel_id in self.inputs if channel_id != "vis051")
        if missing:
            raise ConversionError(f"{platform.spacecraft} {platform.instrument} has no {missing} for the true colour composite")
        return self.inputs

    def ancillary_for(self, platform: Platform, slot: datetime, root: Path | None) -> dict[str, Path]:
        return {}

    def run(
        self,
        platform: Platform,
        inputs: dict[str, np.ndarray],
        ancillary: dict[str, Path],
        *,
        slot: datetime,
        grid: TargetGrid,
    ) -> dict[str, np.ndarray]:
        needed = self.inputs_for(platform)
        missing = [channel_id for channel_id in needed if channel_id not in inputs]
        if missing:
            raise ConversionError(f"the true colour producer needs {list(needed)}; missing {missing}")
        shape = (grid.height, grid.width)
        if any(inputs[channel_id].shape != shape for channel_id in needed):
            raise ConversionError(f"the true colour producer's inputs are not on the {grid.width} x {grid.height} grid")
        solar = [channel_id for channel_id in needed if channel_id not in self.night_inputs]

        # 1. The reflectances, negative values clipped to 0; NaN kept.
        planes = {channel_id: np.maximum(np.asarray(inputs[channel_id], dtype=np.float64), 0.0) for channel_id in solar}
        # 2. The sun (and the satellite) on the grid's own coordinates.
        geometry = viewing_geometry(platform, slot, grid)
        zenith = geometry.sun_zenith
        cosine = geometry.sun_cosine
        # 3. Divided by the cosine, floored at the limit.
        floor = math.cos(math.radians(TRUE_COLOR_ZENITH_LIMIT_DEG))
        divisor = np.maximum(cosine, floor)
        normalized = {channel_id: plane / divisor for channel_id, plane in planes.items()}
        # 3a. The molecular sky taken out, band by band, over the air the
        # red reflectance leaves above the cell.
        column = rayleigh_column_fraction(normalized["vis064"])
        normalized = {
            channel_id: rayleigh_corrected(plane, platform.channel(channel_id).wavelength_um, geometry, column)
            for channel_id, plane in normalized.items()
        }
        # 4. The green.
        if "vis051" in normalized:
            band, nir = TRUE_COLOR_HYBRID_GREEN
            green = band * normalized["vis051"] + nir * normalized["nir086"]
        else:
            blue, red, nir = TRUE_COLOR_SYNTHETIC_GREEN
            green = blue * normalized["vis047"] + red * normalized["vis064"] + nir * normalized["nir086"]
        # 5. The day guns through the curve.
        day = [
            np.clip(np.interp(np.nan_to_num(gun, nan=0.0), TRUE_COLOR_CURVE_IN, TRUE_COLOR_CURVE_OUT), 0.0, 1.0)
            for gun in (normalized["vis064"], green, normalized["vis047"])
        ]
        # 6. The night guns: white cloud over the dark ground.
        ir104 = np.asarray(inputs["ir104"], dtype=np.float64)
        ir039 = np.asarray(inputs["ir039"], dtype=np.float64)
        whiteness = true_color_night_whiteness(ir104, ir039)
        night = [ground + (1.0 - ground) * whiteness for ground in TRUE_COLOR_NIGHT_GROUND]
        # 7. Across the terminator.
        weight = true_color_day_weight(zenith)
        # 8. No data where a side that carries weight lacked an input.
        day_defined = np.ones(shape, dtype=bool)
        for channel_id in solar:
            day_defined &= np.isfinite(planes[channel_id])
        night_defined = np.isfinite(ir104) & np.isfinite(ir039)
        defined = (day_defined | (weight <= 0.0)) & (night_defined | (weight >= 1.0))
        results: dict[str, np.ndarray] = {}
        for output_id, day_gun, night_gun in zip(self.outputs, day, night):
            blended = np.clip(weight * day_gun + (1.0 - weight) * night_gun, 0.0, 1.0)
            results[output_id] = np.where(defined, blended, np.nan)
        return results


PRODUCERS: dict[str, Producer] = {
    bundle_id: producer
    for producer in (DustRGBProducer(), DebraProducer(), TrueColorProducer())
    for bundle_id in producer.bundle_ids
}
"""By the composite bundle each produces; a producer of two bundles is
listed under both, the same object."""


def producer_for(bundle_id: str) -> Producer:
    try:
        return PRODUCERS[bundle_id]
    except KeyError as exc:
        raise ConversionError(f"no producer is registered for the {bundle_id!r} bundle") from exc
