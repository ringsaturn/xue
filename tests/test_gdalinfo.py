"""The wheel's `gdal_info` as a drop-in for the `gdalinfo` CLI.

`xuebuild.gdal.dataset_info` reads its inputs through whichever GDAL the build
is already using: the one linked into the `xuepy` wheel when converting
natively, the `gdalinfo` subprocess otherwise. That is only safe if the two
report the same thing, because the inspection pass is what decides a
downloaded GRIB is readable, which variable sits in which band, and what grid
a run is on — a discrepancy would not fail, it would publish something subtly
wrong.

So these tests diff the two sources field by field over a real GRIB fixture,
and check that the selection follows `XUE_ENCODER` so a run never mixes them.

The comparison cases skip unless both sources are actually present — which
means a wheel new enough to carry `gdal_info`, not merely one installed: the
published wheel can lag this working tree, and CI sits in exactly that state
between a version bump and its release. The selection cases run either way,
because that logic is what decides whether a build quietly needs a system
GDAL it was not given.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from xuebuild import gdal, native
from xuebuild.errors import ConversionError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"
# A GRIB on a map projection: the converter recognises one from the
# coordinate system the inspection reports.
FIXTURE_PROJECTED_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "hrrr.2026091100.f000.crop.grib2"

# The keys xuebuild reads off a band. gdalinfo reports plenty more; nothing
# here promises those, so nothing here compares them.
BAND_KEYS = ("band", "description", "unit", "scale", "offset", "noDataValue")


def wheel_can_inspect() -> bool:
    """Whether the resolved wheel is new enough to inspect.

    Not the same question as `native.available()`. A wheel predating
    `gdal_info` still converts, so it is "available", but inspection falls
    back to the CLI — which is the state CI sits in whenever the pin admits a
    published wheel older than the working tree.
    """
    return native.available() and hasattr(native.require(), "gdal_info")


requires_native_info = unittest.skipUnless(
    wheel_can_inspect(), "the installed xuepy wheel has no gdal_info"
)
requires_cli = unittest.skipUnless(
    shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH"
)


def cli_info(path: Path) -> dict:
    result = subprocess.run(
        ["gdalinfo", "-json", str(path)], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


@requires_native_info
@requires_cli
class NativeMatchesTheCommandLine(unittest.TestCase):
    """Both GDALs, one fixture, the same answers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = cli_info(FIXTURE_GRIB)
        cls.native = native.require().gdal_info(str(FIXTURE_GRIB))

    def test_grid_geometry_matches(self) -> None:
        # _grid_info derives the whole published grid from these two.
        self.assertEqual(self.native["size"], self.cli["size"])
        self.assertEqual(self.native["geoTransform"], self.cli["geoTransform"])

    def test_dataset_metadata_matches(self) -> None:
        # The observation ingest reads time#units out of this domain.
        self.assertEqual(
            self.native.get("metadata", {}).get("", {}),
            self.cli.get("metadata", {}).get("", {}),
        )

    def test_every_band_matches_field_for_field(self) -> None:
        self.assertEqual(len(self.native["bands"]), len(self.cli["bands"]))
        self.assertGreater(len(self.native["bands"]), 0)
        for native_band, cli_band in zip(self.native["bands"], self.cli["bands"]):
            for key in BAND_KEYS:
                with self.subTest(band=cli_band["band"], key=key):
                    # A key gdalinfo omits must be omitted here too, not
                    # emitted empty: the readers default them, so an empty
                    # string and a missing key are equivalent but not
                    # identical, and only identical stays true as the readers
                    # change.
                    self.assertEqual(native_band.get(key), cli_band.get(key))

    def test_band_metadata_matches(self) -> None:
        # This domain carries GRIB_ELEMENT, GRIB_SHORT_NAME, GRIB_REF_TIME and
        # GRIB_VALID_TIME — record matching and the whole time axis.
        for native_band, cli_band in zip(self.native["bands"], self.cli["bands"]):
            with self.subTest(band=cli_band["band"]):
                self.assertEqual(
                    native_band.get("metadata", {}).get("", {}),
                    cli_band.get("metadata", {}).get("", {}),
                )


def wheel_reports_coordinate_systems() -> bool:
    """Whether the resolved wheel reports `coordinateSystem` — newer again
    than `gdal_info` itself, and what a projected source (HRRR) needs."""
    return wheel_can_inspect() and "coordinateSystem" in native.require().gdal_info(str(FIXTURE_GRIB))


@unittest.skipUnless(wheel_reports_coordinate_systems(), "the installed xuepy wheel reports no coordinate system")
@requires_cli
class CoordinateSystemMatchesTheCommandLine(unittest.TestCase):
    """The WKT is what tells a projected grid from a regular one, so the two
    sources must print the same text — a geographic system for the global
    grids, the Lambert conformal conic for HRRR."""

    def test_the_wkt_is_the_same_text(self) -> None:
        for fixture in (FIXTURE_GRIB, FIXTURE_PROJECTED_GRIB):
            with self.subTest(fixture=fixture.name):
                native_wkt = native.require().gdal_info(str(fixture))["coordinateSystem"]["wkt"]
                self.assertEqual(native_wkt, cli_info(fixture)["coordinateSystem"]["wkt"])

    def test_the_grid_is_read_the_same_way(self) -> None:
        from xuebuild.binconvert import _grid_info
        from xuebuild.sources import source_spec

        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            through_wheel = _grid_info(FIXTURE_PROJECTED_GRIB, source_spec("hrrr"))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            through_cli = _grid_info(FIXTURE_PROJECTED_GRIB, source_spec("hrrr"))
        self.assertEqual(through_wheel.metadata(), through_cli.metadata())
        self.assertEqual(through_wheel.resample.source, through_cli.resample.source)


@requires_native_info
@requires_cli
class InspectionAgreesThroughEitherSource(unittest.TestCase):
    """The frames xuebuild derives, not just the JSON underneath them."""

    def test_inspect_grib_multi_agrees(self) -> None:
        variable_ids = ("tmp2m", "prate")
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            through_wheel = gdal.inspect_grib_multi(
                FIXTURE_GRIB, variable_ids, optional_ids=variable_ids
            )
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            through_cli = gdal.inspect_grib_multi(
                FIXTURE_GRIB, variable_ids, optional_ids=variable_ids
            )
        self.assertEqual(through_wheel.keys(), through_cli.keys())
        self.assertTrue(through_wheel, "the fixture must carry at least one variable")
        for variable_id, frame in through_wheel.items():
            self.assertEqual(frame, through_cli[variable_id])


class TheSourceFollowsTheEncoderSelection(unittest.TestCase):
    """One GDAL per build, chosen by XUE_ENCODER.

    A run must not inspect through one GDAL and extract through another: the
    reference pipeline needs a system `gdal_translate` regardless, so it reads
    its metadata from the same install.
    """

    def test_python_always_shells_out(self) -> None:
        # True whatever is installed: the reference pipeline needs a system
        # GDAL for gdal_translate, so it reads its metadata from the same one.
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            self.assertIsNone(gdal._native_gdal_info())

    @requires_native_info
    def test_native_uses_the_wheel(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            self.assertIsNotNone(gdal._native_gdal_info())

    def test_auto_follows_what_the_wheel_can_do(self) -> None:
        # Whether the wheel can *inspect*, not merely whether it is
        # installed: a wheel older than gdal_info converts natively and still
        # inspects through the CLI.
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "auto"}):
            selected = gdal._native_gdal_info()
        self.assertEqual(selected is not None, wheel_can_inspect())

    @unittest.skipUnless(
        native.available() and not wheel_can_inspect(),
        "needs an installed wheel that predates gdal_info",
    )
    def test_an_older_wheel_falls_back_rather_than_failing(self) -> None:
        # The published wheel can lag the working tree, and the build must
        # still run — through the CLI, with publish.yml installing gdal-bin
        # because it asks the same question this does.
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            self.assertIsNone(gdal._native_gdal_info())

    def test_native_without_the_wheel_fails_at_the_first_inspection(self) -> None:
        # Rather than after fetching a whole run: the fetch path inspects
        # every file it downloads, so this is the first thing that asks.
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            with mock.patch.object(
                native, "require", side_effect=ConversionError("no wheel")
            ):
                with self.assertRaises(ConversionError):
                    gdal._native_gdal_info()


if __name__ == "__main__":
    unittest.main()
