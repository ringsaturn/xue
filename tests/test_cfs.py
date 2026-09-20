"""The CFSv2 source: NCEP's operational coupled climate forecast, nine
months of six-hourly output from every cycle, of which the published axis
takes the thirty-nine weeks (6552 hours, 1092 frames) that every 00Z and
12Z cycle reaches.

What is new with it.

*Series-major input.* Every other fetched forecast publishes one object per
frame and a fetch takes the records it wants out of each; CFSv2 publishes
the transpose — one object per variable holding that variable's whole run,
with an ``.idx`` beside it — so a build reads each variable once as a
handful of very large range requests and cuts the frames out of what it
read (:func:`xuebuild.fetch._fetch_cfs_run`). The frames it writes are what
the frame-by-frame sources write, so nothing downstream knows the
difference. The wind object interleaves a block of U records with the same
block of V records, which is why the ranges are coalesced rather than
assumed contiguous (:func:`xuebuild.idx.coalesce_ranges`).

*An axis that starts at a step.* The series begin at hour 6 and the cycle's
analysis lives in another file family, where the flux fields are
instantaneous analysis values rather than the six-hour means every forecast
frame carries and the 10 m wind pair shares one GRIB message. Rather than
publish one frame of a different quantity the source declares
``first_hour`` 6 and never fetches the analysis, so every bundle of a run
carries the same axis and no variable is analysis-optional.

*A horizon that is written down as a calendar.* A run ends at the first 00Z
of the tenth calendar month after its cycle, which is 6564 to 6888 hours
depending on the date; the published 6552 is the length every cycle
reaches. A time-series object is also written as the model runs, and its
sidecar can name records whose bytes have not landed, so completeness asks
both the sidecar and the object's measured length.

``tests/fixtures/cfs.2026091900.f006.crop.grib2`` and ``f012`` are a 48 x 40
cell East Asian window of the first two frames of the 2026-09-19 00Z cycle,
the nine records the source fetches in source order.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import struct
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, fetch as fetchmod, grib2, native, quantize, zstdcli
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import read_bundle
from xuebuild.cli import MAX_FORECAST_HOURS, forecast_hours as cli_forecast_hours
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    CFS_BASE_URL,
    CFS_SERIES_FILES,
    _fetch_cfs_run,
    _run_is_complete,
    cfs_frame_name,
    cfs_series_url,
    cfs_variable_url,
    model_object_url,
    resolve_run,
)
from xuebuild.gdal import inspect_grib_multi
from xuebuild.idx import ByteRange, coalesce_ranges, series_byte_ranges
from xuebuild.model import GfsRun
from xuebuild.sources import MODEL_CORE_BUNDLES, MODEL_PRODUCTS, source_spec
from xuebuild.stac import prose_document
from xuebuild.variables import variable_spec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FRAMES = [FIXTURES / "cfs.2026091900.f006.crop.grib2", FIXTURES / "cfs.2026091900.f012.crop.grib2"]
CFS = source_spec("cfs")
GFS = source_spec("gfs")
RUN = GfsRun(datetime(2026, 9, 19, 0, tzinfo=UTC))
PUBLISHED = ("tmp2m", "prate", "tcdc", "dswrf", "tmpsfc", "icec", "icetk", "wind10m")

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


def grib_messages(path: Path) -> list[bytes]:
    """One GRIB2 message per element, in file order. GRIB2 messages are
    self-delimiting — the magic, then the total length as a big-endian
    64-bit at octet 8 — which is all the stub series below need."""
    payload = path.read_bytes()
    messages: list[bytes] = []
    offset = 0
    while offset < len(payload):
        assert payload[offset : offset + 4] == b"GRIB", path
        (length,) = struct.unpack_from(">Q", payload, offset + 8)
        messages.append(payload[offset : offset + length])
        offset += length
    return messages


class SourceRegistryTests(unittest.TestCase):
    def test_the_axis_is_six_hourly_from_six_to_thirty_nine_weeks(self) -> None:
        self.assertEqual(CFS.steps, ((6552, 6),))
        self.assertEqual(CFS.first_hour, 6)
        self.assertEqual(CFS.horizon_hours, 6552)
        self.assertEqual(CFS.cycle_hours, 12)
        axis = CFS.forecast_hours(6552)
        self.assertEqual(axis, list(range(6, 6553, 6)))
        self.assertEqual(len(axis), 1092)
        self.assertEqual(CFS.forecast_hours(6), [6])
        for off_axis in (0, 3, 9, 6551):
            with self.subTest(hour=off_axis), self.assertRaises(DownloadError):
                CFS.forecast_hours(off_axis)

    def test_every_other_source_still_starts_at_the_analysis(self) -> None:
        from xuebuild.sources import SOURCES

        starts = {source_id: spec.first_hour for source_id, spec in SOURCES.items()}
        self.assertEqual({source_id for source_id, hour in starts.items() if hour}, {"cfs"})
        self.assertEqual(GFS.forecast_hours(6)[0], 0)

    def test_the_identity_strings(self) -> None:
        self.assertEqual(
            (CFS.manifest_model, CFS.product, CFS.latest_filename),
            ("CFSv2", "time-grib-01", "latest-cfs.json"),
        )
        self.assertEqual(MODEL_PRODUCTS["CFSv2"], "time-grib-01")
        self.assertEqual(MODEL_CORE_BUNDLES["CFSv2"], ("tmp2m", "prate"))
        self.assertEqual(CFS.production_grid, (384, 190))
        self.assertEqual(CFS.tile, (96, 95))
        self.assertEqual(CFS.variant_factors, (2,))
        self.assertFalse(CFS.video)
        self.assertEqual(video_variable_ids(CFS), frozenset())
        self.assertTrue(CFS.fetched and CFS.live)
        self.assertFalse(CFS.observation or CFS.series_file)

    def test_the_grid_divides_into_whole_tiles(self) -> None:
        width, height = CFS.production_grid
        self.assertEqual((width % CFS.tile[0], height % CFS.tile[1]), (0, 0))
        self.assertEqual((width // CFS.tile[0]) * (height // CFS.tile[1]), 8)

    def test_the_surface_set_and_the_wind_pair_are_published(self) -> None:
        self.assertEqual(published_bundle_ids(CFS), PUBLISHED)
        self.assertEqual(CFS.core_bundle_ids, ("tmp2m", "prate"))

    def test_nothing_is_analysis_optional_because_there_is_no_analysis(self) -> None:
        self.assertEqual(CFS.optional_at_analysis, ())
        self.assertEqual(analysis_optional_ids(CFS, published_bundle_ids(CFS)), ())
        self.assertFalse(CFS.accumulated_precipitation)
        self.assertFalse(CFS.averaged_precipitation)
        self.assertFalse(CFS.interval_precipitation)

    def test_the_three_flux_fields_are_six_hour_means(self) -> None:
        self.assertEqual(CFS.statistical_processes, (("prate", 0), ("dswrf", 0), ("tcdc", 0)))

    def test_the_prose_names_noaa(self) -> None:
        prose = prose_document()["sources"]["cfs"]
        self.assertEqual(prose["license"], "other")
        self.assertIn("CFS", prose["title"])
        self.assertTrue(any(provider["name"].startswith("NOAA") for provider in prose["providers"]))


class RegistryAlternateTests(unittest.TestCase):
    def test_the_ncep_spelling_of_the_entire_atmosphere_is_an_alternate(self) -> None:
        tcdc = variable_spec("tcdc")
        # The identity stays what pgrb2 writes: WMO surface type 10.
        self.assertEqual((tcdc.grib2_category, tcdc.grib2_number, tcdc.grib2_level_type), (6, 1, 10))
        self.assertIn(
            (0, 6, 1, 200),
            [(a.discipline, a.category, a.number, a.level_type) for a in tcdc.grib2_alternates],
        )
        self.assertIn(":TCDC:entire atmosphere (considered as a single layer):", tcdc.alternate_index_fields)


class IndexTests(unittest.TestCase):
    INDEX = "\n".join(
        (
            "1:0:d=2026091900:UGRD:10 m above ground:6 hour fcst:",
            "2:100:d=2026091900:UGRD:10 m above ground:12 hour fcst:",
            "3:210:d=2026091900:VGRD:10 m above ground:6 hour fcst:",
            "4:320:d=2026091900:VGRD:10 m above ground:12 hour fcst:",
            "5:430:d=2026091900:UGRD:10 m above ground:18 hour fcst:",
            "6:540:d=2026091900:VGRD:10 m above ground:18 hour fcst:",
        )
    )

    def test_a_series_is_read_by_forecast_hour(self) -> None:
        u = series_byte_ranges(self.INDEX, ":UGRD:10 m above ground:")
        self.assertEqual(sorted(u), [6, 12, 18])
        self.assertEqual(u[6], ByteRange(0, 99))
        self.assertEqual(u[12], ByteRange(100, 209))

    def test_the_last_record_needs_the_object_s_length(self) -> None:
        v = series_byte_ranges(self.INDEX, ":VGRD:10 m above ground:")
        self.assertNotIn(18, v, "the sidecar cannot say where the last record ends")
        measured = series_byte_ranges(self.INDEX, ":VGRD:10 m above ground:", file_size=700)
        self.assertEqual(measured[18], ByteRange(540, 699))

    def test_an_alternate_phrase_is_tried_when_the_first_names_nothing(self) -> None:
        text = "1:0:d=2026091900:TCDC:entire atmosphere (considered as a single layer):6 hour fcst:\n2:50:x:y:12 hour fcst:"
        spec = variable_spec("tcdc")
        self.assertEqual(series_byte_ranges(text, spec.index_field), {})
        found = series_byte_ranges(text, spec.index_field, alternate_fields=spec.alternate_index_fields)
        self.assertEqual(found, {6: ByteRange(0, 49)})

    def test_neighbouring_records_become_one_request(self) -> None:
        u = series_byte_ranges(self.INDEX, ":UGRD:10 m above ground:")
        v = series_byte_ranges(self.INDEX, ":VGRD:10 m above ground:")
        pair = coalesce_ranges(sorted([u[6], u[12], v[6], v[12]], key=lambda r: r.start))
        self.assertEqual(pair, [ByteRange(0, 429)])
        split = coalesce_ranges(sorted([u[6], v[6]], key=lambda r: r.start))
        self.assertEqual(split, [ByteRange(0, 99), ByteRange(210, 319)])
        with self.assertRaises(DownloadError):
            coalesce_ranges([ByteRange(0, 99), ByteRange(50, 149)])


class UrlTests(unittest.TestCase):
    def test_a_run_is_one_object_per_variable(self) -> None:
        self.assertEqual(
            cfs_series_url(RUN, "tmp2m"),
            f"{CFS_BASE_URL}/cfs.20260919/00/time_grib_01/tmp2m.01.2026091900.daily.grb2",
        )
        self.assertEqual(cfs_variable_url(RUN, "tcdc"), cfs_series_url(RUN, "tcdcclm"))
        self.assertEqual(cfs_variable_url(RUN, "ugrd10m"), cfs_variable_url(RUN, "vgrd10m"))
        with self.assertRaises(DownloadError):
            cfs_variable_url(RUN, "prmsl")

    def test_every_input_names_an_object(self) -> None:
        self.assertEqual(set(CFS.input_variable_ids), set(CFS_SERIES_FILES))
        self.assertEqual(CFS_SERIES_FILES["ugrd10m"], "wnd10m")

    def test_there_is_no_object_for_one_hour(self) -> None:
        # The hour has no meaning here; the reference object is named so a
        # generic probe asks for something that exists.
        self.assertEqual(model_object_url(RUN, 0, "cfs"), cfs_variable_url(RUN, "tmp2m"))
        self.assertEqual(model_object_url(RUN, 6552, "cfs"), cfs_variable_url(RUN, "tmp2m"))

    def test_the_frame_name_grows_past_a_thousand_hours(self) -> None:
        self.assertEqual(cfs_frame_name(CFS, RUN, 6), "cfs.2026091900.f006.grib2")
        self.assertEqual(cfs_frame_name(CFS, RUN, 6552), "cfs.2026091900.f6552.grib2")


class BucketStub:
    """The two frames of the fixture served the way the bucket serves a run:
    one object per variable, the wind pair's U block before its V block, with
    a sidecar naming each record's forecast hour."""

    def __init__(self, *, hours: tuple[int, ...] = (6, 12), written_records: int | None = None) -> None:
        records = {
            hour: dict(zip(CFS.input_variable_ids, grib_messages(path)))
            for hour, path in zip((6, 12), FIXTURE_FRAMES)
        }
        # Hours past the fixture's two exist only to give the sidecar a
        # successor record; nothing reads their bytes.
        for hour in hours:
            records.setdefault(hour, records[12])
        self.objects: dict[str, bytes] = {}
        self.indexes: dict[str, str] = {}
        for name in dict.fromkeys(CFS_SERIES_FILES.values()):
            fields = [vid for vid in CFS.input_variable_ids if CFS_SERIES_FILES[vid] == name]
            payload = b""
            lines = []
            boundaries = [0]
            number = 0
            for variable_id in fields:
                for hour in hours:
                    number += 1
                    lines.append(
                        f"{number}:{len(payload)}:d={RUN.id}:"
                        f"{variable_spec(variable_id).index_field.strip(':')}:{hour} hour fcst:"
                    )
                    payload += records[hour][variable_id]
                    boundaries.append(len(payload))
            url = cfs_series_url(RUN, name)
            # ``written_records`` is how much of the object has landed: the
            # sidecar always describes the whole run, as the bucket's does
            # while the model is still writing.
            self.objects[url] = payload if written_records is None else payload[: boundaries[written_records]]
            self.indexes[url + ".idx"] = "\n".join(lines) + "\n"
        self.requests: list[tuple[str, ByteRange]] = []

    def fetch_text(self, url: str) -> str:
        try:
            return self.indexes[url]
        except KeyError:
            raise DownloadError(f"no such object: {url}") from None

    def fetch_range(self, url: str, byte_range: ByteRange) -> bytes:
        self.requests.append((url, byte_range))
        body = self.objects[url][byte_range.start : byte_range.end + 1]
        if len(body) != byte_range.length:
            raise DownloadError(f"short read of {url}")
        return body

    def remote_length(self, url: str) -> int | None:
        return len(self.objects[url]) if url in self.objects else None

    def install(self, case: unittest.TestCase) -> None:
        for name in ("fetch_text", "fetch_range", "remote_length"):
            patcher = mock.patch.object(fetchmod, name, getattr(self, name))
            patcher.start()
            case.addCleanup(patcher.stop)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-cfs-fetch-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_run_is_split_into_frames_in_source_order(self) -> None:
        stub = BucketStub()
        stub.install(self)
        paths = _fetch_cfs_run(CFS, RUN, 12, self.root)
        self.assertEqual([path.name for path in paths], ["cfs.2026091900.f006.grib2", "cfs.2026091900.f012.grib2"])
        for path, fixture in zip(paths, FIXTURE_FRAMES):
            self.assertEqual(path.read_bytes(), fixture.read_bytes(), path.name)
        # The temporary series are gone and nothing a build would read is left.
        run_directory = self.root / "cfs.2026091900"
        self.assertEqual(sorted(entry.name for entry in run_directory.iterdir()), [path.name for path in paths])

    def test_the_wind_pair_is_located_in_its_own_block(self) -> None:
        stub = BucketStub()
        stub.install(self)
        paths = _fetch_cfs_run(CFS, RUN, 12, self.root)
        frames = grib2.inspect_grib_fast(paths[1], CFS.input_variable_ids)
        self.assertEqual(frames["ugrd10m"].band, 3)
        self.assertEqual(frames["vgrd10m"].band, 4)
        self.assertEqual({frame.lead_seconds for frame in frames.values()}, {12 * 3600})

    def test_neighbouring_records_cost_one_request_per_object(self) -> None:
        stub = BucketStub()
        stub.install(self)
        _fetch_cfs_run(CFS, RUN, 12, self.root)
        per_object: dict[str, int] = {}
        for url, _ in stub.requests:
            per_object[url] = per_object.get(url, 0) + 1
        # Nine inputs over eight objects, one contiguous request each: the
        # wind object is read once for its U block and once for its V block.
        self.assertEqual(sorted(per_object.values()), [1] * 7 + [2])
        self.assertEqual(per_object[cfs_variable_url(RUN, "ugrd10m")], 2)
        self.assertEqual(sum(per_object.values()), 9)

    def test_a_narrowed_build_touches_only_its_own_objects(self) -> None:
        stub = BucketStub()
        stub.install(self)
        _fetch_cfs_run(CFS, RUN, 12, self.root, input_ids=("tmp2m",))
        self.assertEqual({url for url, _ in stub.requests}, {cfs_variable_url(RUN, "tmp2m")})
        frames = grib2.inspect_grib_fast(self.root / "cfs.2026091900/cfs.2026091900.f006.grib2", ("tmp2m",))
        self.assertEqual(frames["tmp2m"].band, 1)

    def test_readable_frames_are_reused_and_missing_ones_refetched(self) -> None:
        stub = BucketStub()
        stub.install(self)
        _fetch_cfs_run(CFS, RUN, 12, self.root)
        stub.requests.clear()
        _fetch_cfs_run(CFS, RUN, 12, self.root)
        self.assertEqual(stub.requests, [], "every frame was already there")
        (self.root / "cfs.2026091900/cfs.2026091900.f012.grib2").unlink()
        _fetch_cfs_run(CFS, RUN, 12, self.root)
        self.assertTrue(stub.requests)
        self.assertTrue(all(byte_range.length < len(stub.objects[url]) for url, byte_range in stub.requests))

    def test_a_forced_fetch_reads_everything_again(self) -> None:
        stub = BucketStub()
        stub.install(self)
        _fetch_cfs_run(CFS, RUN, 12, self.root)
        stub.requests.clear()
        _fetch_cfs_run(CFS, RUN, 12, self.root, force=True)
        self.assertEqual(len({url for url, _ in stub.requests}), 8)

    def test_an_hour_the_series_lacks_is_an_error(self) -> None:
        stub = BucketStub(hours=(6,))
        stub.install(self)
        with self.assertRaises(DownloadError) as caught:
            _fetch_cfs_run(CFS, RUN, 12, self.root)
        self.assertIn("forecast hour 12", str(caught.exception))


class CompletenessTests(unittest.TestCase):
    def test_a_run_whose_objects_carry_the_last_hour_is_complete(self) -> None:
        stub = BucketStub(hours=(6, 12, 18))
        stub.install(self)
        self.assertTrue(_run_is_complete(RUN, 12, "cfs", fetchmod.remote_exists))
        self.assertFalse(_run_is_complete(RUN, 18, "cfs", fetchmod.remote_exists), "the last record has no successor")

    def test_a_sidecar_ahead_of_its_data_is_not_complete(self) -> None:
        stub = BucketStub(hours=(6, 12, 18), written_records=1)
        stub.install(self)
        self.assertFalse(_run_is_complete(RUN, 12, "cfs", fetchmod.remote_exists))

    def test_an_absent_object_is_not_complete(self) -> None:
        stub = BucketStub()
        stub.objects.pop(cfs_variable_url(RUN, "icetk"))
        stub.indexes.pop(cfs_variable_url(RUN, "icetk") + ".idx")
        stub.install(self)
        with mock.patch.object(fetchmod, "_http_error_code", lambda exc: 404):
            self.assertFalse(_run_is_complete(RUN, 6, "cfs", fetchmod.remote_exists))

    def test_latest_walks_back_over_the_twelve_hour_cycle_grid(self) -> None:
        seen: list[str] = []

        def complete(spec, run, hours, **kwargs):
            seen.append(run.id)
            return run.id == "2026091900"

        with mock.patch.object(fetchmod, "_cfs_run_is_complete", complete):
            run = resolve_run("latest", hours=6552, now=datetime(2026, 9, 20, 5, 30, tzinfo=UTC), model="cfs")
        self.assertEqual(run.id, "2026091900")
        self.assertEqual(seen, ["2026092000", "2026091912", "2026091900"])
        self.assertTrue(all(candidate[8:] in {"00", "12"} for candidate in seen))


class CliTests(unittest.TestCase):
    def test_the_hour_cap_reaches_the_longest_published_axis(self) -> None:
        self.assertEqual(MAX_FORECAST_HOURS, 6552)
        self.assertEqual(cli_forecast_hours("6552"), 6552)
        import argparse

        with self.assertRaises(argparse.ArgumentTypeError):
            cli_forecast_hours("6553")


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path in FIXTURE_FRAMES:
            frames = grib2.inspect_grib_fast(path, CFS.input_variable_ids)
            self.assertEqual(len(frames), 9, path.name)
            self.assertEqual([frames[vid].band for vid in CFS.input_variable_ids], list(range(1, 10)))
        step = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], CFS.input_variable_ids)
        self.assertEqual(step["tmp2m"].lead_seconds, 12 * 3600)
        self.assertEqual(step["prate"].unit, "kg/(m^2 s)")
        self.assertEqual(step["tcdc"].unit, "%")
        self.assertEqual(step["icetk"].unit, "m")

    def test_every_record_is_the_instantaneous_template(self) -> None:
        messages = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[0])}
        self.assertEqual({message.statistical_process for message in messages.values()}, {None})
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[0], CFS.input_variable_ids)
        tcdc = messages[frames["tcdc"].band]
        self.assertEqual((tcdc.parameter_category, tcdc.parameter_number, tcdc.level_type), (6, 1, 200))

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            for path in FIXTURE_FRAMES:
                fast = grib2.inspect_grib_fast(path, CFS.input_variable_ids)
                slow = inspect_grib_multi(path, CFS.input_variable_ids)
                self.assertEqual(set(fast), set(slow), path.name)
                for variable_id, frame in fast.items():
                    self.assertEqual(frame, slow[variable_id], f"{path.name} {variable_id}")


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-cfs-"))
        cls.output = cls.root / "cfs.2026091900"
        cls.report = binconvert.convert_bin(
            FIXTURE_FRAMES,
            cls.output,
            work_root=cls.root / "work",
            manifest_path=cls.output / "manifest.json",
            model="cfs",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _bundle(self, bundle_id: str):
        return read_bundle(self.output / f"{bundle_id}.xue")

    def test_every_published_bundle_is_written_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CFSv2", "time-grib-01"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(PUBLISHED))
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])

    def test_the_axis_starts_at_the_first_step_for_every_bundle(self) -> None:
        for bundle_id in PUBLISHED:
            with self.subTest(bundle=bundle_id):
                self.assertEqual(
                    self._bundle(bundle_id).metadata["time"],
                    {"unitSeconds": 3600, "firstFrameOffset": 6, "frameCount": 2, "frameStep": 6},
                )

    def test_the_flux_fields_declare_their_six_hour_mean(self) -> None:
        for bundle_id in ("prate", "dswrf", "tcdc"):
            with self.subTest(bundle=bundle_id):
                parameter = self._bundle(bundle_id).metadata["variables"][0]["parameter"]
                self.assertEqual(parameter["typeOfStatisticalProcessing"], 0)
        for bundle_id in ("tmp2m", "tmpsfc", "icec", "icetk"):
            with self.subTest(bundle=bundle_id):
                parameter = self._bundle(bundle_id).metadata["variables"][0]["parameter"]
                self.assertNotIn("typeOfStatisticalProcessing", parameter)

    def test_the_total_cloud_cover_is_written_under_the_pgrb2_identity(self) -> None:
        parameter = self._bundle("tcdc").metadata["variables"][0]["parameter"]
        self.assertEqual(
            (parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]),
            (6, 1, 10),
        )
        codes = self._bundle("tcdc").decode_plane(1, 6)
        self.assertEqual(int(codes.max()), 200, "an overcast cell is 100 %")

    def test_the_gaussian_grid_reads_as_the_window_it_was_cut_from(self) -> None:
        grid = self._bundle("tmp2m").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (48, 40))
        self.assertFalse(grid["wrapLongitude"])
        # The T126 step passes the regional snap through unchanged: it is
        # nowhere near a whole thousandth of a degree.
        self.assertAlmostEqual(grid["longitudeStep"], 0.9375, places=5)
        self.assertNotEqual(grid["longitudeStep"], 0.938)
        self.assertAlmostEqual(grid["latitudeStep"], -0.947368, places=5)

    def test_the_ice_thickness_nodata_becomes_the_codebook_bottom(self) -> None:
        codes = self._bundle("icetk").decode_plane(1, 6)
        self.assertEqual(int(codes.max()), 0, "no ice in an East Asian September window")
        self.assertNotIn(255, codes)
        self.assertEqual(variable_spec("icetk").fill_values, (9999.0,))

    def test_the_rate_is_a_six_hour_mean_in_the_ordinary_codebook(self) -> None:
        codes = self._bundle("prate").decode_plane(1, 12)
        codebook = quantize.PROFILES["quality"]["prate"]
        rates = codebook.decode(codes)
        self.assertGreater(float(rates.max()), 0.5)
        self.assertLess(float(rates.max()), 20.0)

    def test_the_wind_pair_is_one_bundle_of_two_variables(self) -> None:
        metadata = self._bundle("wind10m").metadata
        self.assertEqual([variable["id"] for variable in metadata["variables"]], ["ugrd10m", "vgrd10m"])
        u = self._bundle("wind10m").decode_plane(1, 6)
        v = self._bundle("wind10m").decode_plane(2, 6)
        self.assertEqual(u.shape, v.shape)
        self.assertGreater(int(u.max()), 127)
        self.assertLess(int(v.min()), 127)

    def test_an_axis_off_the_published_cadence_is_refused(self) -> None:
        with self.assertRaises(ConversionError):
            binconvert.convert_bin(
                FIXTURE_FRAMES[:1],
                self.root / "one",
                work_root=self.root / "one-work",
                model="cfs",
                require_complete=True,
                expected_hours=12,
            )


@unittest.skipUnless(native.available(), f"{native.DISTRIBUTION} is not installed")
class NativeParityTests(unittest.TestCase):
    """The same two frames through both encoders, byte for byte — the
    Gaussian grid, the NCEP entire-atmosphere surface, the nodata fill and
    an axis that starts at a step, on real CFSv2 records."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not native.knows_source("cfs"):
            raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the CFSv2 source")
        cls.root = Path(tempfile.mkdtemp(prefix="xue-cfs-parity-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        try:
            cls.subject, cls.subject_report = cls._build(native, "subject")
        except ConversionError as exc:
            if "out of step" in str(exc):
                shutil.rmtree(cls.root, ignore_errors=True)
                raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the CFSv2 source: {exc}")
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        run_directory = cls.root / name / "cfs.2026091900"
        report = implementation.convert_bin(
            FIXTURE_FRAMES,
            run_directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=run_directory / "manifest.json",
            latest_path=cls.root / name / "latest-cfs.json",
            run_id="2026091900",
            model="cfs",
        )
        return cls.root / name, report

    def test_every_artifact_is_byte_identical(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI below Python 3.14")
        if self.reference_report["zstdVersion"] != self.subject_report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        reference = sorted(path.relative_to(self.reference).as_posix() for path in self.reference.rglob("*"))
        subject = sorted(path.relative_to(self.subject).as_posix() for path in self.subject.rglob("*"))
        self.assertEqual(reference, subject)
        self.assertTrue(any(name.endswith("wind10m.xue") for name in reference))
        self.assertTrue(any(name.endswith("icetk.xue") for name in reference))
        for relative in reference:
            path = self.reference / relative
            if path.is_dir():
                continue
            with self.subTest(artifact=relative):
                self.assertTrue(filecmp.cmp(path, self.subject / relative, shallow=False), f"{relative} differs")


if __name__ == "__main__":
    unittest.main()
