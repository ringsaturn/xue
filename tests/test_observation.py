"""The NetCDF observation ingest: a local file's bands as Xue frames.

The gdalinfo pass is stubbed, so these run without GDAL and without the
radar dataset — what is under test is how the file's metadata becomes a
frame list, a time axis and a plane-reading recipe.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xue import observation
from xue.errors import ConversionError
from xue.model import PlaneSource
from xue.sources import source_spec

RADAR = source_spec("radar")
EPOCH_2026_08_25_16Z = 1787673600


def band(number: int, time_value: int, **overrides: object) -> dict:
    payload = {
        "band": number,
        "unit": "dBZ",
        "offset": 0.0,
        "scale": 0.1,
        "noDataValue": 32767,
        "metadata": {"": {"NETCDF_DIM_time": str(time_value)}},
    }
    payload.update(overrides)
    return payload


def gdalinfo(hours: list[int], **band_overrides: object) -> str:
    return json.dumps(
        {
            "size": [512, 512],
            "geoTransform": [90.0, 0.087890625, 0.0, 45.0, 0.0, -0.087890625],
            "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
            "bands": [
                band(index + 1, EPOCH_2026_08_25_16Z + hour * 3600, **band_overrides)
                for index, hour in enumerate(hours)
            ],
        }
    )


class InspectObservationTest(unittest.TestCase):
    def _inspect(self, stdout: str, path: Path = Path("radar.nc")) -> observation.ObservationSeries:
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "require_command", return_value="gdalinfo"),
            mock.patch.object(observation, "run_command") as run,
        ):
            run.return_value = mock.Mock(stdout=stdout)
            return observation.inspect_observation(path, RADAR)

    def test_bands_become_frames_keyed_by_lead_time(self) -> None:
        series = self._inspect(gdalinfo([0, 1, 2]))
        self.assertEqual(series.lead_seconds, [0, 3600, 7200])
        self.assertEqual([frames["cref"].band for frames in series.frames], [1, 2, 3])
        first = series.frames[0]["cref"]
        # The first observation is the series' run time; hours count from it.
        self.assertEqual(first.run_time, datetime(2026, 8, 25, 16, tzinfo=UTC))
        self.assertEqual(series.frames[2]["cref"].valid_time, datetime(2026, 8, 25, 18, tzinfo=UTC))
        self.assertEqual(str(first.path), 'NETCDF:"radar.nc":cref')

    def test_a_missed_publication_is_a_gap_in_the_axis(self) -> None:
        """Observation series are not validated against a cadence: a slot the
        archive never published is simply absent."""
        self.assertEqual(
            self._inspect(gdalinfo([0, 1, 4, 5])).lead_seconds, [0, 3600, 14400, 18000]
        )

    def test_packing_and_fill_are_read_off_the_bands(self) -> None:
        plane_source = self._inspect(gdalinfo([0, 1])).plane_source
        self.assertTrue(plane_source.unscale)
        # GDAL may hand the fill back raw or scaled; both mean missing.
        self.assertEqual(plane_source.fill_values, (32767.0, 3276.7000000000003))
        # Missing becomes the bottom of the cref codebook, which a renderer
        # paints as nothing.
        self.assertEqual(plane_source.fill_replacement, 0.0)

    def test_rejects_files_it_cannot_read_as_a_series(self) -> None:
        for stdout, reason in (
            (gdalinfo([0, 1], unit="mm"), "wrong unit"),
            (json.dumps({"bands": [], "metadata": {"": {"time#units": "seconds since 1970-01-01"}}}), "no bands"),
            (json.dumps({"bands": [band(1, 0)], "metadata": {"": {}}}), "no time units"),
        ):
            with self.assertRaises(ConversionError, msg=reason):
                self._inspect(stdout)

    def test_a_sub_hour_cadence_is_kept_exactly(self) -> None:
        """The radar mosaic publishes every six minutes; the axis carries that
        rather than rounding it to an hour."""
        six_minute = json.dumps(
            {
                "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
                "bands": [
                    band(index + 1, EPOCH_2026_08_25_16Z + index * 360) for index in range(4)
                ],
            }
        )
        self.assertEqual(self._inspect(six_minute).lead_seconds, [0, 360, 720, 1080])

    def test_rejects_a_time_axis_off_the_second(self) -> None:
        sub_second = json.dumps(
            {
                "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
                "bands": [band(1, EPOCH_2026_08_25_16Z), band(2, EPOCH_2026_08_25_16Z)],
            }
        )
        # Two frames at the same instant are not a strictly increasing axis.
        with self.assertRaises(ConversionError):
            self._inspect(sub_second)

    def test_rejects_a_non_netcdf_input(self) -> None:
        with mock.patch.object(Path, "is_file", return_value=True):
            with self.assertRaises(ConversionError):
                observation.inspect_observation(Path("radar.grib2"), RADAR)

    def test_rejects_a_forecast_source(self) -> None:
        with self.assertRaises(ConversionError):
            observation.inspect_observation(Path("radar.nc"), source_spec("gfs"))

    def test_other_time_units_are_understood(self) -> None:
        payload = json.loads(gdalinfo([0, 1]))
        payload["metadata"][""]["time#units"] = "hours since 2026-08-25 16:00:00"
        for index, band_payload in enumerate(payload["bands"]):
            band_payload["metadata"][""]["NETCDF_DIM_time"] = str(index)
        series = self._inspect(json.dumps(payload))
        self.assertEqual(series.lead_seconds, [0, 3600])
        self.assertEqual(series.frames[0]["cref"].run_time, datetime(2026, 8, 25, 16, tzinfo=UTC))


class PlaneSourceTest(unittest.TestCase):
    def test_fill_becomes_the_replacement_value(self) -> None:
        plane_source = PlaneSource(unscale=True, fill_values=(32767.0, 3276.7), fill_replacement=0.0)
        values = np.array([0.0, 12.5, 32767.0, 3276.7, 55.0])
        np.testing.assert_allclose(plane_source.apply_fill(values), [0.0, 12.5, 0.0, 0.0, 55.0])

    def test_a_grib_plane_source_touches_nothing(self) -> None:
        values = np.array([32767.0, 1.0])
        np.testing.assert_allclose(PlaneSource().apply_fill(values), values)


if __name__ == "__main__":
    unittest.main()
