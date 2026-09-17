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
back the way it reads the CMA archive. The first producer is the classic
Dust RGB through the ``shachen`` package, in process.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Protocol

import numpy as np

from ..errors import ConversionError
from .platforms import Platform


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
    """Objects on the bucket the pipeline pulls beside the window before
    running it (a cloud-cleared background, a climatology), by name."""

    @property
    def version(self) -> str:
        """The ``producer.version`` written into the metadata: the
        algorithm's own, as installed."""
        ...

    def inputs_for(self, platform: Platform) -> tuple[str, ...]:
        """The channel ids the recipe reads on this platform — ``inputs``
        unless the imager lacks one and the recipe has a stand-in."""
        ...

    def run(self, platform: Platform, inputs: dict[str, np.ndarray], ancillary: dict[str, Path]) -> dict[str, np.ndarray]:
        """Derive the outputs of one slot from its input planes (float64,
        the channel's unit, NaN where no data), one plane per output id,
        NaN where any input was missing."""
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

    def run(self, platform: Platform, inputs: dict[str, np.ndarray], ancillary: dict[str, Path]) -> dict[str, np.ndarray]:
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


PRODUCERS: dict[str, Producer] = {DustRGBProducer().bundle_id: DustRGBProducer()}
"""By the composite bundle each produces."""


def producer_for(bundle_id: str) -> Producer:
    try:
        return PRODUCERS[bundle_id]
    except KeyError as exc:
        raise ConversionError(f"no producer is registered for the {bundle_id!r} bundle") from exc
