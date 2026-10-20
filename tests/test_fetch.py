from __future__ import annotations

import threading
import tempfile
import unittest
import urllib.error
from email.message import Message
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from xuebuild.errors import DownloadError
from xuebuild.fetch import (
    ECMWF_OPEN_DATA_MODELS,
    _download_ecmwf_payload,
    _request,
    _run_is_complete,
    ecmwf_companion_object_url,
    ecmwf_object_part,
    ecmwf_object_url,
    model_object_url,
    fetch_frame,
    fetch_range,
    fetch_run,
    floor_to_cycle,
    object_url,
    parse_run,
    remote_exists,
    resolve_run,
    sflux_object_url,
)
from xuebuild.idx import ByteRange
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec


class _RangeHandler(BaseHTTPRequestHandler):
    payload = b"0123456789"

    def do_GET(self) -> None:
        value = self.headers.get("Range", "")
        start, end = (int(part) for part in value.removeprefix("bytes=").split("-"))
        body = self.payload[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class FetchTests(unittest.TestCase):
    def test_existing_valid_frame_is_reused_without_network_request(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 6, tzinfo=UTC))
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            existing = destination / "gfs.2026081506.f000.grib2"
            existing.write_bytes(b"existing GRIB")

            with (
                patch("xuebuild.gdal.inspect_grib") as inspect_grib,
                patch("xuebuild.fetch.fetch_text") as fetch_text,
                patch("xuebuild.fetch.fetch_range") as fetch_range,
            ):
                result = fetch_frame(run, 0, destination)

            self.assertEqual(result, existing)
            self.assertEqual(inspect_grib.call_count, len(source_spec("gfs").input_variable_ids))
            fetch_text.assert_not_called()
            fetch_range.assert_not_called()

    def test_ecmwf_object_url_and_resolution(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 6, tzinfo=UTC))
        self.assertEqual(
            ecmwf_object_url(run, 0),
            "https://storage.googleapis.com/ecmwf-open-data/20260815/06z/ifs/0p25/oper/"
            "20260815060000-0h-oper-fc.grib2",
        )
        self.assertTrue(ecmwf_object_url(run, 120).endswith("20260815060000-120h-oper-fc.grib2"))

        now = datetime(2026, 8, 15, 11, 35, tzinfo=UTC)

        def exists(url: str) -> bool:
            return "/20260815/00z/" in url

        resolved = resolve_run("latest", hours=120, now=now, exists=exists, model="ecmwf")
        self.assertEqual(resolved.id, "2026081500")

    def test_ecmwf_resolution_uses_a_healthy_mirror(self) -> None:
        now = datetime(2026, 8, 15, 5, 0, tzinfo=UTC)
        calls: list[str] = []

        def exists(url: str) -> bool:
            calls.append(url)
            if "storage.googleapis.com" in url:
                raise DownloadError("503 Slow Down")
            return "ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in url

        resolved = resolve_run("latest", hours=120, now=now, exists=exists, model="ecmwf")

        self.assertEqual(resolved.id, "2026081500")
        self.assertTrue(any("ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in url for url in calls))

    def test_request_honours_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "17"
        unavailable = urllib.error.HTTPError(
            "https://storage.googleapis.com/ecmwf-open-data/file",
            503,
            "Slow Down",
            headers,
            None,
        )
        response = object()
        with (
            patch("xuebuild.fetch.ECMWF_REQUEST_INTERVAL", 0),
            patch("xuebuild.fetch.time.sleep") as sleep,
        ):
            result = _request(
                "https://storage.googleapis.com/ecmwf-open-data/file",
                attempts=2,
                opener=Mock(side_effect=[unavailable, response]),
            )

        self.assertIs(result, response)
        sleep.assert_called_once_with(17.0)

    def test_remote_exists_only_treats_404_as_missing(self) -> None:
        def failure(code: int) -> DownloadError:
            http_error = urllib.error.HTTPError("https://example.test", code, "error", {}, None)
            error = DownloadError("probe failed")
            error.__cause__ = http_error
            return error

        with patch("xuebuild.fetch._request", side_effect=failure(404)):
            self.assertFalse(remote_exists("https://example.test/missing"))
        with patch("xuebuild.fetch._request", side_effect=failure(503)):
            with self.assertRaises(DownloadError):
                remote_exists("https://example.test/throttled")

    def test_ecmwf_frame_switches_mirrors_as_a_unit(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 0, tzinfo=UTC))
        requested_indexes: list[str] = []
        requested_ranges: list[str] = []

        def fetch_text(url: str) -> str:
            requested_indexes.append(url)
            if "storage.googleapis.com" in url:
                raise DownloadError("503 Slow Down")
            return "index"

        def fetch_range(url: str, byte_range: ByteRange) -> bytes:
            requested_ranges.append(url)
            return b"x"

        with (
            patch("xuebuild.fetch.fetch_text", side_effect=fetch_text),
            patch("xuebuild.fetch.fetch_range", side_effect=fetch_range),
            patch("xuebuild.fetch.ecmwf_field_byte_range", return_value=ByteRange(0, 0)),
        ):
            payload = _download_ecmwf_payload(run, 3, source_spec("ecmwf"))

        # Every input but the analysis-only orography, the wave family's from
        # its own stream, all from the mirror the frame settled on: the
        # `oper` index of the throttled mirror, then that mirror's `oper`
        # and `wave` indexes.
        fetched = [
            variable_id
            for variable_id in source_spec("ecmwf").input_variable_ids
            if not variable_spec(variable_id).static
        ]
        self.assertEqual(payload, b"x" * len(fetched))
        self.assertEqual(len(requested_indexes), 3)
        self.assertTrue(requested_indexes[1].endswith("/oper/20260815000000-3h-oper-fc.index"))
        self.assertTrue(requested_indexes[2].endswith("/wave/20260815000000-3h-wave-fc.index"))
        self.assertTrue(
            all(
                "ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in url
                for url in requested_ranges
            )
        )
        self.assertEqual(sum("-wave-fc.grib2" in url for url in requested_ranges), 3)

    def test_the_0p1_ifs_objects_are_split_by_level_type(self) -> None:
        run = GfsRun(datetime(2026, 10, 20, 12, tzinfo=UTC))
        prefix = "https://storage.googleapis.com/ecmwf-open-data/20261020/12z/ifs/0p1/"
        self.assertEqual(
            ecmwf_object_url(run, 3, model="ifs", part="sfc"), prefix + "oper/20261020120000-3h-oper-fc-sfc.grib2"
        )
        self.assertEqual(
            ecmwf_object_url(run, 3, model="ifs", part="pl"), prefix + "oper/20261020120000-3h-oper-fc-pl.grib2"
        )
        self.assertEqual(
            ecmwf_companion_object_url(run, 240, "wave", model="ifs"),
            prefix + "wave/20261020120000-240h-wave-fc-sfc.grib2",
        )
        self.assertEqual(model_object_url(run, 0, "ifs"), ecmwf_object_url(run, 0, model="ifs", part="sfc"))
        # The 0.25° objects stay combined, and the old call shapes name them.
        self.assertEqual(
            ecmwf_object_url(run, 3),
            "https://storage.googleapis.com/ecmwf-open-data/20261020/12z/ifs/0p25/oper/20261020120000-3h-oper-fc.grib2",
        )
        self.assertEqual(model_object_url(run, 3, "ecmwf"), ecmwf_object_url(run, 3))
        self.assertTrue(ecmwf_companion_object_url(run, 3, "wave").endswith("/wave/20261020120000-3h-wave-fc.grib2"))
        # A part is required on a split oper stream, refused on a combined
        # one, and the wave stream of a split source has only its -sfc object.
        with self.assertRaisesRegex(DownloadError, "sfc or pl"):
            ecmwf_object_url(run, 3, model="ifs")
        with self.assertRaises(DownloadError):
            ecmwf_object_url(run, 3, model="ecmwf", part="sfc")
        with self.assertRaises(DownloadError):
            ecmwf_object_url(run, 3, model="ifs", stream="wave", part="pl")
        with self.assertRaises(DownloadError):
            ecmwf_object_url(run, 3, model="gfs")
        self.assertEqual(ECMWF_OPEN_DATA_MODELS["ifs"].short_cycle_hours, 144)
        self.assertEqual(ECMWF_OPEN_DATA_MODELS["ecmwf"].short_cycle_hours, 90)
        self.assertIsNone(ECMWF_OPEN_DATA_MODELS["aifs"].short_cycle_hours)
        self.assertEqual(
            {model: (service.directory, service.resolution, service.split) for model, service in ECMWF_OPEN_DATA_MODELS.items()},
            {"ecmwf": ("ifs", "0p25", False), "aifs": ("aifs-single", "0p25", False), "ifs": ("ifs", "0p1", True)},
        )

    def test_the_part_follows_the_level_type(self) -> None:
        self.assertEqual(ecmwf_object_part("hgt500"), "pl")
        self.assertEqual(ecmwf_object_part("ugrd200"), "pl")
        self.assertEqual(ecmwf_object_part("tmp2m"), "sfc")
        self.assertEqual(ecmwf_object_part("prmsl"), "sfc")
        # The soil layers (levtype sol) travel with the surface records.
        self.assertEqual(ecmwf_object_part("tsoil1"), "sfc")
        self.assertEqual(ecmwf_object_part("soilw4"), "sfc")

    def test_an_ifs_run_is_complete_only_with_all_three_objects(self) -> None:
        run = GfsRun(datetime(2026, 10, 20, 12, tzinfo=UTC))
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "-240h-oper-fc-pl" not in url

        self.assertFalse(_run_is_complete(run, 240, "ifs", exists))
        names = {url.rsplit("/", 1)[1] for url in probed}
        self.assertEqual(
            names,
            {
                f"20261020120000-{hour}h-{name}.grib2"
                for hour in (0, 240)
                for name in ("oper-fc-sfc", "oper-fc-pl", "wave-fc-sfc")
            },
        )
        self.assertTrue(all("/ifs/0p1/" in url for url in probed))
        self.assertTrue(_run_is_complete(run, 240, "ifs", lambda url: True))

    def test_ifs_resolution_skips_the_short_cycles_only_past_144_hours(self) -> None:
        now = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
        self.assertEqual(resolve_run("latest", hours=144, now=now, exists=lambda url: True, model="ifs").id, "2026102018")
        self.assertEqual(resolve_run("latest", hours=240, now=now, exists=lambda url: True, model="ifs").id, "2026102012")
        # The 0.25° short cycles stop at 90 h.
        self.assertEqual(resolve_run("latest", hours=120, now=now, exists=lambda url: True, model="ecmwf").id, "2026102012")
        self.assertEqual(resolve_run("latest", hours=90, now=now, exists=lambda url: True, model="ecmwf").id, "2026102018")

    def test_an_ifs_frame_reads_each_part_from_its_own_index_in_source_order(self) -> None:
        run = GfsRun(datetime(2026, 10, 20, 12, tzinfo=UTC))
        spec = source_spec("ifs")
        requested_indexes: list[str] = []
        located: list[tuple[str, str]] = []

        def fetch_text(url: str) -> str:
            requested_indexes.append(url)
            return url.rsplit("-", 1)[1].removesuffix(".index")

        def locate(text: str, param: str, **kwargs: object) -> ByteRange:
            located.append((text, param))
            return ByteRange(len(located), len(located))

        def fetch_range(url: str, byte_range: ByteRange) -> bytes:
            return url.rsplit("-", 1)[1].removesuffix(".grib2").encode() + b","

        with (
            patch("xuebuild.fetch.fetch_text", side_effect=fetch_text),
            patch("xuebuild.fetch.fetch_range", side_effect=fetch_range),
            patch("xuebuild.fetch.ecmwf_field_byte_range", side_effect=locate),
        ):
            payload = _download_ecmwf_payload(run, 3, spec)

        wanted = [variable_id for variable_id in spec.input_variable_ids if not variable_spec(variable_id).static]
        expected_parts = [
            "sfc" if spec.companion_of(variable_id) is not None else ecmwf_object_part(variable_id) for variable_id in wanted
        ]
        # One index per object, the first time a record needs it; every
        # record located in the index of its own object; the bytes in the
        # source's input order whichever object each came from.
        self.assertEqual(
            [url.rsplit("/", 1)[1] for url in requested_indexes],
            ["20261020120000-3h-oper-fc-sfc.index", "20261020120000-3h-oper-fc-pl.index", "20261020120000-3h-wave-fc-sfc.index"],
        )
        self.assertEqual([text for text, _ in located], expected_parts)
        self.assertEqual([param for _, param in located], [variable_spec(variable_id).ecmwf_param for variable_id in wanted])
        self.assertEqual(payload.decode().rstrip(",").split(","), expected_parts)
        self.assertEqual(expected_parts.count("pl"), 44)

    def test_a_frame_that_carries_none_of_the_requested_records_is_refused(self) -> None:
        # A static field is fetched at the analysis alone, so a later frame
        # asked for it by itself carries nothing. That is an error rather
        # than a file of no records a repack or an inspection would fail on.
        run = GfsRun(datetime(2026, 8, 15, 0, tzinfo=UTC))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(DownloadError, "static or optional"):
                fetch_frame(run, 3, Path(temporary), model="gfs", input_ids=("orog",))

    def test_ecmwf_run_retries_only_the_failed_frame(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 0, tzinfo=UTC))
        attempts: list[int] = []

        def fetch(run: GfsRun, hour: int, destination: Path, **kwargs: object) -> Path:
            attempts.append(hour)
            if hour == 3 and attempts.count(3) == 1:
                raise DownloadError("temporary failure")
            return destination / f"ecmwf.{run.id}.f{hour:03d}.grib2"

        with (
            patch("xuebuild.fetch.fetch_frame", side_effect=fetch),
            patch("xuebuild.fetch.random.uniform", return_value=60),
            patch("xuebuild.fetch.time.sleep") as sleep,
        ):
            paths = fetch_run(run, 3, Path("raw"), model="ecmwf")

        self.assertEqual(attempts, [0, 3, 3])
        self.assertEqual(len(paths), 2)
        sleep.assert_called_once_with(60)

    def test_cycle_floor_and_fallback(self) -> None:
        now = datetime(2026, 8, 15, 11, 35, tzinfo=UTC)
        self.assertEqual(floor_to_cycle(now).hour, 6)
        calls: list[str] = []

        def exists(url: str) -> bool:
            calls.append(url)
            return "gfs.20260815/00/" in url

        run = resolve_run("latest", hours=120, now=now, exists=exists)
        self.assertEqual(run.id, "2026081500")
        self.assertTrue(object_url(run, 120).endswith("gfs.t00z.pgrb2.0p25.f120"))
        self.assertGreaterEqual(len(calls), 3)

    def test_archived_runs_use_the_layout_of_their_day(self) -> None:
        # NOAA moved the per-cycle files under atmos/ on 2021-03-23; the
        # archived cycles before it, which the showcase cases reach into,
        # keep them directly under the cycle.
        modern = parse_run("2021032300")
        legacy = parse_run("2021032218")
        self.assertIn("/00/atmos/gfs.t00z.pgrb2.0p25.f006", object_url(modern, 6))
        self.assertIn("/18/gfs.t18z.pgrb2.0p25.f006", object_url(legacy, 6))
        self.assertNotIn("atmos", object_url(legacy, 6))
        self.assertIn("/18/gfs.t18z.sfluxgrbf006.grib2", sflux_object_url(legacy, 6))

    def test_http_range_requires_and_reads_206(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/file"
            self.assertEqual(fetch_range(url, ByteRange(2, 6)), b"23456")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
