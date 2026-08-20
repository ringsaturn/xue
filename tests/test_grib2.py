"""GRIB2 header index tests: the fast band discovery must agree with GDAL."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xue import grib2
from xue.errors import ConversionError
from xue.gdal import inspect_grib_multi, raster_expression
from xue.variables import variable_spec

FIXTURE = Path(__file__).parent / "fixtures" / "gfs.2026081406.f000.crop.grib2"


class IndexMessagesTests(unittest.TestCase):
    def test_fixture_identities(self) -> None:
        messages = grib2.index_messages(FIXTURE)
        self.assertEqual(len(messages), 2)
        temperature, precipitation = messages
        self.assertEqual(temperature.band, 1)
        self.assertEqual(
            (temperature.discipline, temperature.parameter_category, temperature.parameter_number),
            (0, 0, 0),
        )
        self.assertEqual((temperature.level_type, temperature.level_value), (103, 2.0))
        self.assertIsNone(temperature.statistical_process)
        self.assertEqual(temperature.reference_time, datetime(2026, 8, 14, 6, tzinfo=UTC))
        self.assertEqual(temperature.valid_time, temperature.reference_time)
        self.assertEqual(
            (precipitation.band, precipitation.parameter_category, precipitation.parameter_number),
            (2, 1, 7),
        )
        self.assertEqual((precipitation.level_type, precipitation.level_value), (1, 0.0))

    def test_rejects_non_grib_payload(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".grib2") as handle:
            handle.write(b"not a grib file at all, definitely long enough")
            handle.flush()
            with self.assertRaises(ConversionError):
                grib2.index_messages(Path(handle.name))

    def test_rejects_truncated_message(self) -> None:
        payload = FIXTURE.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".grib2") as handle:
            handle.write(payload[:1000])  # ends inside the first message
            handle.flush()
            with self.assertRaises(ConversionError):
                grib2.index_messages(Path(handle.name))


class InspectGribFastTests(unittest.TestCase):
    def test_matches_gdalinfo_on_fixture(self) -> None:
        variable_ids = ("tmp2m", "prate")
        fast = grib2.inspect_grib_fast(FIXTURE, variable_ids)
        reference = inspect_grib_multi(FIXTURE, variable_ids)
        self.assertEqual(set(fast), set(reference))
        for variable_id in variable_ids:
            fast_frame, reference_frame = fast[variable_id], reference[variable_id]
            self.assertEqual(fast_frame.band, reference_frame.band)
            self.assertEqual(fast_frame.run_time, reference_frame.run_time)
            self.assertEqual(fast_frame.valid_time, reference_frame.valid_time)
            self.assertEqual(fast_frame.forecast_hour, reference_frame.forecast_hour)
            self.assertEqual(
                raster_expression(variable_id, fast_frame.unit),
                raster_expression(variable_id, reference_frame.unit),
            )

    def test_missing_variable_raises_unless_optional(self) -> None:
        with self.assertRaises(ConversionError):
            grib2.inspect_grib_fast(FIXTURE, ("tmp2m", "dswrf"))
        frames = grib2.inspect_grib_fast(FIXTURE, ("tmp2m", "dswrf"), optional_ids=("dswrf",))
        self.assertEqual(set(frames), {"tmp2m"})


class MatcherTests(unittest.TestCase):
    def _message(self, **overrides) -> grib2.MessageInfo:
        base = dict(
            band=1,
            discipline=0,
            parameter_category=1,
            parameter_number=7,
            level_type=1,
            level_value=0.0,
            reference_time=datetime(2026, 8, 19, 6, tzinfo=UTC),
            valid_time=datetime(2026, 8, 19, 7, tzinfo=UTC),
            statistical_process=None,
        )
        base.update(overrides)
        return grib2.MessageInfo(**base)

    def test_instantaneous_rate_rejects_averaged_record(self) -> None:
        averaged = self._message(statistical_process=0)
        self.assertFalse(grib2._matches(variable_spec("prate"), averaged))
        self.assertTrue(grib2._matches(variable_spec("prate_ave"), averaged))
        self.assertTrue(grib2._matches(variable_spec("prate"), self._message()))
        self.assertFalse(grib2._matches(variable_spec("prate_ave"), self._message()))

    def test_missing_level_value_accepted_only_when_spec_allows(self) -> None:
        accumulation = self._message(parameter_number=193, level_value=None, statistical_process=1)
        self.assertTrue(grib2._matches(variable_spec("tp"), accumulation))
        self.assertFalse(grib2._matches(variable_spec("prate"), self._message(level_value=None)))


if __name__ == "__main__":
    unittest.main()
