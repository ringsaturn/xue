from __future__ import annotations

import threading
import tempfile
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from xue.fetch import ecmwf_object_url, fetch_frame, fetch_range, floor_to_cycle, object_url, resolve_run
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
            "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/20260815/06z/ifs/0p25/oper/"
            "20260815060000-0h-oper-fc.grib2",
        )
        self.assertTrue(ecmwf_object_url(run, 120).endswith("20260815060000-120h-oper-fc.grib2"))

        now = datetime(2026, 8, 15, 11, 35, tzinfo=UTC)

        def exists(url: str) -> bool:
            return "/20260815/00z/" in url

        resolved = resolve_run("latest", hours=120, now=now, exists=exists, model="ecmwf")
        self.assertEqual(resolved.id, "2026081500")

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
