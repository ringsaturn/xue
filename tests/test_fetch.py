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

from xue.errors import DownloadError
from xue.fetch import (
    _download_ecmwf_payload,
    _request,
    ecmwf_object_url,
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
from xue.idx import ByteRange
from xue.model import GfsRun
from xue.sources import source_spec


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
                patch("xue.gdal.inspect_grib") as inspect_grib,
                patch("xue.fetch.fetch_text") as fetch_text,
                patch("xue.fetch.fetch_range") as fetch_range,
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
            patch("xue.fetch.ECMWF_REQUEST_INTERVAL", 0),
            patch("xue.fetch.time.sleep") as sleep,
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

        with patch("xue.fetch._request", side_effect=failure(404)):
            self.assertFalse(remote_exists("https://example.test/missing"))
        with patch("xue.fetch._request", side_effect=failure(503)):
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
            patch("xue.fetch.fetch_text", side_effect=fetch_text),
            patch("xue.fetch.fetch_range", side_effect=fetch_range),
            patch("xue.fetch.ecmwf_field_byte_range", return_value=ByteRange(0, 0)),
        ):
            payload = _download_ecmwf_payload(run, 0, source_spec("ecmwf"))

        self.assertEqual(payload, b"xxxx")
        self.assertEqual(len(requested_indexes), 2)
        self.assertTrue(
            all(
                "ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in url
                for url in requested_ranges
            )
        )

    def test_ecmwf_run_retries_only_the_failed_frame(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 0, tzinfo=UTC))
        attempts: list[int] = []

        def fetch(run: GfsRun, hour: int, destination: Path, **kwargs: object) -> Path:
            attempts.append(hour)
            if hour == 3 and attempts.count(3) == 1:
                raise DownloadError("temporary failure")
            return destination / f"ecmwf.{run.id}.f{hour:03d}.grib2"

        with (
            patch("xue.fetch.fetch_frame", side_effect=fetch),
            patch("xue.fetch.random.uniform", return_value=60),
            patch("xue.fetch.time.sleep") as sleep,
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
