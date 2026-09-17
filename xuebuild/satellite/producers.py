"""Producers: composite products derived from a window's channels.

A producer's contract is the window's NetCDF series (:mod:`assemble`):
the same regular grid, the same time axis, one packed variable per
channel — plus whatever ancillary objects it names — in, and one or more
new variables on the same grid and the same (or a coarser) axis out. It
never touches quantization, the container, the store or the manifest: its
outputs go into the series (or a sibling ``window.<producer>.nc``), and
the converter reads them as more channels. Two kinds share the one
interface: an in-process NumPy function with a fixed operation order,
which can be held to a golden, and an external one that runs elsewhere
and writes to the shared bucket, which the pipeline reads back the way it
reads the CMA archive. A produced variable's identity is a local-use
GRIB2 parameter with the ``producer`` block beside it (docs/format.md
§"Band and Producer").

Nothing is registered yet; the interface is fixed now so the first
producer is a class here and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Producer(Protocol):
    id: str
    """The ``producer.id`` written into the metadata: ``shachen``."""
    version: str
    """The ``producer.version`` written into the metadata."""
    inputs: tuple[str, ...]
    """The channel ids the window must carry."""
    outputs: tuple[str, ...]
    """The variable ids it adds."""
    ancillaries: tuple[str, ...]
    """Objects on the bucket the pipeline pulls beside the window before
    running it (a cloud-cleared background, a climatology), by name."""

    def run(self, window: Path, ancillary: dict[str, Path]) -> Path:
        """Read the window's series, write the outputs into it or into a
        sibling file, and return the file the outputs are in."""
        ...


PRODUCERS: dict[str, Producer] = {}
