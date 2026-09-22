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
        # 100 m wind pair, the surface diagnostics (the four categorical
        # precipitation-type flags among them, fetched only to derive the
        # precipitation type) and the vertical velocity, then the ocean —
        # three pgrb2 records and, appended from the cycle's GFS-Wave file,
        # three wave records.
        self.assertEqual(len(messages), 73)
        temperature, precipitation, u_wind, v_wind, pressure = messages[:5]
        heights = messages[5:13]
        upper_air = messages[13:46]
        wind_100m = messages[46:48]
        diagnostics = messages[48:67]
        ocean = messages[67:]
        levels = [100000.0, 92500.0, 85000.0, 70000.0, 50000.0, 30000.0, 25000.0, 20000.0]
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
        # The eight published isobaric surfaces, whose value is the level in
        # pascals — the same number variables.py declares for each level.
        self.assertEqual(
            [(message.band, message.level_type, message.level_value) for message in heights],
            [(6 + index, 100, level) for index, level in enumerate(levels)],
        )
        for height in heights:
            self.assertEqual(
                (height.discipline, height.parameter_category, height.parameter_number),
                (0, 3, 5),
            )
        # Temperature and relative humidity on the eight surfaces, the 850 hPa
        # specific humidity (an input only), then the wind pair on each.
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_value) for m in upper_air],
            [(14 + index, 0, 0, level) for index, level in enumerate(levels)]
            + [(22 + index, 1, 1, level) for index, level in enumerate(levels)]
            + [(30, 1, 0, 85000.0)]
            + [
                (31 + 2 * index + component, 2, 2 + component, level)
                for index, level in enumerate(levels)
                for component in (0, 1)
            ],
        )
        self.assertTrue(all(message.level_type == 100 for message in upper_air))
        # The 100 m wind pair, the turbine hub height: the 10 m pair's own
        # parameters on the 100 m surface.
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_type, m.level_value) for m in wind_100m],
            [(47, 2, 2, 103, 100.0), (48, 2, 3, 103, 100.0)],
        )
        # The surface diagnostics, each on the surface its registry entry
        # declares: the ground, the entire atmosphere, the three cloud
        # layers, the 2 m surface, NCEP's single-layer entire-atmosphere
        # type 200 the precipitable water sits on — and every one the
        # instantaneous record, not the interval average that sits beside
        # the cloud covers.
        self.assertEqual(
            [(m.band, m.parameter_category, m.parameter_number, m.level_type, m.level_value) for m in diagnostics],
            [
                (49, 2, 22, 1, 0.0),
                (50, 6, 1, 10, 0.0),
                (51, 6, 3, 214, 0.0),
                (52, 6, 4, 224, 0.0),
                (53, 6, 5, 234, 0.0),
                (54, 7, 6, 1, 0.0),
                (55, 7, 7, 1, 0.0),
                (56, 19, 0, 1, 0.0),
                (57, 0, 6, 103, 2.0),
                (58, 0, 21, 103, 2.0),
                (59, 1, 3, 200, 0.0),
                (60, 3, 196, 1, 0.0),
                (61, 1, 192, 1, 0.0),
                (62, 1, 193, 1, 0.0),
                (63, 1, 194, 1, 0.0),
                (64, 1, 195, 1, 0.0),
                (65, 2, 8, 100, 85000.0),
                (66, 2, 8, 100, 70000.0),
                (67, 2, 8, 100, 50000.0),
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
                (68, 0, 0, 0, 1, 0.0),
                (69, 10, 2, 0, 1, 0.0),
                (70, 10, 2, 1, 1, 0.0),
                (71, 10, 0, 3, 1, 1.0),
                (72, 10, 0, 11, 1, 1.0),
                (73, 10, 0, 10, 1, 1.0),
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
