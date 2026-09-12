"""GRIB2 header index tests: the fast band discovery must agree with GDAL."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xuebuild import grib2
from xuebuild.errors import ConversionError
from xuebuild.gdal import inspect_grib_multi, raster_expression
from xuebuild.variables import variable_spec

FIXTURE = Path(__file__).parent / "fixtures" / "gfs.2026081406.f000.crop.grib2"


class IndexMessagesTests(unittest.TestCase):
    def test_fixture_identities(self) -> None:
        messages = grib2.index_messages(FIXTURE)
        # Every record the GFS source fetches, in the order the fetcher assembles
        # them: the surface fields, the pressure family, the upper-air inputs
        # (the 850 hPa specific humidity among them, fetched only to derive
        # the vapour flux and the equivalent potential temperature), then the
        # surface diagnostics and the vertical velocity, then the ocean —
        # three pgrb2 records and, appended from the cycle's GFS-Wave file,
        # three wave records.
        self.assertEqual(len(messages), 40)
        temperature, precipitation, u_wind, v_wind, pressure = messages[:5]
        heights = messages[5:9]
        upper_air = messages[9:22]
        diagnostics = messages[22:34]
        ocean = messages[34:]
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
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_type, m.level_value) for m in (u_wind, v_wind)],
            [(3, 2, 2, 103, 10.0), (4, 2, 3, 103, 10.0)],
        )
        # Mean sea level pressure: the surface (101) carries no value of its
        # own, which GRIB2 writes as a zero the registry declares as None.
        self.assertEqual(
            (pressure.band, pressure.parameter_category, pressure.parameter_number),
            (5, 3, 1),
        )
        self.assertEqual((pressure.level_type, pressure.level_value), (101, 0.0))
        # The four published isobaric surfaces, whose value is the level in
        # pascals — the same number variables.py declares for each level.
        self.assertEqual(
            [(message.band, message.level_type, message.level_value) for message in heights],
            [(6, 100, 85000.0), (7, 100, 70000.0), (8, 100, 50000.0), (9, 100, 25000.0)],
        )
        for height in heights:
            self.assertEqual(
                (height.discipline, height.parameter_category, height.parameter_number),
                (0, 3, 5),
            )
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_value) for m in upper_air],
            [
                (10, 0, 0, 92500.0),
                (11, 0, 0, 85000.0),
                (12, 0, 0, 50000.0),
                (13, 1, 1, 85000.0),
                (14, 1, 1, 70000.0),
                (15, 1, 1, 50000.0),
                (16, 1, 0, 85000.0),
                (17, 2, 2, 92500.0),
                (18, 2, 3, 92500.0),
                (19, 2, 2, 85000.0),
                (20, 2, 3, 85000.0),
                (21, 2, 2, 25000.0),
                (22, 2, 3, 25000.0),
            ],
        )
        self.assertTrue(all(message.level_type == 100 for message in upper_air))
        # The surface diagnostics, each on the surface its registry entry
        # declares: the ground, the entire atmosphere, the three cloud
        # layers, the 2 m surface — and every one the instantaneous record,
        # not the interval average that sits beside the cloud covers.
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_type, m.level_value) for m in diagnostics],
            [
                (23, 2, 22, 1, 0.0),
                (24, 6, 1, 10, 0.0),
                (25, 6, 3, 214, 0.0),
                (26, 6, 4, 224, 0.0),
                (27, 6, 5, 234, 0.0),
                (28, 7, 6, 1, 0.0),
                (29, 19, 0, 1, 0.0),
                (30, 0, 6, 103, 2.0),
                (31, 0, 21, 103, 2.0),
                (32, 2, 8, 100, 85000.0),
                (33, 2, 8, 100, 70000.0),
                (34, 2, 8, 100, 50000.0),
            ],
        )
        self.assertTrue(all(message.statistical_process is None for message in diagnostics))
        # The ocean fields: the skin temperature and the two sea ice fields
        # on the ground surface (value 0), then the wave fields on the water
        # surface as WAVEWATCH III writes it — value 1, which the registry
        # declares as None and so accepts. The ice and wave fields are the
        # first records of GRIB2's oceanographic discipline in the set.
        self.assertEqual(
            [(m.band, m.discipline, m.parameter_category, m.parameter_number, m.level_type, m.level_value) for m in ocean],
            [
                (35, 0, 0, 0, 1, 0.0),
                (36, 10, 2, 0, 1, 0.0),
                (37, 10, 2, 1, 1, 0.0),
                (38, 10, 0, 3, 1, 1.0),
                (39, 10, 0, 11, 1, 1.0),
                (40, 10, 0, 10, 1, 1.0),
            ],
        )
        self.assertTrue(all(message.statistical_process is None for message in ocean))

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
            self.assertEqual(fast_frame.lead_seconds, reference_frame.lead_seconds)
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

    def test_alias_triple_matches_on_the_same_surface_only(self) -> None:
        # ECMWF msl: plain pressure (0/3/0) on the mean sea level surface is
        # the registry's PRMSL (0/3/1); the same triple at the ground is the
        # surface pressure, a different field.
        prmsl = variable_spec("prmsl")
        self.assertTrue(grib2._matches(prmsl, self._message(parameter_category=3, parameter_number=1, level_type=101)))
        self.assertTrue(grib2._matches(prmsl, self._message(parameter_category=3, parameter_number=0, level_type=101)))
        self.assertFalse(grib2._matches(prmsl, self._message(parameter_category=3, parameter_number=0, level_type=1)))
        self.assertFalse(grib2._matches(prmsl, self._message(parameter_category=3, parameter_number=192, level_type=101)))


if __name__ == "__main__":
    unittest.main()
