"""Which encoder a build runs through.

Two implementations of the same conversion live in this repository, and they
are held to byte-for-byte identical output:

* `binconvert` — the reference, pure Python driving `gdal_translate`, `zstd`
  and `ffmpeg` as subprocesses. It is what a format change lands in first, and
  it runs anywhere GDAL does.
* `native` — the same conversion inside the `xuepy` wheel, with GDAL, grib-rs
  and zstd linked in. Roughly an order of magnitude less process churn, which
  is the whole reason a scheduled build wants it.

A build takes the native encoder when the wheel is installed. `XUE_ENCODER`
overrides that: `native` insists on it (and fails loudly if the wheel is
missing), `python` insists on the reference, `auto` is the default.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import binconvert, native
from .errors import ConversionError

LOG = logging.getLogger("xue.encoder")

SELECTION_VARIABLE = "XUE_ENCODER"
SELECTIONS = ("auto", "native", "python")


def selection() -> str:
    """The requested policy, as set in the environment."""
    requested = os.environ.get(SELECTION_VARIABLE, "auto").strip().lower() or "auto"
    if requested not in SELECTIONS:
        raise ConversionError(
            f"{SELECTION_VARIABLE} must be one of {', '.join(SELECTIONS)}, not {requested!r}"
        )
    return requested


def resolve() -> str:
    """The encoder this build will actually use: ``native`` or ``python``."""
    requested = selection()
    if requested == "python":
        return "python"
    if requested == "native":
        # Fail here rather than silently building the slow way: a scheduled
        # run that asked for the native encoder wants to know it is missing.
        native.require()
        return "native"
    if native.available():
        return "native"
    LOG.info(
        "%s is not installed; converting with the Python pipeline "
        "(set %s=native to require the wheel)",
        native.DISTRIBUTION,
        SELECTION_VARIABLE,
    )
    return "python"


def convert_bin(
    input_path: Path | Sequence[Path], output_dir: Path, **options: Any
) -> dict[str, Any]:
    """Convert a run through the selected encoder.

    A thin dispatch: both sides take the same arguments and return the same
    report, so nothing here knows what a bundle is.
    """
    implementation = native if resolve() == "native" else binconvert
    return implementation.convert_bin(input_path, output_dir, **options)
