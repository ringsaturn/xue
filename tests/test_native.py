"""The native encoder as a drop-in for the reference pipeline.

`xuebuild.native` is only worth having if a build through it is
indistinguishable from a build through `xuebuild.binconvert`, so that is what
these tests check: same bundles, same posters, same variants, same H.264
companions, same manifest, same live pointer — byte for byte, from the same
GRIB fixture, in one run each.

The parity cases skip when the `xuepy` wheel is not installed, and when the
reference encoder is compressing through the zstd CLI — below Python 3.14 it
streams through a pipe, which is a different frame from the one-shot compress
the native encoder does, so identical bytes are not on offer there. Everything
that does not depend on the compressor is checked regardless, including which
encoder gets picked, because that logic is what decides whether a scheduled
build silently falls back.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xuebuild import binconvert, binformat, encoder, native, zstdcli
from xuebuild.errors import ConversionError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"
# The three GFS-Wave records of the 2026-09-11 00Z analysis, the same window,
# packed as WAVEWATCH III publishes them: JPEG 2000 (DRS template 5.40) with
# a bitmap over land. gdal_translate re-encodes what it crops, so the crop
# fixture above never carried that packing.
FIXTURE_JP2_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfswave.2026091100.f000.jp2.crop.grib2"

requires_native = unittest.skipUnless(
    native.available(), f"{native.DISTRIBUTION} is not installed"
)


def require_comparable_compression(
    case: unittest.TestCase, reference: dict, subject: dict
) -> None:
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


def _selection(value: str):
    """Run with `XUE_ENCODER` set to one value."""
    return mock.patch.dict(os.environ, {encoder.SELECTION_VARIABLE: value})


class EncoderSelectionTests(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(encoder.SELECTION_VARIABLE, None)
            self.assertEqual(encoder.selection(), "auto")

    def test_selection_is_case_insensitive_and_trimmed(self) -> None:
        with _selection("  Native \n"):
            self.assertEqual(encoder.selection(), "native")

    def test_unknown_selection_is_user_actionable(self) -> None:
        with _selection("rust"):
            with self.assertRaises(ConversionError) as raised:
                encoder.selection()
        self.assertIn(encoder.SELECTION_VARIABLE, str(raised.exception))

    def test_python_is_honoured_even_when_the_wheel_is_installed(self) -> None:
        with _selection("python"):
            self.assertEqual(encoder.resolve(), "python")

    def test_auto_falls_back_when_the_wheel_is_missing(self) -> None:
        with _selection("auto"), mock.patch.object(native, "available", return_value=False):
            self.assertEqual(encoder.resolve(), "python")

    def test_native_refuses_to_fall_back(self) -> None:
        failure = ConversionError("the native encoder needs the xuepy wheel")
        with _selection("native"), mock.patch.object(native, "require", side_effect=failure):
            with self.assertRaises(ConversionError):
                encoder.resolve()

    def test_dispatch_follows_the_selection(self) -> None:
        for requested, module in (("python", binconvert), ("native", native)):
            with self.subTest(requested=requested):
                with _selection(requested), mock.patch.object(
                    module, "convert_bin", return_value={"ok": requested}
                ) as delegate, mock.patch.object(native, "require", return_value=object()):
                    report = encoder.convert_bin(FIXTURE_GRIB, Path("out"), model="gfs")
        self.assertEqual(report, {"ok": "native"})
        delegate.assert_called_once()


@requires_native
class NativeParityTests(unittest.TestCase):
    """One build each, compared file by file.

    Both encoders write into identically shaped directories under one root, so
    even the live pointer — which names the manifest relative to itself and
    carries its CRC32 — has to come out the same.
    """

    root: Path
    reference: Path
    subject: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-native-parity-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        cls.subject, cls.subject_report = cls._build(native, "subject")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        run_directory = cls.root / name / "gfs.2026081406"
        report = implementation.convert_bin(
            FIXTURE_GRIB,
            run_directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=run_directory / "manifest.json",
            latest_path=cls.root / name / "latest.json",
            run_id="2026081406",
            model="gfs",
        )
        return cls.root / name, report

    def test_the_same_files_are_written(self) -> None:
        self.assertEqual(
            sorted(path.relative_to(self.reference).as_posix() for path in self.reference.rglob("*")),
            sorted(path.relative_to(self.subject).as_posix() for path in self.subject.rglob("*")),
        )

    def test_every_artifact_is_byte_identical(self) -> None:
        require_comparable_compression(self, self.reference_report, self.subject_report)
        for path in sorted(self.reference.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(self.reference)
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(
                    filecmp.cmp(path, self.subject / relative, shallow=False),
                    f"{relative} differs between the two encoders",
                )

    def test_the_video_companions_were_actually_built(self) -> None:
        """Parity is worth nothing if both sides skipped the same work."""
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not installed, so neither encoder built a video")
        run_directory = self.subject / "gfs.2026081406"
        for variable_id in ("tmp2m", "prate"):
            self.assertTrue((run_directory / f"{variable_id}.h264").is_file())
            self.assertTrue((run_directory / f"{variable_id}.h264.index.json").is_file())
            self.assertTrue((run_directory / f"{variable_id}.h264.m3u8").is_file())


@requires_native
class NativeReportTests(unittest.TestCase):
    """The report is the CLI's output and the publish workflow's summary
    input, so it has to carry the same keys the reference does."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-native-report-"))
        cls.reference = binconvert.convert_bin(
            FIXTURE_GRIB, cls.root / "reference", work_root=cls.root / "work", skip_video=True
        )
        cls.subject = native.convert_bin(FIXTURE_GRIB, cls.root / "subject", skip_video=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_reports_have_the_same_shape(self) -> None:
        self.assertEqual(sorted(self.reference), sorted(self.subject))

    def test_skip_video_leaves_no_companions(self) -> None:
        self.assertEqual(self.subject["videos"], [])
        self.assertEqual(list((self.root / "subject").glob("*.h264")), [])

    def test_the_measured_quantization_error_agrees(self) -> None:
        """Quantization happens before compression, so this holds everywhere."""
        for key in ("temperatureClampedPoints", "precipitationOverflowPoints"):
            with self.subTest(key=key):
                self.assertEqual(self.reference[key], self.subject[key])
        self.assertAlmostEqual(
            self.reference["temperatureMaxAbsError"], self.subject["temperatureMaxAbsError"], places=6
        )

    def test_the_bundles_come_out_the_same_size(self) -> None:
        require_comparable_compression(self, self.reference, self.subject)
        self.assertEqual(self.reference["byteLength"], self.subject["byteLength"])


@requires_native
class NativeRestrictedBuildTests(unittest.TestCase):
    """A showcase case: one variable, cropped, and no core-pair requirement."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-native-case-"))
        cls.bbox = (119.0, 30.0, 125.0, 36.0)
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        cls.subject, cls.subject_report = cls._build(native, "subject")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        directory = cls.root / name
        report = implementation.convert_bin(
            FIXTURE_GRIB,
            directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=directory / "manifest.json",
            model="gfs",
            bbox=cls.bbox,
            bundle_ids=("tmp2m",),
            skip_video=True,
            skip_variants=True,
        )
        return directory, report

    def test_only_the_requested_bundle_is_published(self) -> None:
        manifest = json.loads((self.subject / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], ["tmp2m"])

    def test_the_cropped_build_is_byte_identical(self) -> None:
        require_comparable_compression(self, self.reference_report, self.subject_report)
        for path in sorted(self.reference.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(self.reference)
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(
                    filecmp.cmp(path, self.subject / relative, shallow=False),
                    f"{relative} differs between the two encoders",
                )


@requires_native
class NativeJpeg2000Tests(unittest.TestCase):
    """The GDAL the wheel carries decodes JPEG 2000-packed records — the
    GFS-Wave family as published, which the first wheel refused ("Is the
    JPEG2000 driver available?") and the fetcher repacked for — and reads
    them, bitmap included, to the same values the reference GDAL does: the
    wave bundles and the wave vector derived from two of them come out
    byte-identical from the raw records."""

    WAVE_BUNDLES = ("htsgw", "perpw", "wave")

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-native-jp2-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        cls.subject, cls.subject_report = cls._build(native, "subject")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        directory = cls.root / name
        report = implementation.convert_bin(
            FIXTURE_JP2_GRIB,
            directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=directory / "manifest.json",
            model="gfs",
            bundle_ids=cls.WAVE_BUNDLES,
            skip_video=True,
            skip_variants=True,
        )
        return directory, report

    def test_the_wave_bundles_are_built_from_the_raw_records(self) -> None:
        manifest = json.loads((self.subject / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(self.WAVE_BUNDLES))
        wave = binformat.Bundle((self.subject / "wave.xue").read_bytes())
        self.assertEqual([variable["id"] for variable in wave.metadata["variables"]], ["uwave", "vwave"])

    def test_the_jpeg_2000_build_is_byte_identical(self) -> None:
        require_comparable_compression(self, self.reference_report, self.subject_report)
        for path in sorted(self.reference.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(self.reference)
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(
                    filecmp.cmp(path, self.subject / relative, shallow=False),
                    f"{relative} differs between the two encoders",
                )


if __name__ == "__main__":
    unittest.main()
