"""The tropical cyclone product (``xuebuild/tc``): each parser on the
fetched fixture and on constructed edge cases, the identity rules on
scenarios built to trip them, the validators on malformed input, and the
whole build held to a committed golden.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xuebuild.errors import TcProductError
from xuebuild.tc import atcf, bufrtracks, ibtracs, tcw
from xuebuild.tc.build import build_product, previous_systems
from xuebuild.tc.fetch import ecmwf_tf_url
from xuebuild.tc.identity import MATCH_KM, PreviousSystem, Sighting, distance_km, resolve, sighting_kind
from xuebuild.tc.registry import AGENCIES, MODELS, registry_document
from xuebuild.tc.schema import (
    build_pointer,
    crc32_hex,
    encode_json,
    parse_issue,
    validate_index,
    validate_pointer,
    validate_storm,
)
from xuebuild.tc.track import MISSING, wrap_longitude

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TC_FIXTURES = FIXTURES / "tc"
RAW = TC_FIXTURES / "tc.2026091206"
EXPECTED = TC_FIXTURES / "expected"
ISSUE = parse_issue("2026091206")
HAS_BUFR_DUMP = shutil.which("bufr_dump") is not None


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


class RegistryTests(unittest.TestCase):
    def test_registry_matches_the_pinned_fixture(self) -> None:
        pinned = json.loads((FIXTURES / "tc-registry.json").read_text(encoding="utf-8"))
        self.assertEqual(registry_document(), pinned)

    def test_ids_are_product_keys(self) -> None:
        for key in (*AGENCIES, *MODELS):
            self.assertRegex(key, r"^[a-z][a-z0-9]*$")
        self.assertEqual(len(MODELS["gefs"].techs), 31)


class AtcfTests(unittest.TestCase):
    def test_line_fields_and_units(self) -> None:
        record = atcf.parse_line(
            "EP, 14, 2026091200, 03, AVNO, 006, 170N, 1273W,  41,  998, XX,  34, NEQ, 0077, 0000, 0030, 0066, 1010,  294,  33"
        )
        assert record is not None
        self.assertEqual(record.storm_id, "EP142026")
        self.assertEqual((record.lat, record.lon), (17.0, -127.3))
        self.assertEqual((record.vmax_kt, record.mslp, record.tau), (41, 998, 6))
        self.assertIsNone(record.cls)  # XX is "no class"
        self.assertEqual(record.radii_nm, (77, 0, 30, 66))
        self.assertEqual(record.rmw_nm, 33)
        self.assertTrue(record.numbered)
        self.assertIsNone(atcf.parse_line("# comment"))
        self.assertIsNone(atcf.parse_line("EP, 14, 2026091200"))

    def test_missing_values_and_symmetric_radii(self) -> None:
        record = atcf.parse_line(
            "WP, 98, 2026091200, 03, AVNO, 000, 157N, 1096E,  33, 1007, XX,  34, AAA, 0040, 0000, 0000, 0000,  -99,  -99,  69"
        )
        assert record is not None
        self.assertEqual(record.storm_id, "WP982026")
        self.assertTrue(record.invest)
        self.assertEqual(record.radii_nm, (40, 40, 40, 40))
        self.assertEqual(record.lon, 109.6)
        zero = atcf.parse_line(
            "EP, 14, 2026091218, 03, OFCL,  12, 180N, 1311W,  45,    0, TS,  34, NEQ,   80,   20,   30,   80"
        )
        assert zero is not None
        self.assertIsNone(zero.mslp)
        self.assertEqual(zero.cls, "TS")

    def test_forecast_merges_radii_lines_and_takes_the_newest_base(self) -> None:
        records = atcf.parse_atcf((RAW / "nhc" / "aep142026.dat").read_text())
        official = atcf.forecasts(records, "OFCL")["EP142026"]
        self.assertEqual(official.base, utc(2026, 9, 12, 0))
        leads = [int((point.time - official.base).total_seconds()) // 3600 for point in official.points]
        self.assertEqual(leads, [0, 3, 12, 24, 36, 48, 60, 72, 96, 120])
        first = official.points[1]
        self.assertEqual(first.cls, "TS")
        self.assertEqual(first.vmax, 28.3)  # 55 kt
        self.assertEqual(first.gust, 33.4)  # 65 kt
        self.assertEqual(first.radii, {"34": [166.7, 74.1, 55.6, 92.6], "50": [55.6, 0.0, 0.0, 37.0]})
        self.assertNotIn("64", first.radii or {})

    def test_best_track_and_name(self) -> None:
        records = atcf.parse_atcf((RAW / "nhc" / "bep142026.dat").read_text())
        track = atcf.best_track(records, "EP142026")
        assert track is not None
        self.assertEqual(atcf.storm_name(records, "EP142026"), "NORBERT")
        times = [point.time for point in track.points]
        self.assertEqual(times, sorted(times))
        self.assertEqual(track.points[-1].time, utc(2026, 9, 12, 0))
        self.assertEqual(track.points[-1].rmw, 46.3)  # 25 nm
        self.assertEqual(track.points[-1].radii, {"34": [166.7, 74.1, 55.6, 92.6], "50": [55.6, 0.0, 0.0, 37.0]})
        self.assertEqual(track.points[0].cls, "DB")

    def test_tracker_output_lists_invests_beside_numbered_systems(self) -> None:
        records = atcf.parse_atcf((RAW / "gfs" / "2026091200" / "avno.t00z.cyclone.trackatcfunix").read_text())
        self.assertEqual(atcf.storm_ids(records), ["EP972026", "WP982026", "EP142026"])
        forecasts = atcf.forecasts(records, "AVNO")
        self.assertEqual(forecasts["EP142026"].run, "2026091200")
        self.assertEqual(len(forecasts["EP142026"].points), 35)


class TcwTests(unittest.TestCase):
    def test_warning(self) -> None:
        product = tcw.parse_tcw((RAW / "jtwc" / "ep1426.tcw").read_text())
        self.assertEqual(
            (product.kind, product.short_id, product.name, product.number), ("warning", "14E", "NORBERT", "010")
        )
        self.assertEqual(product.base, utc(2026, 9, 12, 0))
        self.assertEqual(product.issued, utc(2026, 9, 12, 3, 20, 55))
        assert product.forecast is not None
        leads = [int((p.time - product.base).total_seconds()) // 3600 for p in product.forecast.points]
        self.assertEqual(leads, [0, 12, 24, 36, 48, 60, 72, 96, 120])
        day_one = product.forecast.points[2]
        self.assertEqual(day_one.vmax, 33.4)  # 65 kt
        self.assertEqual(
            day_one.radii, {"64": [27.8, 0.0, 0.0, 0.0], "50": [55.6, 0.0, 0.0, 37.0], "34": [166.7, 74.1, 55.6, 92.6]}
        )
        # Reissued warnings repeat history rows; each time appears once.
        times = [p.time for p in product.history.points]
        self.assertEqual(len(times), len(set(times)))
        self.assertEqual(product.history.points[0].time, utc(2026, 9, 7, 18))
        self.assertEqual(product.history.points[-1].vmax, 28.3)
        self.assertTrue(product.history.provisional)

    def test_formation_alert(self) -> None:
        product = tcw.parse_tcw((RAW / "jtwc" / "ep9726.tcw").read_text())
        self.assertEqual((product.kind, product.short_id), ("alert", "97X"))
        self.assertIsNone(product.forecast)
        self.assertEqual(product.base, utc(2026, 9, 11, 18))
        self.assertEqual(product.alert_line, ((12.8, -112.7), (15.4, -117.7)))
        self.assertEqual(product.alert_half_width, 268.5)  # 145 nm
        self.assertEqual(product.alert_center, (12.9, -112.8))
        self.assertEqual(len(product.history.points), 6)

    def test_rejects_other_text(self) -> None:
        with self.assertRaises(ValueError):
            tcw.parse_tcw("ABPW10 PGTW 120600\nMSGID/GENADMIN\n")


def _entry(key: str, value: object, index: int | None = None) -> dict[str, object]:
    entry: dict[str, object] = {"key": key, "value": value}
    if index is not None:
        entry["index"] = index
    return entry


# One message, two subsets (members 1 and 2): the header, the analysis
# block, then two periods; member 2 loses the system at the second. A
# scalar is shared by the subsets, a pair is one value per subset.
_SYNTHETIC_ROWS: list[tuple[str, object]] = [
    ("centre", 98),
    ("stormIdentifier", "14E"),
    ("longStormName", "   NORBERT"),
    ("ensembleMemberNumber", [1, 2]),
    ("ensembleForecastType", [0, 4]),
    ("year", 2026),
    ("month", 9),
    ("day", 12),
    ("hour", 0),
    ("minute", 0),
    ("meteorologicalAttributeSignificance", 1),
    ("latitude", 17.1),
    ("longitude", -126.1),
    ("meteorologicalAttributeSignificance", [4, 4]),
    ("latitude", [16.8, 16.9]),
    ("longitude", [-126.0, -126.2]),
    ("pressureReducedToMeanSeaLevel", [99700, 99800]),
    ("meteorologicalAttributeSignificance", 3),
    ("latitude", [17.7, 17.6]),
    ("longitude", [-125.7, -125.8]),
    ("windSpeedAt10M", [18.5, 17.0]),
    ("timePeriod", 6),
    ("meteorologicalAttributeSignificance", 1),
    ("latitude", [16.6, 16.7]),
    ("longitude", [-127.1, 233.0]),
    ("pressureReducedToMeanSeaLevel", [100000, 100100]),
    ("meteorologicalAttributeSignificance", 3),
    ("latitude", [17.5, 17.4]),
    ("longitude", [-126.8, -126.9]),
    ("windSpeedAt10M", [17.5, 16.0]),
    ("windSpeedThreshold", 18),
    ("bearingOrAzimuth", 0),
    ("bearingOrAzimuth", 90),
    ("effectiveRadiusWithRespectToWindSpeedsAboveThreshold", [103700, 0]),
    ("bearingOrAzimuth", 90),
    ("bearingOrAzimuth", 180),
    ("effectiveRadiusWithRespectToWindSpeedsAboveThreshold", [0, 0]),
    ("bearingOrAzimuth", 180),
    ("bearingOrAzimuth", 270),
    ("effectiveRadiusWithRespectToWindSpeedsAboveThreshold", [0, 0]),
    ("bearingOrAzimuth", 270),
    ("bearingOrAzimuth", 0),
    ("effectiveRadiusWithRespectToWindSpeedsAboveThreshold", [42600, None]),
    ("timePeriod", 12),
    ("meteorologicalAttributeSignificance", 1),
    ("latitude", [17.1, None]),
    ("longitude", [-128.0, None]),
    ("pressureReducedToMeanSeaLevel", [99900, None]),
    ("meteorologicalAttributeSignificance", 3),
    ("latitude", [17.8, None]),
    ("longitude", [-127.5, None]),
    ("windSpeedAt10M", [19.0, None]),
]


def _synthetic_dump(compressed: bool) -> dict[str, object]:
    """The rows above as ``bufr_dump -j f`` would print them: one message
    with per-subset arrays when compressed, the two subsets one after the
    other behind ``subsetNumber`` markers when not."""
    if compressed:
        return {"messages": [_entry(key, value, index + 1) for index, (key, value) in enumerate(_SYNTHETIC_ROWS)]}
    entries: list[dict[str, object]] = []
    for subset in range(2):
        entries.append(_entry("subsetNumber", subset + 1))
        for index, (key, value) in enumerate(_SYNTHETIC_ROWS):
            entries.append(_entry(key, value[subset] if isinstance(value, list) else value, index + 1))
    return {"messages": entries}


class BufrTracksTests(unittest.TestCase):
    def check(self, dump: dict[str, object]) -> None:
        storms = bufrtracks.parse_bufr_tracks(dump)
        self.assertEqual(len(storms), 1)
        storm = storms[0]
        self.assertEqual((storm.identifier, storm.name, storm.base), ("14E", "NORBERT", utc(2026, 9, 12, 0)))
        self.assertEqual([m.member for m in storm.members], [1, 2])
        self.assertEqual([m.kind for m in storm.members], [0, 4])
        control = storm.members[0].forecast
        self.assertEqual(len(control.points), 3)
        # t=0 is the model's analysed centre (significance 4), not the
        # observed one (significance 1).
        self.assertEqual((control.points[0].lat, control.points[0].lon, control.points[0].pmin), (16.8, -126.0, 997.0))
        self.assertEqual(control.points[0].vmax, 18.5)
        six = control.points[1]
        self.assertEqual(six.time, utc(2026, 9, 12, 6))
        self.assertEqual(six.radii, {"34": [103.7, 0.0, 0.0, 42.6]})
        member = storm.members[1].forecast
        self.assertEqual(len(member.points), 2)  # lost at +12
        self.assertEqual(member.points[1].lon, -127.0)  # 233 E wrapped
        # A zero radius is a real "no such wind"; only a fully missing
        # threshold is dropped.
        self.assertEqual(member.points[1].radii, {"34": [0.0, 0.0, 0.0, None]})

    def test_compressed_message(self) -> None:
        self.check(_synthetic_dump(compressed=True))

    def test_uncompressed_message(self) -> None:
        self.check(_synthetic_dump(compressed=False))

    @unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
    def test_fixture_files(self) -> None:
        from xuebuild.eccodescli import bufr_dump_json

        oper = bufrtracks.parse_bufr_tracks(bufr_dump_json(RAW / "ecmwf" / "2026091200-oper-tf.bufr"))
        self.assertEqual([s.identifier for s in oper], ["14E", "70W"])
        self.assertEqual(len(oper[0].members), 1)
        self.assertEqual(len(oper[0].members[0].forecast.points), 25)  # analysis + 24 six-hourly periods
        ens = bufrtracks.parse_bufr_tracks(bufr_dump_json(RAW / "ecmwfens" / "2026091100-enfo-tf.bufr"))
        self.assertEqual(len(ens), 1)
        self.assertEqual(sorted(m.member for m in ens[0].members), list(range(1, 52)))
        self.assertEqual(ens[0].base, utc(2026, 9, 11, 0))

    def test_tf_url_horizon_follows_the_cycle(self) -> None:
        self.assertTrue(ecmwf_tf_url(utc(2026, 9, 12, 0), "oper").endswith("/20260912000000-360h-oper-tf.bufr"))
        self.assertTrue(
            ecmwf_tf_url(utc(2026, 9, 12, 6), "enfo").endswith("/06z/ifs/0p25/enfo/20260912060000-144h-enfo-tf.bufr")
        )


IBTRACS_HEADER = (
    "SID,SEASON,NUMBER,BASIN,SUBBASIN,NAME,ISO_TIME,NATURE,LAT,LON,WMO_WIND,WMO_PRES,WMO_AGENCY,TRACK_TYPE,"
    "USA_AGENCY,USA_ATCF_ID,USA_LAT,USA_LON,USA_STATUS,USA_WIND,USA_PRES,USA_R34_NE,USA_R34_SE,USA_R34_SW,USA_R34_NW,"
    "USA_RMW,TOKYO_LAT,TOKYO_LON,TOKYO_GRADE,TOKYO_WIND,TOKYO_PRES,CMA_LAT,CMA_LON,CMA_CAT,CMA_WIND,CMA_PRES\n"
    " ,Year, , , , , , ,degrees_north,degrees_east,kts,mb, , , , ,degrees_north,degrees_east, ,kts,mb,nmile,nmile,nmile,nmile,"
    "nmile,degrees_north,degrees_east,1,kts,mb,degrees_north,degrees_east,1,kts,mb\n"
)


class IbtracsTests(unittest.TestCase):
    def test_agency_columns_become_tracks(self) -> None:
        text = IBTRACS_HEADER + (
            "2026244N23132,2026,60,WP,MM,KROVANH,2026-09-01 00:00:00,TS,23.0,132.0,35,996,tokyo,PROVISIONAL,"
            "jtwc_wp,WP222026,23.0,132.1,TS,40,995,60,60,40,40,30,23.1,132.0,3,35,996,23.0,132.0,2,18,996\n"
            "2026244N23132,2026,60,WP,MM,KROVANH,2026-09-01 06:00:00,TS,23.5,131.0,40,992,tokyo,PROVISIONAL,"
            "jtwc_wp,WP222026,23.5,131.1,TS,45,990, , , , ,25,23.6,131.0,3,40,992, , , , , \n"
        )
        storms = ibtracs.parse_ibtracs(text)
        storm = storms["2026244N23132"]
        self.assertEqual(
            (storm.name, storm.basin, storm.atcf_id, storm.provisional), ("KROVANH", "WP", "WP222026", True)
        )
        self.assertEqual(sorted(storm.agencies), ["cma", "jma", "usa"])
        usa = storm.agencies["usa"]
        self.assertEqual(len(usa.points), 2)
        self.assertEqual(usa.points[0].vmax, 20.6)  # 40 kt
        self.assertEqual(usa.points[0].radii, {"34": [111.1, 111.1, 74.1, 74.1]})
        self.assertEqual(usa.points[0].rmw, 55.6)
        self.assertIsNone(usa.points[1].radii)
        jma = storm.agencies["jma"]
        self.assertEqual(jma.points[1].cls, "3")
        self.assertEqual(jma.points[1].vmax, 20.6)
        self.assertEqual(len(storm.agencies["cma"].points), 1)
        self.assertEqual(ibtracs.by_atcf_id(storms)["WP222026"].sid, "2026244N23132")

    def test_fixture(self) -> None:
        storms = ibtracs.parse_ibtracs((RAW / "ibtracs" / "ibtracs.ACTIVE.list.v04r01.csv").read_text())
        self.assertEqual(sorted(storms), ["2026248N22138", "2026251N13251"])
        norbert = storms["2026251N13251"]
        self.assertEqual(norbert.atcf_id, "EP142026")
        self.assertIsNone(norbert.name)  # UNNAMED at the time
        self.assertEqual(len(norbert.agencies["usa"].points), 19)


def _sighting(source: str, key: str, basin: str, time: datetime, lat: float, lon: float, **extra: object) -> Sighting:
    return Sighting(source=source, key=key, kind=sighting_kind(key), basin=basin, time=time, lat=lat, lon=lon, **extra)  # type: ignore[arg-type]


def _previous(
    system_id: str, level: str, basin: str, aliases: dict[str, dict[str, str]], time: datetime, lat: float, lon: float
) -> PreviousSystem:
    return PreviousSystem(
        id=system_id, level=level, basin=basin, aliases=aliases, last_time=time, last_lat=lat, last_lon=lon
    )


class IdentityTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(sighting_kind("EP142026"), "numbered")
        self.assertEqual(sighting_kind("EP97"), "invest")
        self.assertEqual(sighting_kind("70W"), "unnumbered")
        self.assertEqual(sighting_kind("EP55"), "unnumbered")  # test / internal numbers never publish

    def test_numbered_id_is_the_key_across_sources(self) -> None:
        now = utc(2026, 9, 12, 6)
        resolution = resolve(
            [
                _sighting("nhc", "EP142026", "EP", now, 17.0, -126.0, name="NORBERT"),
                _sighting("gfs", "EP142026", "EP", now, 17.2, -126.4),
            ],
            [],
            now,
        )
        self.assertEqual(
            [(s.id, s.level, s.name, len(s.sightings)) for s in resolution.systems], [("EP142026", "A", "NORBERT", 2)]
        )

    def test_invest_gets_a_synthetic_id_that_the_previous_index_carries(self) -> None:
        now = utc(2026, 9, 12, 6)
        first = resolve([_sighting("gfs", "EP97", "EP", now, 14.0, -112.6, first_seen=utc(2026, 9, 10, 12))], [], now)
        self.assertEqual([(s.id, s.level) for s in first.systems], [("x-ep-2026091012-1", "B")])
        self.assertEqual(
            first.systems[0].aliases, {"EP97": {"from": "2026-09-12T06:00:00Z", "to": "2026-09-12T06:00:00Z"}}
        )
        later = now + timedelta(hours=1)
        previous = [_previous("x-ep-2026091012-1", "B", "EP", first.systems[0].aliases, now, 14.0, -112.6)]
        second = resolve([_sighting("jtwc", "EP97", "EP", later, 14.4, -113.0)], previous, later)
        self.assertEqual(second.systems[0].id, "x-ep-2026091012-1")
        self.assertEqual(second.systems[0].aliases["EP97"]["to"], "2026-09-12T07:00:00Z")
        # The same number three days on is another system.
        much_later = now + timedelta(days=3)
        third = resolve([_sighting("jtwc", "EP97", "EP", much_later, 10.0, -100.0)], previous, much_later)
        self.assertEqual(third.systems[0].id, "x-ep-2026091506-1")

    def test_invest_merges_into_the_storm_it_became(self) -> None:
        now = utc(2026, 9, 12, 6)
        previous = [
            _previous(
                "x-ep-2026091012-1",
                "B",
                "EP",
                {"EP97": {"from": "2026-09-11T18:00:00Z", "to": "2026-09-12T00:00:00Z"}},
                utc(2026, 9, 12, 0),
                14.0,
                -112.6,
            )
        ]
        resolution = resolve([_sighting("nhc", "EP152026", "EP", now, 14.6, -113.4)], previous, now)
        self.assertEqual(resolution.crosswalk, {"x-ep-2026091012-1": "EP152026"})
        self.assertIn("EP97", resolution.systems[0].aliases)
        far = resolve([_sighting("nhc", "EP152026", "EP", now, 25.0, -140.0)], previous, now)
        self.assertEqual(far.crosswalk, {})

    def test_model_system_attaches_by_proximity_or_stands_alone(self) -> None:
        now = utc(2026, 9, 12, 6)
        base = utc(2026, 9, 12, 0)
        sightings = [
            _sighting("nhc", "EP142026", "EP", base, 17.0, -126.0),
            _sighting("ecmwf", "70E", "EP", base, 16.8, -126.0, first_seen=base),
            _sighting("ecmwf", "71E", "EP", base + timedelta(hours=48), 12.0, -100.0, first_seen=base),
            _sighting("ecmwfens", "71E", "EP", base + timedelta(hours=48), 12.5, -100.5, first_seen=base),
        ]
        resolution = resolve(sightings, [], now)
        by_id = {s.id: s for s in resolution.systems}
        self.assertEqual(sorted(by_id), ["EP142026", "x-ep-2026091200-1"])
        self.assertIn("ecmwf:70E", by_id["EP142026"].aliases)
        self.assertEqual(by_id["x-ep-2026091200-1"].level, "C")
        self.assertEqual(len(by_id["x-ep-2026091200-1"].sightings), 2)
        # Next hour, the same C-level system is found where it was.
        previous = [
            _previous(
                "x-ep-2026091200-1",
                "C",
                "EP",
                by_id["x-ep-2026091200-1"].aliases,
                base + timedelta(hours=48),
                12.0,
                -100.0,
            )
        ]
        again = resolve(
            [
                _sighting(
                    "ecmwf", "72E", "EP", base + timedelta(hours=54), 12.4, -101.0, first_seen=base + timedelta(hours=6)
                )
            ],
            previous,
            now + timedelta(hours=6),
        )
        self.assertEqual(again.systems[0].id, "x-ep-2026091200-1")

    def test_distance(self) -> None:
        self.assertAlmostEqual(distance_km(0, 0, 0, 3), 333.6, places=0)
        self.assertLess(MATCH_KM, 340)


class SchemaTests(unittest.TestCase):
    def storm(self) -> dict[str, object]:
        return json.loads((EXPECTED / "EP142026.json").read_text(encoding="utf-8"))

    def test_expected_files_validate(self) -> None:
        validate_storm(self.storm())
        validate_index(json.loads((EXPECTED / "index.json").read_text(encoding="utf-8")))
        validate_pointer(json.loads((EXPECTED / "latest-tc.json").read_text(encoding="utf-8")))

    def test_rejects_malformed_storms(self) -> None:
        cases: list[tuple[str, object]] = [
            ("schemaVersion", 2),
            ("id", "ep142026"),
            ("level", "D"),
            ("basin", "ep"),
            ("aliases", []),
            ("impact", None),
            ("sources", [{"id": "jtwc", "ok": False}]),
        ]
        for key, value in cases:
            payload = self.storm()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(TcProductError):
                validate_storm(payload)
        payload = self.storm()
        payload["level"] = "B"  # an A-level id under another level
        payload["id"] = "EP142026"
        validate_storm({**payload, "level": "A"})
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "id": "x-ep-2026091012-1", "level": "A"})

    def test_rejects_broken_points_and_arrays(self) -> None:
        payload = self.storm()
        agencies = payload["agencies"]
        assert isinstance(agencies, dict)
        nhc = json.loads(json.dumps(agencies["nhc"]))
        nhc["points"][1]["lead"] = nhc["points"][0]["lead"]
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "agencies": {"nhc": nhc}})
        nhc = json.loads(json.dumps(agencies["nhc"]))
        nhc["points"][1]["radii"] = {"34": [1, 2, 3]}
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "agencies": {"nhc": nhc}})
        nhc = json.loads(json.dumps(agencies["nhc"]))
        nhc["points"][1]["lon"] = 181
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "agencies": {"nhc": nhc}})
        models = payload["models"]
        assert isinstance(models, dict)
        ens = json.loads(json.dumps(models["ecmwfens"]))
        ens["lat"] = ens["lat"][:-1]
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "models": {"ecmwfens": ens}})
        ens = json.loads(json.dumps(models["ecmwfens"]))
        ens["lat"][0] = 9001
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "models": {"ecmwfens": ens}})
        ens = json.loads(json.dumps(models["ecmwfens"]))
        ens["lat"][0] = MISSING
        validate_storm({**payload, "models": {"ecmwfens": ens}})

    def test_unknown_agency_ids_are_admitted(self) -> None:
        payload = self.storm()
        agencies = payload["agencies"]
        assert isinstance(agencies, dict)
        validate_storm({**payload, "agencies": {"somebody": agencies["nhc"]}})
        with self.assertRaises(TcProductError):
            validate_storm({**payload, "agencies": {"Some-Body": agencies["nhc"]}})

    def test_pointer(self) -> None:
        index = encode_json({"schemaVersion": 1})
        pointer = build_pointer(ISSUE, "tc.2026091206/index.json", index)
        self.assertEqual(pointer["crc32"], crc32_hex(index))
        with self.assertRaises(TcProductError):
            validate_pointer({**pointer, "path": "tc.2026091207/index.json"})
        with self.assertRaises(TcProductError):
            validate_pointer({**pointer, "path": "/tc.2026091206/index.json"})
        with self.assertRaises(TcProductError):
            parse_issue("2026091206Z")

    def test_wrap_longitude(self) -> None:
        self.assertEqual(wrap_longitude(247.3), -112.7)
        self.assertEqual(wrap_longitude(-180.0), 180.0)
        self.assertEqual(wrap_longitude(109.6), 109.6)


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
class GoldenBuildTests(unittest.TestCase):
    def test_build_matches_the_golden(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            report = build_product(ISSUE, TC_FIXTURES, output, force=True, now=utc(2026, 9, 12, 7, 5))
            directory = output / "tc.2026091206"
            built = {path.name: json.loads(path.read_bytes()) for path in directory.iterdir()}
            expected = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in EXPECTED.iterdir()
                if path.name != "latest-tc.json"
            }
            self.assertEqual(sorted(built), sorted(expected))
            for name in sorted(expected):
                with self.subTest(file=name):
                    self.assertEqual(built[name], expected[name])
            index = built["index.json"]
            for entry in index["storms"]:
                payload = (directory / entry["path"]).read_bytes()
                self.assertEqual((len(payload), crc32_hex(payload)), (entry["byteLength"], entry["crc32"]))
            pointer = json.loads((output / "latest-tc.json").read_bytes())
            self.assertEqual(pointer, json.loads((EXPECTED / "latest-tc.json").read_text(encoding="utf-8")))
            index_bytes = (directory / "index.json").read_bytes()
            self.assertEqual((pointer["byteLength"], pointer["crc32"]), (len(index_bytes), crc32_hex(index_bytes)))
            self.assertEqual(
                [s["id"] for s in report["storms"]], ["EP142026", "x-ep-2026091012-1", "x-wp-2026091200-1"]
            )
            self.assertTrue(all(status["ok"] for status in report["sources"]))

    def test_second_hour_keeps_ids_and_a_failed_source_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch) / "raw"
            shutil.copytree(TC_FIXTURES / "tc.2026091206", raw / "tc.2026091207")
            shutil.rmtree(raw / "tc.2026091207" / "ecmwfens")
            (raw / "tc.2026091207" / "nhc" / "fetch.json").write_text(
                json.dumps(
                    {"id": "nhc", "ok": False, "fetched": "2026-09-12T08:01:00Z", "error": "HTTP 503", "files": []}
                )
            )
            output = Path(scratch) / "out"
            previous = json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))
            report = build_product(parse_issue("2026091207"), raw, output, previous_index=previous, force=True)
            self.assertEqual(sorted(s["id"] for s in report["storms"]), sorted(s["id"] for s in previous["storms"]))
            statuses = {status["id"]: status for status in report["sources"]}
            self.assertEqual(statuses["nhc"]["error"], "HTTP 503")
            self.assertEqual(statuses["ecmwfens"]["error"], "not fetched")
            self.assertTrue(statuses["jtwc"]["ok"])
            storm = json.loads((output / "tc.2026091207" / "EP142026.json").read_bytes())
            # Without NHC's b-deck the US best track comes from JTWC's warning history.
            self.assertEqual(storm["best"]["usa"]["source"], "jtwc")
            self.assertEqual(sorted(storm["agencies"]), ["jtwc"])
            self.assertNotIn("ecmwfens", storm["models"])
            self.assertEqual(len(previous_systems(previous)), 3)

    def test_pointer_is_withheld_when_nothing_contributed(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch) / "raw"
            (raw / "tc.2026091206" / "ibtracs").mkdir(parents=True)
            shutil.copy(RAW / "ibtracs" / "ibtracs.ACTIVE.list.v04r01.csv", raw / "tc.2026091206" / "ibtracs")
            shutil.copy(RAW / "ibtracs" / "fetch.json", raw / "tc.2026091206" / "ibtracs")
            output = Path(scratch) / "out"
            report = build_product(ISSUE, raw, output, force=True)
            self.assertIsNone(report["pointer"])
            self.assertFalse((output / "latest-tc.json").exists())
            self.assertTrue((output / "tc.2026091206" / "index.json").exists())
            with self.assertRaises(TcProductError):
                build_product(ISSUE, raw, output)


if __name__ == "__main__":
    unittest.main()
