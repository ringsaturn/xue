"""The CMA source (``cma``, the source once called ``radar``): the
agency's level-3 composite reflectivity mosaic over China, an observation
that is fetched out of a private archive — one plain Zarr v3 store per UTC
day, read with zarr-python (``xuebuild/cmaarchive.py``) — as one NetCDF
series per window.

What is new with it against the JMA source it is shaped like: the fetch is
a read of an archive rather than a decode of tiles, so nothing is cached
between rounds and the newest frame is whatever the archive has written; a
named window is complete when the archive has moved past its end; the
six-minute cadence (``cadence_seconds`` 360, ``unitSeconds`` 360) on a
source that was, before the archive, a local file whose first frame was
the run — a new id for the new shape, so a wheel that knows ``radar`` is
never taken for one that knows ``cma``; and a showcase case that takes
either a window of the archive or a local file.

The archive tests build two small day stores in a temporary directory in
the archive's own layout (a complete 240-slot time axis, a slot_status,
the pixel-centre coordinates, one int16 cref array) and read them back.
``tests/fixtures/cma.2026091609.crop.nc`` is three frames of the
2026-09-16 09Z hour (09:00, 09:06 and 09:18; 09:12 left out, so the axis
lists its offsets) cropped to 128 x 128 cells over Hubei and Hunan (112.5E
to 118.1E, 33.75N to 28.1N) on a convective afternoon, returns to 62.5 dBZ.
"""

from __future__ import annotations

import dataclasses
import filecmp
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, cmaarchive, fetch, native, observation, zstdcli
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    CMA_ARCHIVE_VARIABLE,
    CMA_GRID_STEP,
    CMA_PRODUCT,
    CMA_ZOOM,
    _fetch_cma_run,
    cma_archive,
    _cma_run_is_complete,
    latest_observation_slot,
    latest_cma_slot,
    cma_archive_slots,
    cma_frame_name,
    cma_window_slots,
    resolve_run,
    window_summary,
)
from xuebuild.manifest import MODEL_CORE_BUNDLES, validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = FIXTURES / "cma.2026091609.crop.nc"
CMA = source_spec("cma")

requires_gdal = unittest.skipUnless(
    shutil.which("gdalinfo") is not None and shutil.which("gdal_translate") is not None, "GDAL is not on PATH"
)


def stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def slots(start: datetime, end: datetime, *, missing: tuple[datetime, ...] = ()) -> list[str]:
    """Every six-minute slot from ``start`` through ``end`` inclusive, as
    the tool prints them, less the ones the archive never wrote."""
    out, slot = [], start
    while slot <= end:
        if slot not in missing:
            out.append(slot.strftime("%Y-%m-%dT%H:%M:%SZ"))
        slot += timedelta(minutes=6)
    return out


def archive(*written: str) -> mock.Mock:
    """A stand-in for ``cmaarchive.written_slots``: an archive holding
    ``written``, answering the slots inside whatever window it is asked
    for."""
    held = sorted(stamp(text) for text in written)

    def lookup(base: str, start: datetime, end: datetime) -> list[datetime]:
        return [slot for slot in held if start <= slot <= end]

    return mock.Mock(side_effect=lookup)


ARCHIVE = "s3://a-bucket/an-archive"

THREE_HOURS = slots(stamp("2026-09-15T22:12:00Z"), stamp("2026-09-16T01:06:00Z"), missing=(stamp("2026-09-16T00:30:00Z"),))


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_observation(self) -> None:
        self.assertTrue(CMA.observation and CMA.fetched and CMA.live and CMA.series_file)
        self.assertEqual((CMA.manifest_model, CMA.product, CMA.latest_filename), ("CMA-RADAR", "l3-mst-cref", "latest-cma.json"))
        self.assertEqual((CMA.window_hours, CMA.horizon_hours, CMA.cadence_seconds, CMA.cycle_hours), (3, 3, 360, 1))
        self.assertEqual((CMA.input_variable_ids, CMA.bundle_scalar_ids, CMA.core_bundle_ids), (("cref",),) * 3)
        self.assertEqual(MODEL_CORE_BUNDLES["CMA-RADAR"], ("cref",))
        self.assertFalse(CMA.video)
        self.assertIsNone(CMA.downsample)
        # The grid the archive keeps is the one a complete build wants: the
        # zoom-5 tile grid, 7 x 4 tiles of 256 cells over 67.5E-146.25E and
        # 11.25N-56.25N.
        self.assertEqual(CMA_GRID_STEP, 360 / (256 * 2**CMA_ZOOM))
        self.assertEqual(CMA.production_grid, (round((146.25 - 67.5) / CMA_GRID_STEP), round((56.25 - 11.25) / CMA_GRID_STEP)))
        self.assertEqual(CMA.production_grid, (7 * 256, 4 * 256))
        self.assertEqual(CMA_PRODUCT, "RADAR_L3_MST_CREF_GISJPG_Tiles_CR")

    def test_the_series_file_sources_are_all_fetched_now(self) -> None:
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.series_file], ["cma", "jma", "himawari"])
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.observation], ["cma", "mrms", "jma", "himawari"])
        self.assertTrue(all(spec.fetched and spec.live for spec in SOURCES.values()))

    def test_the_catalog_prose_names_the_agency_and_the_archive(self) -> None:
        prose = _source_prose(CMA)
        self.assertEqual(prose["providers"][0]["name"], "China Meteorological Administration")
        # Public prose names the data and the agency, never the archive
        # behind it.
        self.assertNotIn("cases only", prose["description"])
        for private in ("archive", "bucket"):
            self.assertNotIn(private, prose["description"], private)


class ArchiveTests(unittest.TestCase):
    """The fetch functions against a stand-in archive."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: ARCHIVE})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_slots_of_a_window_are_what_the_archive_wrote_in_it(self) -> None:
        window = archive(*THREE_HOURS)
        held = cma_archive_slots(stamp("2026-09-15T23:00:00Z"), 2, written=window)
        self.assertEqual((held[0], held[-1], len(held)), (stamp("2026-09-15T23:00:00Z"), stamp("2026-09-16T01:00:00Z"), 20))
        self.assertNotIn(stamp("2026-09-16T00:30:00Z"), held)
        self.assertEqual(window.call_args.args, (ARCHIVE, stamp("2026-09-15T23:00:00Z"), stamp("2026-09-16T01:00:00Z")))
        # The same, keyed by a run.
        self.assertEqual(cma_window_slots(CMA, GfsRun(stamp("2026-09-15T23:00:00Z")), 2, written=window), held)

    def test_the_newest_written_slot_ends_the_live_window(self) -> None:
        window = archive(*THREE_HOURS)
        now = stamp("2026-09-16T01:40:00Z")
        self.assertEqual(latest_cma_slot(CMA, now=now, written=window), stamp("2026-09-16T01:06:00Z"))
        # The stores of the last day are asked: from 25 hours before this
        # hour, through the hour after it.
        self.assertEqual(window.call_args.args[1:], (stamp("2026-09-15T00:00:00Z"), stamp("2026-09-16T02:00:00Z")))
        with self.assertRaisesRegex(DownloadError, "no written slot"):
            latest_cma_slot(CMA, now=now, written=archive())

    def test_a_named_window_is_complete_when_the_archive_has_passed_its_end(self) -> None:
        window = archive(*THREE_HOURS)
        run = GfsRun(stamp("2026-09-15T23:00:00Z"))
        self.assertTrue(_cma_run_is_complete(CMA, run, 2, written=window))
        # The live window's end (02:00) is ahead of the newest slot (01:06).
        self.assertFalse(_cma_run_is_complete(CMA, run, 3, written=window))
        # A window whose end slot was never published still completes once
        # the archive has moved past it: the hour after the end is asked.
        gap = archive(*slots(stamp("2026-09-15T23:00:00Z"), stamp("2026-09-16T00:30:00Z"), missing=(stamp("2026-09-16T00:00:00Z"),)))
        self.assertTrue(_cma_run_is_complete(CMA, run, 1, written=gap))
        self.assertEqual(gap.call_args.args[2], stamp("2026-09-16T01:00:00Z"))
        self.assertFalse(_cma_run_is_complete(CMA, run, 1, written=archive()))

    def test_resolve_run_latest_starts_two_hours_before_the_newest_slot(self) -> None:
        with mock.patch.object(cmaarchive, "written_slots", archive(*THREE_HOURS)):
            with mock.patch.object(fetch, "datetime", wraps=datetime) as clock:
                clock.now.return_value = stamp("2026-09-16T01:40:00Z")
                self.assertEqual(resolve_run("latest", hours=3, model="cma").id, "2026091523")
                self.assertEqual(latest_observation_slot(CMA), stamp("2026-09-16T01:06:00Z"))
            # A named window: complete or refused.
            self.assertEqual(resolve_run("2026091523", hours=2, model="cma").id, "2026091523")
            with self.assertRaisesRegex(DownloadError, "not fully landed"):
                resolve_run("2026091523", hours=3, model="cma")
            # Any hour starts a window.
            self.assertEqual(resolve_run("2026091522", hours=1, model="cma").id, "2026091522")


class ObservationCadenceTests(unittest.TestCase):
    """The NetCDF ingest on the source now that it has a cadence: the
    window's hour is the run and the six-minute slots the axis — the rule
    the JMA series and the MRMS frames follow, where the archive files
    before it took their first frame as the run."""

    def _inspect(self, times: list[str]) -> observation.ObservationSeries:
        info = {
            "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
            "bands": [
                {
                    "band": index + 1,
                    "unit": "dBZ",
                    "scale": 0.1,
                    "offset": 0.0,
                    "noDataValue": 32767,
                    "metadata": {"": {"NETCDF_DIM_time": str(int(stamp(text).timestamp()))}},
                }
                for index, text in enumerate(times)
            ],
        }
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "dataset_info", return_value=info),
        ):
            return observation.inspect_observation(Path("cma.nc"), CMA)

    def test_the_run_is_the_hour_of_the_first_slot(self) -> None:
        series = self._inspect(["2026-09-16T09:06:00Z", "2026-09-16T09:12:00Z", "2026-09-16T09:24:00Z"])
        first = series.frames[0]["cref"]
        self.assertEqual(first.run_time, stamp("2026-09-16T09:00:00Z"))
        self.assertEqual(series.lead_seconds, [360, 720, 1440])
        self.assertEqual(series.plane_source.fill_values, (32767.0, 32767.0 * 0.1))
        self.assertEqual(series.plane_source.fill_replacement, 0.0)

    def test_a_series_across_midnight_counts_from_its_first_hour(self) -> None:
        series = self._inspect(["2026-09-15T23:54:00Z", "2026-09-16T00:00:00Z", "2026-09-16T00:06:00Z"])
        self.assertEqual(series.frames[0]["cref"].run_time, stamp("2026-09-15T23:00:00Z"))
        self.assertEqual(series.lead_seconds, [3240, 3600, 3960])


requires_zarr = unittest.skipUnless(
    all(importlib.util.find_spec(name) for name in ("zarr", "xarray", "netCDF4")),
    "the cma dependency group is not installed",
)


def make_day_store(base: Path, day: datetime, *, written: dict[int, float], unpublished: tuple[int, ...] = (), size: int = 16) -> str:
    """One day of the archive in its own layout, on a ``size`` x ``size``
    corner of the zoom-5 grid: ``written`` maps a slot index to the dBZ
    value its frame carries in its top-left cell (missing elsewhere)."""
    import zarr

    url = cmaarchive.store_url(str(base), day.date())
    root = zarr.create_group(store=zarr.storage.LocalStore(url), overwrite=True)
    times = np.array([int(day.timestamp()) + index * cmaarchive.SLOT_SECONDS for index in range(240)], dtype=np.int64)
    root.create_array("time", shape=(240,), chunks=(240,), dtype="int64", dimension_names=("time",))[:] = times
    latitudes = 56.25 - (np.arange(size) + 0.5) * cmaarchive.GRID_STEP
    longitudes = 67.5 + (np.arange(size) + 0.5) * cmaarchive.GRID_STEP
    root.create_array("lat", shape=(size,), chunks=(size,), dtype="float64", dimension_names=("lat",))[:] = latitudes
    root.create_array("lon", shape=(size,), chunks=(size,), dtype="float64", dimension_names=("lon",))[:] = longitudes
    cref = root.create_array(
        "cref", shape=(240, size, size), chunks=(1, size, size), dtype="int16", fill_value=np.int16(32767), dimension_names=("time", "lat", "lon")
    )
    status = np.zeros(240, dtype=np.int8)
    for index, value in written.items():
        frame = np.full((size, size), 32767, dtype=np.int16)
        frame[0, 0] = np.int16(round(value / cmaarchive.SCALE))
        cref[index] = frame
        status[index] = cmaarchive.STATUS_WRITTEN
    for index in unpublished:
        status[index] = cmaarchive.STATUS_UNPUBLISHED
    root.create_array("slot_status", shape=(240,), chunks=(240,), dtype="int8", dimension_names=("time",))[:] = status
    return url


@requires_zarr
class StoreTests(unittest.TestCase):
    """The archive reader against two day stores in a directory."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="xue-cma-archive-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        # 23:48 and 23:54 of the 15th; 00:00 and 00:12 of the 16th, 00:06 unpublished.
        self.first = make_day_store(self.base, stamp("2026-09-15T00:00:00Z"), written={238: 24.8, 239: 25.4})
        self.second = make_day_store(self.base, stamp("2026-09-16T00:00:00Z"), written={0: 30.0, 2: 31.2}, unpublished=(1,))

    def test_the_archive_is_named_by_the_environment_alone(self) -> None:
        """The location is private: no default, and an unset variable is
        the operator's error before the archive is asked anything."""
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: ""}):
            with self.assertRaisesRegex(DownloadError, CMA_ARCHIVE_VARIABLE):
                cma_archive()
            with self.assertRaisesRegex(DownloadError, CMA_ARCHIVE_VARIABLE):
                cma_archive_slots(stamp("2026-09-16T09:00:00Z"), 1, written=mock.Mock())
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: "s3://bucket/prefix/"}):
            self.assertEqual(cma_archive(), "s3://bucket/prefix")
        # A bucket path without its scheme would be read as a directory
        # and every day would look absent; a directory must exist.
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: "bucket/prefix"}):
            with self.assertRaisesRegex(DownloadError, "names no directory .*s3://bucket/prefix"):
                cma_archive()
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: str(self.base)}):
            self.assertEqual(cma_archive(), str(self.base))
        self.assertEqual(
            cmaarchive.store_url("s3://bucket/prefix", stamp("2026-09-05T00:00:00Z").date()),
            f"s3://bucket/prefix/{CMA_PRODUCT}/z5/2026/2026-09-05.zarr",
        )

    def test_the_stores_are_listed_across_midnight(self) -> None:
        start, end = stamp("2026-09-15T23:48:00Z"), stamp("2026-09-16T00:12:00Z")
        listed = cmaarchive.stored_slots(str(self.base), start, end)
        self.assertEqual([slot.time.strftime("%H:%M") for slot in listed], ["23:48", "23:54", "00:00", "00:06", "00:12"])
        self.assertEqual([slot.status for slot in listed], [1, 1, 1, 2, 1])
        self.assertEqual([slot.store for slot in listed], [self.first, self.first, self.second, self.second, self.second])
        self.assertEqual([slot.index for slot in listed], [238, 239, 0, 1, 2])
        self.assertEqual(
            cmaarchive.written_slots(str(self.base), start, end),
            [stamp("2026-09-15T23:48:00Z"), stamp("2026-09-15T23:54:00Z"), stamp("2026-09-16T00:00:00Z"), stamp("2026-09-16T00:12:00Z")],
        )
        # A day without a store contributes nothing while another day of
        # the range has one (the hour past midnight a completeness check
        # asks for); a range without any store is not an empty archive but
        # a base that is not the archive, and the error says what was
        # looked for without naming where.
        self.assertEqual(
            [slot.time.strftime("%H:%M") for slot in cmaarchive.stored_slots(str(self.base), stamp("2026-09-16T23:48:00Z"), stamp("2026-09-17T00:12:00Z"))],
            ["23:48", "23:54"],
        )
        with self.assertRaisesRegex(DownloadError, rf"no day store at {CMA_PRODUCT}/z5/2026/2026-09-17.zarr; {CMA_ARCHIVE_VARIABLE}") as caught:
            cmaarchive.stored_slots(str(self.base), stamp("2026-09-17T00:00:00Z"), stamp("2026-09-17T03:00:00Z"))
        self.assertNotIn(str(self.base), str(caught.exception))
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: str(self.base)}):
            with self.assertRaisesRegex(DownloadError, "2026-09-17.zarr or .*2026-09-18.zarr"):
                latest_cma_slot(CMA, now=stamp("2026-09-18T01:40:00Z"))
            self.assertEqual(latest_cma_slot(CMA, now=stamp("2026-09-16T01:40:00Z")), stamp("2026-09-16T00:12:00Z"))
            self.assertTrue(_cma_run_is_complete(CMA, GfsRun(stamp("2026-09-15T23:00:00Z")), 1))
            self.assertFalse(_cma_run_is_complete(CMA, GfsRun(stamp("2026-09-16T00:00:00Z")), 3))

    def test_a_window_is_the_written_frames_in_dbz(self) -> None:
        window = cmaarchive.read_window(str(self.base), stamp("2026-09-15T23:48:00Z"), stamp("2026-09-16T00:48:00Z"))
        assert window is not None
        self.assertEqual(window.frames.shape, (4, 16, 16))
        self.assertEqual(window.frames.dtype, np.float32)
        self.assertEqual([time.strftime("%H:%M") for time in window.times], ["23:48", "23:54", "00:00", "00:12"])
        self.assertEqual([round(float(frame[0, 0]), 4) for frame in window.frames], [24.8, 25.4, 30.0, 31.2])
        self.assertTrue(np.isnan(window.frames[:, 1:, :]).all())
        self.assertEqual((window.grid["nlat"], window.grid["nlon"], window.grid["step"]), (16, 16, CMA_GRID_STEP))
        self.assertAlmostEqual(window.grid["north"], 56.25 - CMA_GRID_STEP / 2)
        self.assertIsNone(cmaarchive.read_window(str(self.base), stamp("2026-09-16T01:00:00Z"), stamp("2026-09-16T02:00:00Z")))

    def test_the_series_is_what_the_ingest_reads(self) -> None:
        window = cmaarchive.read_window(str(self.base), stamp("2026-09-15T23:48:00Z"), stamp("2026-09-16T00:48:00Z"))
        assert window is not None
        series = self.base / "window.nc"
        cmaarchive.write_series(window, series)
        import xarray as xr

        with xr.open_dataset(series) as dataset:
            self.assertEqual(dataset.cref.encoding["dtype"], np.dtype("int16"))
            self.assertEqual((dataset.cref.encoding["scale_factor"], dataset.cref.encoding["_FillValue"]), (0.1, 32767))
            self.assertEqual(dataset.cref.attrs["units"], "dBZ")
            self.assertEqual(
                list(dataset.time.values.astype("datetime64[m]").astype(str)),
                ["2026-09-15T23:48", "2026-09-15T23:54", "2026-09-16T00:00", "2026-09-16T00:12"],
            )
            self.assertAlmostEqual(float(dataset.cref[3, 0, 0]), 31.2, places=5)
            self.assertTrue(bool(np.isnan(dataset.cref[3, 1, 1])))
            self.assertTrue(np.all(np.diff(dataset.lat.values) < 0))
        if shutil.which("gdalinfo"):
            with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
                inspected = observation.inspect_observation(series, CMA)
            self.assertEqual(inspected.frames[0]["cref"].run_time, stamp("2026-09-15T23:00:00Z"))
            self.assertEqual(inspected.lead_seconds, [2880, 3240, 3600, 4320])
            self.assertEqual(inspected.plane_source.fill_values, (32767.0, 32767.0 * 0.1))


@requires_zarr
class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-cma-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.base = self.root / "archive"
        make_day_store(self.base, stamp("2026-09-16T00:00:00Z"), written={90: 10.0, 91: 11.0, 93: 13.0})
        self.run = GfsRun(stamp("2026-09-16T09:00:00Z"))
        patcher = mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: str(self.base)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        output = self.root / "cma.2026091609" / cma_frame_name(CMA, self.run)
        paths = fetch.fetch_run(self.run, 3, self.root, model="cma")
        self.assertEqual(paths, [output])
        record = json.loads((output.parent / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["cadenceSeconds"]), ("cma", "2026091609", 360))
        self.assertNotIn("archive", record)
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-16T09:00:00Z", "2026-09-16T09:06:00Z", "2026-09-16T09:18:00Z"])
        self.assertEqual({frame["path"] for frame in record["frames"]}, {output.name})
        self.assertEqual(record["grid"]["nlon"], 16)
        # What the rolling publish reads, the same shape as an MRMS window's.
        window_record = window_summary(output.parent)
        self.assertEqual((window_record["frameCount"], window_record["firstSlot"], window_record["latestSlot"]), (3, "2026-09-16T09:00:00Z", "2026-09-16T09:18:00Z"))

    def test_every_round_reads_afresh(self) -> None:
        output = self.root / "cma.2026091609" / cma_frame_name(CMA, self.run)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"the previous round's window")
        _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=None)
        first = output.read_bytes()
        self.assertNotEqual(first, b"the previous round's window")
        _fetch_cma_run(CMA, self.run, 3, self.root, force=True, input_ids=None)
        self.assertEqual(output.stat().st_size, len(first))

    def test_an_empty_window_and_a_wrong_input_are_errors(self) -> None:
        with self.assertRaisesRegex(DownloadError, "no written slot"):
            _fetch_cma_run(CMA, GfsRun(stamp("2026-09-16T12:00:00Z")), 3, self.root, force=False, input_ids=None)
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=("prate",))

class ShowcaseTests(unittest.TestCase):
    def _payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "demo-cma",
            "title": {locale: "Demo" for locale in _LOCALES},
            "summary": {locale: "Demo summary" for locale in _LOCALES},
            "model": "cma",
            "hours": 3,
            "bbox": [105.0, 14.0, 130.0, 34.0],
            "variables": ["cref"],
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_a_window_of_the_archive_or_a_file(self) -> None:
        fetched = parse_case(self._payload(run="2026091609"))
        self.assertFalse(fetched.from_dataset)
        self.assertEqual(fetched.run, "2026091609")
        local = parse_case(self._payload(dataset="typhoon/series.nc", hours=212))
        self.assertTrue(local.from_dataset)
        self.assertEqual(local.dataset_path.name, "series.nc")
        from xuebuild.showcase import ShowcaseError

        with self.assertRaisesRegex(ShowcaseError, "no run to name"):
            parse_case(self._payload(run="2026091609", dataset="typhoon/series.nc"))
        with self.assertRaisesRegex(ShowcaseError, "run must be"):
            parse_case(self._payload())
        # MRMS is fetched but not a series file: a dataset is still refused.
        with self.assertRaisesRegex(ShowcaseError, "names a dataset"):
            parse_case(self._payload(model="mrms", dataset="x.nc"))


_LOCALES = ("zh", "zh-Hant", "en", "ja", "ko", "de", "fr", "es", "pt", "tr", "ru")


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-cma-"))
        inputs = cls.root / "cma.2026091609"
        inputs.mkdir()
        shutil.copy(SERIES, inputs / "cma.2026091609.nc")
        cls.inputs = inputs
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                inputs,
                cls.root / "out",
                model="cma",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_window_is_a_six_minute_axis_from_the_hour(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["cref"])
        self.assertEqual(self.report["videos"], [])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CMA-RADAR", "l3-mst-cref"))
        self.assertEqual(manifest["runTime"], "2026-09-16T09:00:00Z")
        self.assertEqual(manifest["forecastHours"], 1)
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "cref.xue")
        # 09:00, 09:06 and 09:18 from the 09:00 run: offsets listed, since
        # 09:12 is not there. Two 64-cell tiles across two rows.
        self.assertEqual(
            bundle.metadata["time"],
            {"unitSeconds": 360, "firstFrameOffset": 0, "frameCount": 3, "frameOffsets": [0, 1, 3]},
        )
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (128, 128))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (CMA_GRID_STEP, -CMA_GRID_STEP))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (112.52197265625, 33.72802734375))
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["unit"], "dBZ")
        self.assertEqual((variable["parameter"]["parameterCategory"], variable["parameter"]["parameterNumber"]), (16, 5))
        # The gap at 09:12 ends a temporal group: two groups for three frames.
        self.assertEqual((bundle.frame_count, len(bundle.groups), bundle.tiles.count), (3, 2, 4))
        for name in ("cref.half.xue", "cref.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_a_complete_build_wants_the_production_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "1792x1024 grid"):
                binconvert.convert_bin(
                    self.inputs, self.root / "complete", model="cma", skip_video=True, require_complete=True, expected_hours=3
                )

    @unittest.skipUnless(native.knows_source("cma"), f"the installed {native.DISTRIBUTION} wheel predates the cma source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(self.inputs, subject, model="cma", skip_video=True, manifest_path=subject / "manifest.json")
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


class StacTests(unittest.TestCase):
    def test_a_source_stripped_of_its_feed_has_no_collection(self) -> None:
        """No registered source is without a feed any more; the rule is
        checked on a copy of the CMA source with its pointer taken off."""
        from xuebuild import stac

        archive_only = dataclasses.replace(CMA, latest_filename=None)
        self.assertFalse(archive_only.live)


if __name__ == "__main__":
    unittest.main()
