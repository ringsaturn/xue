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
it (docs/format.md §"Band and Producer"): the id is registered on the
variable (:attr:`xuebuild.variables.VariableSpec.producer_id`), the
version is stamped on the series by the producer that ran, and both
converters read it off the series.

Two kinds share the interface: an in-process NumPy function with a fixed
operation order, which can be held to a golden, and an external one that
runs elsewhere and writes to the shared bucket, which the pipeline reads
back the way it reads the CMA archive. Both producers here are the first
kind, through the ``shachen`` package: the classic Dust RGB, which reads
the slot's channels and nothing else, and DEBRA, which also reads two
ancillary fields (:mod:`ancillary`) the fetch stage puts on disk for the
slot before the producer runs (:meth:`Producer.ancillary_for`).
"""

from __future__ import annotations

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
    """The composite bundle the outputs are the components of."""
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

    The product-layer gate the operational DEBRA pipeline paints by
    (its ``cf_gated``): a cell whose ``dt1`` and ``dt2`` (Eqs. 13–14 as
    DEBRA produced them) are both at or below 0 reads 0, since cloud
    has no split-window signal and dust nearly always does — the one
    failure it removes is the mid-level cloud too warm for CM1 and too
    thin for CM3, which Eq. 15's thermal-contrast test alone reads as
    dust. It works around the clock and it removes without reordering;
    what it costs, on 42 dust days of East Asian station data, is a
    daytime detection rate at confidence 0.1 of 0.411 → 0.373 for a
    false-alarm ratio of 0.422 → 0.23. Not part of DEBRA (Eqs. 1–29)
    and not in ``shachen``. A NaN test counts as "did not respond"; a
    NaN confidence stays NaN."""
    d1 = np.nan_to_num(np.asarray(dt1, dtype=np.float64), nan=0.0)
    d2 = np.nan_to_num(np.asarray(dt2, dtype=np.float64), nan=0.0)
    cf = np.asarray(confidence, dtype=np.float64)
    return np.where((d1 <= 0.0) & (d2 <= 0.0) & np.isfinite(cf), 0.0, cf)


@dataclass(frozen=True)
class DebraProducer:
    """DEBRA (Miller et al. 2017, doi:10.1002/2017JD027365), the Dynamic
    Enhancement Background Reduction Algorithm for dust: Eqs. 1–22 as the
    ``shachen`` package implements them, run per slot on five infrared
    windows — the 3.9 µm window and the 6.2 µm water vapour band for the
    cloud mask, the 8.6, 10.4 and 12.3 µm windows for the mask, the
    split-window dust tests and the thermal contrast — against a
    semianalytic clear-sky background (the CAMEL emissivity times a Planck
    curve at the GFS skin temperature, :mod:`ancillary`), with the
    ``shachen`` ABI retune (the Eq. 19 daytime floor at 0.40 rather than
    the printed 0.25, and the night branch on its own interval) on every
    imager, as the operational pipeline runs it. The one output is the
    combined confidence factor ``cf_comb`` in 0–1 with the
    :func:`split_window_gate` applied — the field that pipeline paints —
    as the ``dustcf`` bundle's one variable.

    The chain is composed here from ``shachen``'s per-equation modules
    rather than through ``shachen.pipeline.run_debra``, because that entry
    point takes a pyresample area and regrids, finds the sun and masks
    land itself; on the plate carrée grid the bundles carry, whose
    longitudes may run past 180°, those three are this module's
    (:func:`ancillary.regrid`, pyorbital's zenith on the grid's own
    coordinates, the land mask on wrapped ones). The arithmetic of every
    equation is ``shachen``'s, in its order.

    Where the confidence is defined: a cell is computed wherever every
    input channel has data and the ground is either water (DEBRA takes
    the water surface's emissivity as unity and needs no climatology
    there) or inside the extent of a staged CAMEL file; land no file
    reaches is no data, so staging a region is what extends the product
    over it, and nothing is computed against an emissivity that was
    never read."""

    id: str = "shachen"
    bundle_id: str = "dustcf"
    inputs: tuple[str, ...] = ("ir039", "wv062", "ir086", "ir104", "ir123")
    outputs: tuple[str, ...] = ("dustcf",)
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
            from shachen.confidence import confidence  # noqa: PLC0415
            from shachen.constants import ABI_TUNED  # noqa: PLC0415
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
        constants = ABI_TUNED
        background = background_signals(skin_da, emissivity_ds)
        mask = cloud_mask(scene, skin_da, constants.cloud_mask)
        tests = dust_tests(scene, background, skin_da, xr.DataArray(is_land, dims=dims), constants.dust_tests)
        combined = confidence(tests, mask, xr.DataArray(zenith, dims=dims), constants.confidence)
        gated = split_window_gate(np.asarray(combined["cf_comb"].values), np.asarray(tests["dt1"].values), np.asarray(tests["dt2"].values))

        # Defined where every input has data and the ground is water or
        # staged; NaN everywhere else, which the frame packs as no data.
        defined = staged | ~is_land
        for channel_id in needed:
            defined &= np.isfinite(inputs[channel_id])
        defined &= np.isfinite(skin)
        plane = np.where(defined, gated, np.nan)
        return {"dustcf": np.clip(plane, 0.0, 1.0)}


PRODUCERS: dict[str, Producer] = {
    producer.bundle_id: producer for producer in (DustRGBProducer(), DebraProducer())
}
"""By the composite bundle each produces."""


def producer_for(bundle_id: str) -> Producer:
    try:
        return PRODUCERS[bundle_id]
    except KeyError as exc:
        raise ConversionError(f"no producer is registered for the {bundle_id!r} bundle") from exc
