"""What the Python tests share: skip guards, the byte-identity comparisons
the two encoders are held to, and the scratch directories they build in.

Imported as ``from tests._support import …``, which resolves both under
``unittest discover -s tests`` (the repository root is the working
directory) and under ``unittest tests.test_x``. The leading underscore keeps
discovery's ``test_*.py`` pattern from collecting it.
"""

from __future__ import annotations

import filecmp
import functools
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from xuebuild import grib2, native, zstdcli
from xuebuild.gdal import inspect_grib_multi

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _have(*commands: str) -> bool:
    return all(shutil.which(command) is not None for command in commands)


def _importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


requires_gdalinfo = unittest.skipUnless(_have("gdalinfo"), "gdalinfo is not on PATH")
#: The reference pipeline's extraction: inspection and `gdal_translate`.
requires_gdal = unittest.skipUnless(_have("gdalinfo", "gdal_translate"), "GDAL is not on PATH")
#: The satellite fetch stage, which also mosaics and warps.
requires_gdal_warp = unittest.skipUnless(
    _have("gdalinfo", "gdal_translate", "gdalwarp", "gdalbuildvrt"), "GDAL is not on PATH"
)
@functools.cache
def _fci_unreadable() -> str | None:
    """Why the GDAL on PATH cannot read an FCI chunk, or None when it can.

    The chunks are compressed with a JPEG-LS HDF5 filter that `hdf5plugin`
    ships, and a GDAL built against another HDF5 fails on it with
    "undefined filter" even with the plugin path set; this is the probe
    `publish-meteosat.yml` runs before a round, on the fixture chunk.
    """
    if not _importable("hdf5plugin"):
        return "hdf5plugin is not installed (uv sync --group satellite)"
    if not _have("gdal_translate"):
        return "gdal_translate is not on PATH"
    import hdf5plugin  # noqa: PLC0415

    chunk = next((FIXTURES / "meteosat").glob("*.nc"))
    with tempfile.TemporaryDirectory(prefix="xue-fci-probe-") as scratch:
        # A copy, so GDAL's sidecars never land beside the fixture.
        copy = Path(scratch) / "chunk.nc"
        shutil.copy(chunk, copy)
        result = subprocess.run(
            [
                "gdal_translate", "-q", "-of", "ENVI",
                f'NETCDF:"{copy}":/data/ir_105/measured/effective_radiance', str(Path(scratch) / "probe.bin"),
            ],
            env=dict(os.environ, HDF5_PLUGIN_PATH=str(hdf5plugin.PLUGIN_PATH)),
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        return "the GDAL on PATH cannot decode an FCI chunk through hdf5plugin's filter"
    return None


def requires_hdf5plugin(item):
    """Skip unless an FCI chunk can be read: `hdf5plugin` installed and the
    GDAL on PATH able to use its filter. Probed once, when first applied."""
    reason = _fci_unreadable()
    return unittest.skipIf(reason is not None, reason or "")(item)
requires_shachen = unittest.skipUnless(
    _importable("shachen"), "shachen is not installed (uv sync --group satellite)"
)
requires_native = unittest.skipUnless(native.available(), f"{native.DISTRIBUTION} is not installed")


def native_knows(model: str) -> bool:
    """Whether the installed wheel is there and has ``model`` with every
    bundle this source table publishes for it. A source can land here
    before the wheel that carries it ships."""
    return native.available() and native.knows_source(model)


def requires_native_source(model: str, what: str | None = None):
    """Skip unless the installed wheel knows ``model``."""
    return unittest.skipUnless(
        native_knows(model),
        f"the installed {native.DISTRIBUTION} wheel predates {what or f'the {model} source'}",
    )


def require_comparable_compression(case: unittest.TestCase, reference: dict, subject: dict) -> None:
    """Skip unless both encoders compressed the same way.

    Everything downstream of a compressed payload — the bundles, their CRC32s,
    the manifest that records them, the pointer that CRCs the manifest — is
    only comparable when the two ran the same libzstd the same way.
    """
    if not zstdcli.compresses_in_process():
        case.skipTest(
            "the reference encoder is compressing through the zstd CLI "
            "(Python < 3.14), which streams rather than one-shot: the frames "
            "decode the same but the bytes cannot match"
        )
    if reference["zstdVersion"] != subject["zstdVersion"]:
        case.skipTest(
            f"libzstd differs: the reference has {reference['zstdVersion']}, "
            f"{native.DISTRIBUTION} carries {subject['zstdVersion']}"
        )


def tree_listing(root: Path) -> list[str]:
    """Every path under ``root``, directories included, relative and sorted."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def assert_trees_identical(
    case: unittest.TestCase, reference: Path, subject: Path, expect: Iterable[str] = ()
) -> None:
    """The same paths under both roots, every file byte for byte.

    ``expect`` names relative paths that must be among them, so a parity
    test cannot pass by both sides skipping the same work.
    """
    listing = tree_listing(reference)
    case.assertEqual(listing, tree_listing(subject))
    for relative in expect:
        case.assertIn(relative, listing)
    for relative in listing:
        path = reference / relative
        if path.is_dir():
            continue
        with case.subTest(artifact=relative):
            case.assertTrue(filecmp.cmp(path, subject / relative, shallow=False), f"{relative} differs")


def assert_native_matches(
    case: unittest.TestCase,
    inputs: Path | Sequence[Path],
    reference: Path,
    reference_report: dict,
    subject: Path,
    *,
    model: str,
    **options: Any,
) -> None:
    """Build ``inputs`` through the wheel into ``subject`` the way an
    observation round builds (no video, the manifest beside the bundles)
    and hold it to the reference build in ``reference``, byte for byte."""
    if not zstdcli.compresses_in_process():
        case.skipTest("the reference encoder compresses through the zstd CLI")
    with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
        report = native.convert_bin(
            inputs, subject, model=model, skip_video=True, manifest_path=subject / "manifest.json", **options
        )
    require_comparable_compression(case, reference_report, report)
    assert_trees_identical(case, reference, subject)


def assert_gdalinfo_agrees(
    case: unittest.TestCase, cases: Iterable[tuple[Path, Sequence[str], Sequence[str]]]
) -> None:
    """The GRIB2 header index and GDAL's band metadata find the same frames.

    ``cases`` is ``(path, variable ids, optional ids)`` per file; GDAL is the
    `gdalinfo` subprocess, so the reference pipeline's own reader is what is
    compared.
    """
    with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
        for path, variable_ids, optional_ids in cases:
            fast = grib2.inspect_grib_fast(path, variable_ids, optional_ids=optional_ids)
            slow = inspect_grib_multi(path, variable_ids, optional_ids=optional_ids)
            case.assertEqual(set(fast), set(slow), path.name)
            for variable_id, frame in fast.items():
                case.assertEqual(frame, slow[variable_id], f"{path.name} {variable_id}")


def counting(listing: Callable[[str], str]) -> tuple[Callable[[str], str], list[str]]:
    """A listing that records the URLs it was asked for."""
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        return listing(url)

    return fetch, asked


def registry_entry(variable_id: str, **extra: Any) -> dict:
    """A variable as the committed registry fixtures describe it, the one
    shape the encoders and the frontend must agree on; ``extra`` is what a
    family's fixture adds (a contour interval, an aerosol block)."""
    from xuebuild.quantize import PROFILES  # noqa: PLC0415
    from xuebuild.variables import variable_spec  # noqa: PLC0415

    spec = variable_spec(variable_id)
    return {
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
        **extra,
        "quality": PROFILES["quality"][variable_id].metadata(),
        "compact": PROFILES["compact"][variable_id].metadata(),
    }


class TempRoot:
    """A fresh scratch directory per test, as ``self.root``, removed after.

    Mix in before ``unittest.TestCase``; set ``root_prefix`` to name it.
    """

    root_prefix = "xue-"
    root: Path

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.root = Path(tempfile.mkdtemp(prefix=self.root_prefix))
        self.addCleanup(shutil.rmtree, self.root, True)  # type: ignore[attr-defined]


class ClassTempRoot:
    """One scratch directory per class, as ``cls.root``, made before
    ``setUpClass`` runs and removed after ``tearDownClass``.

    Mix in before ``unittest.TestCase``; set ``root_prefix`` to name it.
    A subclass's ``setUpClass`` calls ``super().setUpClass()`` first.
    """

    root_prefix = "xue-"
    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()  # type: ignore[misc]
        cls.root = Path(tempfile.mkdtemp(prefix=cls.root_prefix))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)  # type: ignore[attr-defined]
