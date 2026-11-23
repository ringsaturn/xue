"""The radiosonde sounding product (``xuebuild/sounding``): the BUFR
parser on the fetched fixture's three bulletins, the identity and
deduplication rules on constructed soundings, the derived quantities on
profiles with answers worked out by hand, the incremental listing, the
copy-forward of an unchanged station, the validators on malformed input,
and the whole build held to a committed golden.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from tests._support import FIXTURES
from xuebuild.errors import DownloadError, SoundingProductError
from xuebuild.sounding import bufr, derive, meteofrance
from xuebuild.sounding.build import (
    KEEP_TIMES,
    _ascent_messages,
    _read_previous_soundings,
    _sounding_json,
    _sounding_of,
    build_product,
    newest_ascents,
    station_name,
    station_names,
)
from xuebuild.sounding.fetch import (
    BUCKET_BASE_URL,
    DEFAULT_SOURCES,
    GATEWAY_PREFIX,
    NATIVE_PREFIX,
    SOURCE_IDS,
    RemoteObject,
    fetch_gateway,
    fetch_sources,
    fetched_sources,
    issue_raw_directory,
    list_centres,
    list_objects,
    objects_to_fetch,
    read_fetch_record,
    source_directory,
)
from xuebuild.sounding.schema import (
    build_pointer,
    crc32_hex,
    encode_json,
    parse_issue,
    SOUNDINGS_FILENAME,
    validate_derived,
    validate_index,
    validate_pointer,
    validate_station_line,
)

SOUNDING_FIXTURES = FIXTURES / "sounding"
RAW = SOUNDING_FIXTURES / "sounding.2026091402"
EXPECTED = SOUNDING_FIXTURES / "expected"
MISSING = bufr.MISSING
ISSUE = parse_issue("2026091402")
GATEWAY = "jp-jma-gts-to-wis2"
HAS_BUFR_DUMP = shutil.which("bufr_dump") is not None

RJTD = "A_IUSC01RJTD140000_C_RJTD_20260914012350_137.bufr"
KWBC = "A_IUSC01KWBC140000CCA_C_RJTD_20260914015222_129.bufr"
BABJ = "A_IUSN01BABJ140000_C_RJTD_20260914023429_185.bufr"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def read_bulletin(name: str) -> bufr.BulletinResult:
    from xuebuild.eccodescli import bufr_dump_json

    parsed = bufr.parse_file_name(name)
    assert parsed is not None
    return bufr.parse_bulletin(bufr_dump_json(RAW / GATEWAY / name), name=parsed, gateway=GATEWAY)


def sounding(
    *,
    key: str = "47401",
    wigos: str | None = None,
    wmo: str | None = "47401",
    time: datetime | None = None,
    arrived: datetime | None = None,
    levels: int = 10,
    bulletin: str = "IUSC01 RJTD 140000",
) -> bufr.Sounding:
    """A constructed sounding: ``levels`` levels descending from 1000 hPa
    in whole hectopascals, everything else plausible and constant."""
    pressures = tuple(100000 - 1000 * step for step in range(levels))
    return bufr.Sounding(
        key=key,
        wigos=wigos,
        wmo=wmo,
        time=time or utc(2026, 9, 14, 0),
        launched=None,
        lat=45.415,
        lon=141.679,
        elev=2.9,
        sonde=41,
        bulletin=bulletin,
        gateway=GATEWAY,
        arrived=arrived or utc(2026, 9, 14, 1, 23, 50),
        reported=levels,
        p=pressures,
        z=tuple(80 * step for step in range(levels)),
        t=tuple(29315 - 65 * step for step in range(levels)),
        td=tuple(29015 - 80 * step for step in range(levels)),
        wd=tuple(180 for _ in range(levels)),
        ws=tuple(50 + step for step in range(levels)),
        sig=tuple(bufr.STANDARD_BIT for _ in range(levels)),
    )


class FileNameTests(unittest.TestCase):
    def test_the_heading_the_nominal_hour_and_the_arrival(self) -> None:
        parsed = bufr.parse_file_name(RJTD)
        assert parsed is not None
        self.assertEqual(parsed.header, "IUSC01 RJTD 140000")
        self.assertEqual(parsed.nominal, utc(2026, 9, 14, 0, 0))
        self.assertEqual(parsed.arrived, utc(2026, 9, 14, 1, 23, 50))
        self.assertIsNone(parsed.correction)

    def test_a_correction_suffix_is_part_of_the_heading(self) -> None:
        parsed = bufr.parse_file_name(KWBC)
        assert parsed is not None
        self.assertEqual(parsed.header, "IUSC01 KWBC 140000 CCA")
        self.assertEqual(parsed.correction, "CCA")

    def test_the_day_of_month_is_dated_across_a_month_boundary(self) -> None:
        parsed = bufr.parse_file_name("A_IUSC01RJTD301200_C_RJTD_20261001011500_1.bufr")
        assert parsed is not None
        self.assertEqual(parsed.nominal, utc(2026, 9, 30, 12, 0))
        late = bufr.parse_file_name("A_IUSC01RJTD010000_C_RJTD_20260930235900_1.bufr")
        assert late is not None
        self.assertEqual(late.nominal, utc(2026, 10, 1, 0, 0))

    def test_the_dwd_gateway_writes_no_extension(self) -> None:
        parsed = bufr.parse_file_name("A_IUSC01RJTD140000CCB_C_EDZW_20260914014522_30102177")
        assert parsed is not None
        self.assertEqual(parsed.header, "IUSC01 RJTD 140000 CCB")
        self.assertEqual(parsed.nominal, utc(2026, 9, 14, 0, 0))
        self.assertEqual(parsed.arrived, utc(2026, 9, 14, 1, 45, 22))

    def test_a_name_that_is_not_the_convention_is_refused(self) -> None:
        self.assertIsNone(bufr.parse_file_name("fetch.json"))
        self.assertIsNone(bufr.parse_file_name("A_IUSC01RJTD140000_C_RJTD_notatime_1.bufr"))


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
class ParserTests(unittest.TestCase):
    def test_the_japanese_bulletin_carries_five_stations(self) -> None:
        result = read_bulletin(RJTD)
        self.assertEqual((result.subsets, result.dropped), (5, 0))
        self.assertEqual([item.key for item in result.soundings], ["47401", "47412", "47418", "47582", "47600"])
        first = result.soundings[0]
        # Checked by hand against `bufr_dump -p`, subset 1 level 1:
        # pressure=100800 Pa, no geopotential height, 295.15 K, 290.35 K,
        # 210°, 4.6 m/s, significance 131072 (bit 1, the surface).
        self.assertEqual(
            [first.p[0], first.z[0], first.t[0], first.td[0], first.wd[0], first.ws[0], first.sig[0]],
            [100800, bufr.MISSING, 29515, 29035, 210, 46, 131072],
        )
        self.assertEqual(first.n, 15)
        self.assertEqual((first.lat, first.lon, first.elev), (45.415, 141.679, 2.9))
        self.assertEqual(first.sonde, 41)
        self.assertEqual(first.time, utc(2026, 9, 14, 0))
        self.assertEqual(first.launched, utc(2026, 9, 14, 0, 16))
        self.assertEqual(first.bulletin, "IUSC01 RJTD 140000")

    def test_a_station_with_no_wigos_identifier_takes_the_legacy_form(self) -> None:
        first = read_bulletin(RJTD).soundings[0]
        self.assertIsNone(first.wigos)
        self.assertEqual(first.wmo, "47401")
        self.assertEqual(first.id, "0-20000-0-47401")

    def test_the_guam_bulletin_is_one_high_resolution_ascent(self) -> None:
        result = read_bulletin(KWBC)
        self.assertEqual(len(result.soundings), 1)
        guam = result.soundings[0]
        self.assertEqual((guam.key, guam.id), ("91212", "0-20000-0-91212"))
        self.assertEqual(guam.n, 219)
        self.assertEqual(
            [guam.p[0], guam.z[0], guam.t[0], guam.td[0], guam.wd[0], guam.ws[0], guam.sig[0]],
            [100000, 76, 30255, 29835, 50, 36, 196608],
        )
        # The second level is a significant wind level: no height, no
        # temperature, no dew point.
        self.assertEqual([guam.z[1], guam.t[1], guam.td[1], guam.sig[1]], [bufr.MISSING] * 3 + [2048])

    def test_the_chinese_bulletin_carries_native_wigos_identifiers(self) -> None:
        result = read_bulletin(BABJ)
        self.assertEqual(len(result.soundings), 13)
        urumqi = result.soundings[0]
        self.assertEqual(urumqi.wigos, "0-20001-0-51463")
        self.assertEqual((urumqi.key, urumqi.wmo, urumqi.id), ("51463", "51463", "0-20001-0-51463"))
        # One unflagged level sat inside the thinning ratio of its
        # neighbour; the other 165 are the classical set and survive.
        self.assertEqual((urumqi.reported, urumqi.n), (166, 165))
        self.assertEqual(
            [urumqi.p[0], urumqi.z[0], urumqi.t[0], urumqi.td[0], urumqi.wd[0], urumqi.ws[0], urumqi.sig[0]],
            [90560, 1008, 29465, 27414, 145, 34, 210944],
        )
        # The launch is 23:15 on the 13th, 45 minutes before the 00Z the
        # bulletin is filed under.
        self.assertEqual(urumqi.time, utc(2026, 9, 14, 0))
        self.assertEqual(urumqi.launched, utc(2026, 9, 13, 23, 15))
        self.assertEqual(urumqi.sonde, 208)

    def test_levels_are_in_strictly_descending_pressure(self) -> None:
        for name in (RJTD, KWBC, BABJ):
            for item in read_bulletin(name).soundings:
                with self.subTest(bulletin=name, station=item.key):
                    self.assertTrue(all(b < a for a, b in zip(item.p, item.p[1:])))
                    self.assertTrue(all(len(getattr(item, key)) == item.n for key in ("z", "t", "td", "wd", "ws", "sig")))


class LevelWalkTests(unittest.TestCase):
    def dump(self, entries: list[tuple[str, object]]) -> dict[str, object]:
        return {"messages": [{"key": key, "value": value} for key, value in entries]}

    def header(self) -> list[tuple[str, object]]:
        return [
            ("subsetNumber", 1),
            ("blockNumber", 47),
            ("stationNumber", 401),
            ("radiosondeType", 41),
            ("year", 2026),
            ("month", 9),
            ("day", 14),
            ("hour", 0),
            ("minute", 16),
            ("latitude", 45.415),
            ("longitude", 141.679),
            ("heightOfStationGroundAboveMeanSeaLevel", 2.9),
        ]

    def level(self, pressure: float, **values: object) -> list[tuple[str, object]]:
        row: list[tuple[str, object]] = [
            ("extendedVerticalSoundingSignificance", values.get("sig", 65536)),
            ("pressure", pressure),
            ("nonCoordinateGeopotentialHeight", values.get("z")),
            ("airTemperature", values.get("t")),
            ("dewpointTemperature", values.get("td")),
            ("windDirection", values.get("wd")),
            ("windSpeed", values.get("ws")),
        ]
        return row

    def parse(self, entries: list[tuple[str, object]]) -> bufr.BulletinResult:
        name = bufr.parse_file_name(RJTD)
        assert name is not None
        return bufr.parse_bulletin(self.dump(entries), name=name, gateway=GATEWAY)

    def test_missing_values_become_the_fixed_point_sentinel(self) -> None:
        result = self.parse(self.header() + self.level(100000.0, t=283.15) + self.level(92500.0))
        item = result.soundings[0]
        self.assertEqual(item.t, (28315, bufr.MISSING))
        self.assertEqual(item.td, (bufr.MISSING, bufr.MISSING))
        self.assertEqual(item.ws, (bufr.MISSING, bufr.MISSING))

    def test_levels_out_of_order_are_sorted_and_a_level_without_pressure_is_dropped(self) -> None:
        entries = self.header() + self.level(50000.0, t=253.15) + self.level(100000.0, t=283.15)
        entries += [("extendedVerticalSoundingSignificance", 2048), ("windSpeed", 12.0)]
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.p, (100000, 50000))
        self.assertEqual(item.t, (28315, 25315))

    def test_two_levels_at_one_pressure_are_folded_into_one(self) -> None:
        entries = self.header() + self.level(85000.0, t=273.15, sig=8192) + self.level(85000.0, ws=10.0, sig=2048)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.n, 1)
        self.assertEqual(item.t, (27315,))
        self.assertEqual(item.ws, (100,))
        self.assertEqual(item.sig, (8192 | 2048,))

    def test_a_value_outside_its_range_is_missing_not_published(self) -> None:
        # Live GTS bulletins carry wind directions like 504°; one must not
        # take down the hour, and it is not a measurement either.
        entries = self.header() + self.level(100000.0, t=283.15, wd=504.0, ws=5.0)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.wd, (bufr.MISSING,))
        self.assertEqual(item.ws, (50,))
        entries = self.header() + self.level(100000.0, t=999.0)
        self.assertEqual(self.parse(entries).soundings[0].t, (bufr.MISSING,))

    def test_a_part_d_ten_times_too_low_is_put_back(self) -> None:
        def parse(name: str, *pressures: float) -> tuple[int, ...]:
            entries = self.header()
            for pressure in pressures:
                entries += self.level(pressure, t=217.15, sig=8192)
            parsed = bufr.native_name(name, utc(2026, 9, 14, 1, 5))
            dump = self.dump(entries)
            return bufr.parse_bulletin(dump, name=parsed, gateway="wis2:us-noaa-nws").soundings[0].p

        self.assertEqual(parse("TEMP_DD_47401_x.bufr4", 99200.0, 7200.0), (9920, 720))
        # A part D that is right already is taken as it is, and so is any
        # other object whatever it holds.
        self.assertEqual(parse("TEMP_DD_47401_x.bufr4", 9920.0, 720.0), (9920, 720))
        self.assertEqual(parse("TEMP_BB_47401_x.bufr4", 99200.0, 10000.0), (99200, 10000))
        self.assertEqual(parse("WIGOS_0-20000-0-47401_x.bufr4", 99200.0, 7200.0), (99200, 7200))

    def test_a_level_whose_pressure_is_out_of_range_is_dropped(self) -> None:
        entries = self.header() + self.level(0.0, t=283.15) + self.level(100000.0, t=284.15)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.p, (100000,))
        self.assertEqual(item.t, (28415,))

    def test_the_parser_never_emits_what_the_validator_refuses(self) -> None:
        self.assertEqual(sorted(bufr.VALUE_BOUNDS), sorted(("p", "z", "t", "td", "wd", "ws", "sig")))

    def test_an_unflagged_ascent_is_thinned_to_the_pressure_ratio(self) -> None:
        # A second-resolution sonde: a thousand unflagged levels from
        # 1000 hPa to 10 hPa, none of them worth drawing on their own.
        entries = self.header()
        pressures = [100000.0 * (0.01 ** (step / 999)) for step in range(1000)]
        for pressure in pressures:
            entries += self.level(pressure, t=283.15, sig=0)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.reported, 1000)
        self.assertLessEqual(item.n, 160)
        self.assertEqual(item.p[0], bufr._fixed(pressures[0], 1, "p"))
        self.assertEqual(item.p[-1], bufr._fixed(pressures[-1], 1, "p"))
        # Every consecutive kept pair stands at least the ratio apart,
        # the last one aside: the top level is kept unconditionally.
        for earlier, later in zip(item.p[:-2], item.p[1:-1]):
            self.assertGreaterEqual(earlier / later, 1.0 + bufr.THINNING_RATIO)

    def test_a_flagged_level_is_never_thinned_away(self) -> None:
        entries = self.header()
        flags = [
            bufr.SURFACE_BIT,
            bufr.STANDARD_BIT,
            bufr.TROPOPAUSE_BIT,
            bufr.MAX_WIND_BIT,
            bufr.SIGNIFICANT_TEMPERATURE_BIT,
            bufr.SIGNIFICANT_HUMIDITY_BIT,
            bufr.SIGNIFICANT_WIND_BIT,
        ]
        # Levels a hair apart in pressure — far inside the ratio — so only
        # their flags can save them.
        for step, flag in enumerate(flags):
            entries += self.level(100000.0 - step, t=283.15, sig=flag)
        entries += self.level(100000.0 - len(flags), t=283.15, sig=0)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.n, len(flags) + 1)  # the last is kept as the top
        self.assertEqual(list(item.sig), flags + [0])

    def test_an_unflagged_level_between_flagged_ones_is_dropped(self) -> None:
        entries = self.header()
        entries += self.level(100000.0, t=283.15, sig=bufr.SURFACE_BIT)
        entries += self.level(99900.0, t=283.05, sig=0)  # 0.1 % below: too close
        entries += self.level(92500.0, t=280.15, sig=bufr.STANDARD_BIT)
        entries += self.level(50000.0, t=253.15, sig=bufr.STANDARD_BIT)
        item = self.parse(entries).soundings[0]
        self.assertEqual((item.reported, item.n), (4, 3))
        self.assertEqual(item.p, (100000, 92500, 50000))

    def test_a_missing_significance_counts_as_unflagged(self) -> None:
        entries = self.header()
        entries += self.level(100000.0, t=283.15, sig=None)
        entries += self.level(99900.0, t=283.05, sig=None)
        entries += self.level(50000.0, t=253.15, sig=None)
        item = self.parse(entries).soundings[0]
        self.assertEqual((item.reported, item.n), (3, 2))
        self.assertEqual(item.p, (100000, 50000))

    def test_a_short_ascent_is_never_thinned(self) -> None:
        entries = self.header() + self.level(100000.0, t=283.15, sig=0) + self.level(99900.0, t=283.05, sig=0)
        item = self.parse(entries).soundings[0]
        self.assertEqual((item.reported, item.n), (2, 2))

    def test_a_subset_without_a_station_identity_is_dropped(self) -> None:
        entries = [entry for entry in self.header() if entry[0] not in ("blockNumber", "stationNumber")]
        result = self.parse(entries + self.level(100000.0, t=283.15))
        self.assertEqual((len(result.soundings), result.subsets, result.dropped), (0, 1, 1))

    def test_a_launch_far_from_the_nominal_hour_is_not_published(self) -> None:
        entries = [("minute", 16) if key == "minute" else (key, value) for key, value in self.header()]
        entries = [("hour", 6) if key == "hour" else (key, value) for key, value in entries]
        item = self.parse(entries + self.level(100000.0, t=283.15)).soundings[0]
        self.assertIsNone(item.launched)

    def test_a_compressed_message_gives_one_sounding_per_subset(self) -> None:
        entries: list[tuple[str, object]] = [
            ("blockNumber", [47, 47]),
            ("stationNumber", [401, 412]),
            ("year", 2026),
            ("month", 9),
            ("day", 14),
            ("hour", 0),
            ("minute", 16),
            ("latitude", [45.415, 43.06]),
            ("longitude", [141.679, 141.329]),
            ("extendedVerticalSoundingSignificance", [65536, 65536]),
            ("pressure", [100000.0, 100000.0]),
            ("airTemperature", [283.15, 284.15]),
        ]
        result = self.parse(entries)
        self.assertEqual([item.key for item in result.soundings], ["47401", "47412"])
        self.assertEqual([item.t[0] for item in result.soundings], [28315, 28415])


def part(letters: str) -> bufr.Sounding:
    """One part of a TEMP as the US node sends it: A and C with the
    standard levels and their heights, B and D with the significant ones."""
    temperature = bufr.SIGNIFICANT_TEMPERATURE_BIT
    levels = {
        "AA": [
            (100000, 176, MISSING, bufr.STANDARD_BIT),
            (98600, MISSING, 27575, bufr.SURFACE_BIT),
            (92500, 802, 27055, bufr.STANDARD_BIT),
            (10000, 16000, 21700, bufr.STANDARD_BIT),
        ],
        "BB": [
            (98600, MISSING, 27575, bufr.SURFACE_BIT | temperature),
            (82900, MISSING, 26275, temperature),
            (10000, MISSING, 21710, temperature),
        ],
        "CC": [(7000, 18390, 21555, bufr.STANDARD_BIT)],
        "DD": [(9920, MISSING, 21715, temperature)],
    }[letters]
    count = len(levels)
    return replace(
        sounding(key="72649", wmo="72649", time=utc(2026, 11, 23), bulletin=f"TEMP_{letters}_72649_x.bufr4"),
        gateway="wis2:us-noaa-nws",
        arrived=utc(2026, 11, 23, 1, 5) if letters in ("CC", "DD") else utc(2026, 11, 23, 0, 53),
        reported=count,
        p=tuple(level[0] for level in levels),
        z=tuple(level[1] for level in levels),
        t=tuple(level[2] for level in levels),
        td=(MISSING,) * count,
        wd=(MISSING,) * count,
        ws=(MISSING,) * count,
        sig=tuple(level[3] for level in levels),
    )


class IdentityAndDedupTests(unittest.TestCase):
    def test_the_key_is_the_wmo_number_and_the_id_prefers_the_native_wigos(self) -> None:
        legacy = sounding()
        native = sounding(wigos="0-20001-0-47401")
        self.assertEqual(legacy.key, native.key)
        self.assertEqual(legacy.id, "0-20000-0-47401")
        self.assertEqual(native.id, "0-20001-0-47401")

    def test_a_station_with_no_wmo_number_is_keyed_by_its_wigos_id(self) -> None:
        item = sounding(key="0-20000-0-99999", wigos="0-20000-0-99999", wmo=None)
        self.assertEqual(item.id, "0-20000-0-99999")

    def test_the_sounding_with_the_most_levels_wins(self) -> None:
        short = sounding(levels=40, arrived=utc(2026, 9, 14, 3))
        long = sounding(levels=180, arrived=utc(2026, 9, 14, 1))
        self.assertEqual(bufr.deduplicate([short, long]), [long])
        self.assertEqual(bufr.deduplicate([long, short]), [long])

    def test_a_correction_supersedes_at_the_same_level_count(self) -> None:
        first = sounding(levels=70, arrived=utc(2026, 9, 14, 1, 23, 50))
        correction = sounding(
            levels=70, arrived=utc(2026, 9, 14, 2, 10), bulletin="IUSC01 RJTD 140000 CCA"
        )
        self.assertEqual(bufr.deduplicate([first, correction])[0].bulletin, "IUSC01 RJTD 140000 CCA")
        self.assertEqual(bufr.deduplicate([correction, first])[0].bulletin, "IUSC01 RJTD 140000 CCA")

    def test_different_hours_and_different_stations_are_kept_apart(self) -> None:
        deduplicated = bufr.deduplicate(
            [
                sounding(time=utc(2026, 9, 14, 0)),
                sounding(time=utc(2026, 9, 13, 12)),
                sounding(key="47412", wmo="47412"),
            ]
        )
        self.assertEqual(len(deduplicated), 3)

    def test_the_part_is_read_off_a_node_object_s_name(self) -> None:
        name = bufr.native_name("TEMP_DD_72649_d6e1a535.bufr4", utc(2026, 11, 23, 1, 5, 36))
        self.assertEqual(name.part, "DD")
        self.assertIsNone(bufr.native_name("WIGOS_0-20000-0-82022_20261104T233000.bufr4", name.arrived).part)

    def test_the_parts_of_one_ascent_are_merged(self) -> None:
        merged = bufr.deduplicate([part("DD"), part("BB"), part("CC"), part("AA")])
        self.assertEqual(len(merged), 1)
        ascent = merged[0]
        # 1000 hPa is under the surface level with a height and nothing
        # measured, and is left out.
        self.assertEqual(ascent.p, (98600, 92500, 82900, 10000, 9920, 7000))
        self.assertEqual(ascent.z, (MISSING, 802, MISSING, 16000, MISSING, 18390))
        self.assertEqual(ascent.t, (27575, 27055, 26275, 21700, 21715, 21555))
        # The surface is in A and in B, and 100 hPa too: A keeps its values
        # and the flags are or-ed.
        self.assertEqual(ascent.sig[0], bufr.SURFACE_BIT | bufr.SIGNIFICANT_TEMPERATURE_BIT)
        self.assertEqual(ascent.sig[3], bufr.STANDARD_BIT | bufr.SIGNIFICANT_TEMPERATURE_BIT)
        self.assertEqual(ascent.reported, ascent.n)
        self.assertIsNone(ascent.part)
        self.assertEqual(
            ascent.bulletin,
            "TEMP_AA_72649_x.bufr4 + TEMP_BB_72649_x.bufr4 + TEMP_CC_72649_x.bufr4 + TEMP_DD_72649_x.bufr4",
        )
        self.assertEqual(ascent.arrived, utc(2026, 11, 23, 1, 5))

    def test_the_fullest_copy_of_a_part_is_the_one_merged(self) -> None:
        early = replace(
            part("BB"),
            reported=1,
            p=(98600,),
            z=(MISSING,),
            t=(27000,),
            td=(26000,),
            wd=(0,),
            ws=(0,),
            sig=(bufr.SURFACE_BIT,),
        )
        merged = bufr.merge_parts([early, part("DD"), part("BB")])
        assert merged is not None
        self.assertEqual(merged.p, (98600, 82900, 10000, 9920))
        # One copy of a part and another of the same are still one part.
        self.assertIsNone(bufr.merge_parts([part("BB"), early]))

    def test_a_part_is_told_by_its_levels(self) -> None:
        for letters in ("AA", "BB", "CC", "DD"):
            self.assertEqual(part(letters).part, letters)
        self.assertEqual(sounding(levels=60).part, "AA")
        through = replace(sounding(levels=3), p=(100000, 50000, 5000))
        self.assertIsNone(through.part)

    def test_the_lower_half_of_a_full_ascent_is_completed_without_part_b(self) -> None:
        half = replace(sounding(levels=80, key="72649", wmo="72649", time=utc(2026, 11, 23)))
        merged = bufr.merge_parts([half, part("BB"), part("CC"), part("DD")])
        assert merged is not None
        self.assertEqual(merged.p, (*half.p, 9920, 7000))
        self.assertEqual(merged.bulletin, "IUSC01 RJTD 140000 + TEMP_CC_72649_x.bufr4 + TEMP_DD_72649_x.bufr4")

    def test_parts_of_another_flight_are_not_merged(self) -> None:
        base = replace(part("AA"), launched=utc(2026, 11, 22, 23, 30))
        second = replace(part("BB"), launched=utc(2026, 11, 23, 0, 30))
        self.assertIsNone(bufr.merge_parts([base, second]))
        # The nominal hour on the clock is no launch.
        stand_in = replace(part("BB"), launched=utc(2026, 11, 23))
        self.assertIsNotNone(bufr.merge_parts([base, stand_in]))

    def test_a_whole_ascent_is_kept_over_parts_that_hold_no_more(self) -> None:
        merged = bufr.merge_parts([part("AA"), part("BB"), part("CC"), part("DD")])
        assert merged is not None
        whole = replace(merged, bulletin="IUSC60 RJTD 230000", arrived=utc(2026, 11, 23, 0, 30))
        self.assertEqual(bufr.deduplicate([part("AA"), part("BB"), part("CC"), part("DD"), whole]), [whole])

    def test_an_ascent_a_previous_issue_published_is_completed(self) -> None:
        station = {"wmo": "72649", "lat": 45.415, "lon": 141.679, "elev": 2.9}
        (published,) = bufr.deduplicate([part("AA"), part("CC")])
        history = _sounding_of(station, _sounding_json(published), "72649")
        self.assertEqual(history, published)
        (complete,) = bufr.deduplicate([history, part("DD"), part("BB")])
        self.assertEqual(complete.p, (98600, 92500, 82900, 10000, 9920, 7000))
        # The same part sent again changes nothing.
        self.assertEqual(bufr.deduplicate([complete, part("DD")]), [complete])

    def test_the_upper_parts_alone_are_no_ascent(self) -> None:
        self.assertEqual(bufr.deduplicate([part("CC"), part("DD")]), [])

    def test_the_parts_of_two_hours_are_not_merged(self) -> None:
        later = replace(part("BB"), time=utc(2026, 11, 23, 12))
        self.assertEqual([item.n for item in bufr.deduplicate([part("AA"), later])], [3, 3])

    def test_a_lone_upper_part_already_published_is_not_carried_forward(self) -> None:
        lower = _sounding_json(part("BB"))
        upper = _sounding_json(part("DD")) | {"time": "2026-11-23T12:00:00Z"}
        entries = [upper, lower]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            directory = root / "sounding/2026/11/23/sounding.2026112309"
            directory.mkdir(parents=True)
            lines = [
                {"id": "0-20000-0-72649", "soundings": entries},
                {"id": "0-20000-0-74455", "soundings": entries[:1]},
            ]
            (directory / SOUNDINGS_FILENAME).write_text("".join(json.dumps(line) + "\n" for line in lines))
            previous = _read_previous_soundings(
                root, {"issued": "2026-11-23T09:00:00Z", "soundings": {"path": SOUNDINGS_FILENAME}}
            )
        self.assertEqual(list(previous), ["0-20000-0-72649"])
        self.assertEqual([item["n"] for item in previous["0-20000-0-72649"]["soundings"]], [3])


    def test_a_line_keeps_the_newest_four_nominal_times(self) -> None:
        ascents = [sounding(time=utc(2026, 9, day)) for day in range(8, 15)]
        kept = newest_ascents(ascents)
        self.assertEqual(len(kept), KEEP_TIMES)
        self.assertEqual([item.time.day for item in kept], [14, 13, 12, 11])


class DerivedTests(unittest.TestCase):
    def profile(self) -> dict[str, object]:
        return {
            "n": 5,
            "p": [100000, 90000, 85000, 80000, 50000],
            "z": [100, 1000, 1500, 2000, 5600],
            "t": [28315, 27615, 27365, 27115, 25315],
            "td": [28015, 27315, 27065, 26815, 24315],
            "wd": [180, 190, 200, 210, 250],
            "ws": [50, 60, 70, 80, 300],
            "sig": [bufr.SURFACE_BIT, 0, bufr.STANDARD_BIT, 0, bufr.STANDARD_BIT | bufr.TROPOPAUSE_BIT],
        }

    def test_freezing_level_is_linear_in_temperature(self) -> None:
        # 273.65 K at 1500 gpm and 271.15 K at 2000 gpm straddle 273.15 K
        # a fifth of the way up: 1500 + 0.2 × 500 = 1600 gpm.
        self.assertEqual(derive.derive(self.profile())["freezingLevel"], 1600)

    def test_a_surface_below_freezing_has_no_freezing_level(self) -> None:
        profile = self.profile()
        profile["t"] = [27215, 27115, 27015, 26915, 25315]
        self.assertIsNone(derive.derive(profile)["freezingLevel"])

    def test_a_profile_that_never_crosses_has_no_freezing_level(self) -> None:
        profile = self.profile()
        profile["t"] = [28315, 28215, 28115, 28015, 27915]
        self.assertIsNone(derive.derive(profile)["freezingLevel"])

    def test_the_lapse_rate_is_between_the_850_and_500_levels(self) -> None:
        # (273.65 − 253.15) K over (5600 − 1500) / 1000 km = 5.0 K/km.
        self.assertEqual(derive.derive(self.profile())["lapse850_500"], 5.0)

    def test_the_lapse_rate_needs_both_standard_levels_within_a_hectopascal(self) -> None:
        profile = self.profile()
        profile["p"] = [100000, 90000, 84800, 80000, 50000]
        self.assertIsNone(derive.derive(profile)["lapse850_500"])
        profile["p"] = [100000, 90000, 84950, 80000, 50000]
        self.assertEqual(derive.derive(profile)["lapse850_500"], 5.0)

    def test_a_derived_height_may_be_negative_as_a_published_height_may(self) -> None:
        # Live bulletins flag a tropopause at a height below sea level.
        # That is a bad reading, not a schema violation: a derived height
        # is a published `z`, and `z` goes down to −1000.
        profile = self.profile()
        profile["z"] = [-100, -50, 10, 200, -600]
        derived = derive.derive(profile)
        self.assertEqual(derived["tropopause"], -600)
        validate_derived(derived, "derived")

    def test_the_tropopause_is_the_flagged_level(self) -> None:
        self.assertEqual(derive.derive(self.profile())["tropopause"], 5600)
        profile = self.profile()
        profile["sig"] = [bufr.STANDARD_BIT] * 5
        self.assertIsNone(derive.derive(profile)["tropopause"])

    def test_precipitable_water_against_a_hand_computation(self) -> None:
        # Two levels at a constant 10 °C dew point. Bolton's saturation
        # vapour pressure is 12.271696 hPa, so the mixing ratios are
        # 0.622 × 12.271696 / (1000 − 12.271696) and the same at 900 hPa;
        # the specific humidities are 0.00766857 and 0.00852504, and the
        # trapezium over 10000 Pa divided by 9.80665 is 8.2564 mm.
        profile = {
            "n": 2,
            "p": [100000, 90000],
            "z": [100, 1000],
            "t": [28315, 28315],
            "td": [28315, 28315],
            "wd": [180, 180],
            "ws": [50, 50],
            "sig": [bufr.SURFACE_BIT, bufr.STANDARD_BIT],
        }
        self.assertEqual(derive.derive(profile)["pw"], 8.3)

    def test_precipitable_water_stops_at_300_hectopascals(self) -> None:
        profile = {
            "n": 3,
            "p": [100000, 90000, 20000],
            "z": [100, 1000, 12000],
            "t": [28315, 28315, 22315],
            "td": [28315, 28315, 22315],
            "wd": [180, 180, 180],
            "ws": [50, 50, 50],
            "sig": [bufr.SURFACE_BIT, bufr.STANDARD_BIT, bufr.STANDARD_BIT],
        }
        self.assertEqual(derive.derive(profile)["pw"], 8.3)

    def test_missing_levels_are_skipped_not_counted_as_zero(self) -> None:
        profile = self.profile()
        profile["td"] = [28015, bufr.MISSING, 27065, 26815, 24315]
        with_gap = derive.derive(profile)["pw"]
        assert isinstance(with_gap, float)
        self.assertGreater(with_gap, 0.0)
        self.assertLess(with_gap, 100.0)

    def test_the_headline_reads_500_hectopascals_in_celsius(self) -> None:
        profile = self.profile()
        headline = derive.headline(profile, derive.derive(profile))
        self.assertEqual(headline["t500"], round(253.15 - 273.15, 1))
        self.assertEqual(headline["td500"], round(243.15 - 273.15, 1))
        self.assertEqual(headline["levels"], 5)

    def test_the_bolton_formula_is_the_encoder_s(self) -> None:
        # 6.112 hPa at 0 °C is the constant Bolton (1980) eq. 10 starts from.
        self.assertAlmostEqual(float(np.asarray(derive.saturation_vapour_pressure(0.0))), 6.112, places=6)


class ListingTests(unittest.TestCase):
    def listing(self, *pages: str) -> list[str]:
        return list(pages)

    def page(self, keys: list[tuple[str, str, int]], token: str | None = None) -> bytes:
        rows = "".join(
            f"<Contents><Key>{key}</Key><LastModified>{modified}</LastModified>"
            f"<Size>{size}</Size></Contents>"
            for key, modified, size in keys
        )
        truncated = "true" if token else "false"
        continuation = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>wis2globalcache</Name><IsTruncated>{truncated}</IsTruncated>{continuation}{rows}"
            "</ListBucketResult>"
        ).encode("utf-8")

    def branch(self, url: str, objects: list[tuple[str, str, int]]) -> bytes:
        """The objects of the one branch of the heading tree a listing asks for."""
        return self.page([item for item in objects if f"{item[0].split('/I/U/')[1][0]}%2F&" in f"{url}&"])

    def test_the_listing_follows_the_continuation_token(self) -> None:
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        pages = {
            f"{BUCKET_BASE_URL}?list-type=2&prefix={prefix.replace('/', '%2F')}": self.page(
                [(f"{prefix}S/A/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755)], token="next+token"
            ),
        }

        requested: list[str] = []

        def get(url: str) -> bytes:
            requested.append(url)
            if "continuation-token" in url:
                return self.page([(f"{prefix}K/N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636)])
            return next(iter(pages.values()))

        objects = list_objects(prefix, get=get)
        self.assertEqual(len(requested), 2)
        self.assertIn("continuation-token=next%2Btoken", requested[1])
        self.assertEqual([item.name for item in objects], [RJTD, BABJ])
        self.assertEqual(objects[0].last_modified, utc(2026, 9, 14, 1, 24, 1))
        self.assertEqual(objects[1].size, 51636)

    def objects(self) -> list[RemoteObject]:
        return [
            RemoteObject("data/a", utc(2026, 9, 14, 1, 0), 10),
            RemoteObject("data/b", utc(2026, 9, 14, 2, 0), 20),
            RemoteObject("data/c", utc(2026, 9, 14, 2, 30), 30),
        ]

    def test_the_watermark_selects_only_what_arrived_since(self) -> None:
        now = utc(2026, 9, 14, 3)
        wanted = objects_to_fetch(self.objects(), utc(2026, 9, 14, 2, 0), now=now)
        self.assertEqual([item.key for item in wanted], ["data/c"])

    def test_no_watermark_takes_everything(self) -> None:
        wanted = objects_to_fetch(self.objects(), None, now=utc(2026, 9, 14, 3))
        self.assertEqual(len(wanted), 3)

    def test_a_watermark_older_than_the_retention_takes_everything(self) -> None:
        stale = utc(2026, 9, 14, 2, 0) - timedelta(hours=30)
        wanted = objects_to_fetch(self.objects(), stale, now=utc(2026, 9, 14, 3))
        self.assertEqual(len(wanted), 3)

    def test_the_parts_of_a_temp_are_taken_again_behind_the_watermark(self) -> None:
        objects = [
            RemoteObject("data/TEMP_AA_72649_a.bufr4", utc(2026, 9, 14, 0, 53), 10),
            RemoteObject("data/TEMP_AA_72649_b.bufr4", utc(2026, 9, 13, 12, 53), 10),
            RemoteObject("data/WIGOS_0-20000-0-82022_20260913T233000.bufr4", utc(2026, 9, 14, 0, 53), 10),
            RemoteObject("data/TEMP_DD_72649_c.bufr4", utc(2026, 9, 14, 1, 35), 10),
        ]
        wanted = objects_to_fetch(
            objects, utc(2026, 9, 14, 1, 0), now=utc(2026, 9, 14, 2), part_lookback=timedelta(hours=4)
        )
        self.assertEqual(
            [item.name for item in wanted], ["TEMP_AA_72649_a.bufr4", "TEMP_DD_72649_c.bufr4"]
        )

    def test_the_sources_are_the_two_gateways(self) -> None:
        self.assertEqual(SOURCE_IDS, ("jp-jma-gts-to-wis2", "de-dwd-gts-to-wis2"))

    def gateway(self, scratch: Path, watermark: datetime | None, **kwargs: object):
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        objects = [
            (f"{prefix}S/C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755),
            (f"{prefix}K/N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636),
            (f"{prefix}S/N/01/BABJ/20260914005246_38.04896", "2026-09-14T02:35:00.000Z", 99),
        ]
        bodies = {key: b"x" * size for key, _, size in objects}

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.branch(url, objects)
            key = url[len(BUCKET_BASE_URL) :]
            return bodies[key.replace("%2F", "/")]

        return fetch_gateway(GATEWAY, scratch, watermark=watermark, get=get, **kwargs)  # type: ignore[arg-type]

    def test_a_first_fetch_takes_every_bulletin_and_skips_what_is_not_one(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            result = self.gateway(directory, None, now=utc(2026, 9, 14, 3))
            self.assertEqual(sorted(result.files), sorted([RJTD, BABJ]))
            self.assertEqual(result.status.detail["objects"], 2)
            self.assertEqual(result.status.detail["listed"], 3)
            self.assertEqual(result.status.detail["bytes"], 1755 + 51636)
            self.assertEqual(result.status.watermark, utc(2026, 9, 14, 2, 35))
            self.assertTrue((directory / RJTD).is_file())
            self.assertFalse((directory / "20260914005246_38.04896").exists())

    def test_the_watermark_stops_the_download_but_not_the_listing(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            result = self.gateway(Path(scratch), utc(2026, 9, 14, 1, 24, 1), now=utc(2026, 9, 14, 3))
            self.assertEqual(result.files, [BABJ])
            self.assertEqual(result.status.detail["listed"], 3)
            self.assertEqual(result.status.watermark, utc(2026, 9, 14, 2, 35))

    def test_a_bulletin_the_previous_issue_holds_is_copied_not_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            reuse = Path(scratch) / "previous" / GATEWAY
            reuse.mkdir(parents=True)
            (reuse / RJTD).write_bytes(b"x" * 1755)
            result = self.gateway(
                Path(scratch) / "issue",
                None,
                now=utc(2026, 9, 14, 3),
                reuse_root=Path(scratch) / "previous",
            )
            self.assertEqual((result.status.detail["objects"], result.status.detail["reused"]), (1, 1))

    def test_one_object_the_bucket_will_not_serve_does_not_lose_the_hour(self) -> None:
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        objects = [
            (f"{prefix}S/C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755),
            (f"{prefix}K/N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636),
        ]

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.branch(url, objects)
            if BABJ in url:
                raise DownloadError("HTTP Error 404: Not Found")
            return b"x" * 1755

        with tempfile.TemporaryDirectory() as scratch:
            result = fetch_gateway(GATEWAY, Path(scratch), watermark=None, now=utc(2026, 9, 14, 3), get=get)
            self.assertTrue(result.status.ok)
            self.assertEqual(result.files, [RJTD])
            self.assertEqual(result.status.detail["failed"], 1)

    def test_a_gateway_serving_nothing_fails_as_a_whole(self) -> None:
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        objects = [(f"{prefix}S/C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755)]

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.branch(url, objects)
            raise DownloadError("HTTP Error 503: Service Unavailable")

        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(DownloadError):
                fetch_gateway(GATEWAY, Path(scratch), watermark=None, now=utc(2026, 9, 14, 3), get=get)

    def test_an_unknown_gateway_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SoundingProductError):
                fetch_gateway("somewhere-else", Path(scratch), watermark=None)


def bufr_message(descriptors: tuple[int, ...], *, edition: int = 4, optional: bool = False) -> bytes:
    """A BUFR message with nothing in it but its framing and Section 3's
    descriptors — enough for the template reader, which reads no data."""
    flag_offset = 9 if edition == 4 else 7
    section1 = bytearray(22 if edition == 4 else 18)
    section1[0:3] = len(section1).to_bytes(3, "big")
    section1[flag_offset] = 0x80 if optional else 0
    section2 = (6).to_bytes(3, "big") + bytes(3) if optional else b""
    body = b"".join(
        ((descriptor // 100000) << 14 | ((descriptor // 1000) % 100) << 8 | descriptor % 1000).to_bytes(2, "big")
        for descriptor in descriptors
    )
    section3 = (7 + len(body)).to_bytes(3, "big") + b"\x00" + (1).to_bytes(2, "big") + b"\x80" + body
    section4 = (4).to_bytes(3, "big") + b"\x00"
    rest = bytes(section1) + section2 + section3 + section4 + b"7777"
    return b"BUFR" + (8 + len(rest)).to_bytes(3, "big") + bytes([edition]) + rest


class NativeNodeTests(unittest.TestCase):
    """The national WIS2 nodes: discovery, the per-node fetch, the nominal
    time from a subset's own clock, and the descent filter."""

    def listing(self, keys: list[tuple[str, str, int]] = (), prefixes: list[str] = ()) -> bytes:  # type: ignore[assignment]
        rows = "".join(
            f"<Contents><Key>{key}</Key><LastModified>{modified}</LastModified><Size>{size}</Size></Contents>"
            for key, modified, size in keys
        )
        common = "".join(f"<CommonPrefixes><Prefix>{prefix}</Prefix></CommonPrefixes>" for prefix in prefixes)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>wis2globalcache</Name><IsTruncated>false</IsTruncated>{rows}{common}"
            "</ListBucketResult>"
        ).encode("utf-8")

    def test_the_default_sources_add_the_node_discovery_and_meteo_france(self) -> None:
        self.assertEqual(DEFAULT_SOURCES, (*SOURCE_IDS, "wis2", "meteofrance"))

    def test_a_node_sits_under_the_selector_directory(self) -> None:
        root = Path("/raw/sounding.2026111006")
        self.assertEqual(source_directory(root, "wis2:fr-meteofrance"), root / "wis2" / "fr-meteofrance")
        self.assertEqual(source_directory(root, GATEWAY), root / GATEWAY)

    def test_discovery_skips_the_gateways_and_the_catalogues(self) -> None:
        page = self.listing(
            prefixes=[
                "data/br-inmet/",
                "data/de-dwd-gts-to-wis2/",
                "data/de-dwd-global-discovery-catalogue/",
                "data/fr-meteofrance/",
                "data/Not_A_Node/",
            ]
        )
        requested: list[str] = []

        def get(url: str) -> bytes:
            requested.append(url)
            return page

        self.assertEqual(list_centres(get=get), ["br-inmet", "fr-meteofrance"])
        self.assertIn("delimiter=%2F", requested[0])

    def test_the_clock_files_a_subset_on_its_synoptic_hour(self) -> None:
        def clock(day: int, hour: int, minute: int) -> dict[str, object]:
            return {"year": 2026, "month": 11, "day": day, "hour": hour, "minute": minute}

        self.assertEqual(bufr.nominal_from_clock(clock(9, 23, 15)), utc(2026, 11, 10, 0))
        self.assertEqual(bufr.nominal_from_clock(clock(10, 1, 11)), utc(2026, 11, 10, 0))
        self.assertEqual(bufr.nominal_from_clock(clock(10, 0, 0)), utc(2026, 11, 10, 0))
        self.assertEqual(bufr.nominal_from_clock(clock(10, 11, 40)), utc(2026, 11, 10, 12))
        self.assertEqual(bufr.nominal_from_clock(clock(10, 5, 15)), utc(2026, 11, 10, 6))
        self.assertEqual(bufr.nominal_from_clock(clock(30, 22, 0)), utc(2026, 12, 1, 0))
        self.assertIsNone(bufr.nominal_from_clock({"year": 2026, "month": 11, "day": 10, "hour": 1}))

    def test_a_node_name_carries_no_nominal_time(self) -> None:
        name = bufr.native_name("WIGOS_0-20000-0-82022_20261104T233000.bufr4", utc(2026, 11, 5, 2, 48, 9))
        self.assertIsNone(name.nominal)
        self.assertEqual(name.header, "WIGOS_0-20000-0-82022_20261104T233000.bufr4")
        self.assertEqual(name.arrived, utc(2026, 11, 5, 2, 48, 9))

    def test_the_template_is_read_off_section_3(self) -> None:
        self.assertEqual(bufr.unexpanded_descriptors(bufr_message((301150, 309052))), (301150, 309052))
        self.assertEqual(
            bufr.unexpanded_descriptors(bufr_message((309057,), edition=3, optional=True)), (309057,)
        )
        self.assertIsNone(bufr.unexpanded_descriptors(b"BUFR\x00\x00\x08\x02"))
        self.assertTrue(bufr.is_descent(bufr_message((301150, 309056))))
        self.assertFalse(bufr.is_descent(bufr_message((301150, 309052))))

    def test_messages_are_split_by_their_own_length(self) -> None:
        ascent, descent = bufr_message((309052,)), bufr_message((309056,))
        self.assertEqual(bufr.split_messages(b"\x01\r\r\n" + ascent + b"\r\r\n" + descent), [ascent, descent])

    def test_descents_are_left_out_of_the_decode(self) -> None:
        ascent, descent = bufr_message((309052,)), bufr_message((309056,))
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "object.bufr4"
            path.write_bytes(ascent)
            self.assertEqual(_ascent_messages(path), path)
            path.write_bytes(descent)
            self.assertIsNone(_ascent_messages(path))
            path.write_bytes(descent + ascent)
            kept = _ascent_messages(path)
            assert kept is not None
            self.assertNotEqual(kept, path)
            self.assertEqual(kept.read_bytes(), ascent)

    def bucket(self, *, discovery_fails: bool = False):
        """Three nodes: one with a TEMP topic, one whose topic is empty, and
        a gateway the discovery must not take for a node."""
        prefix = NATIVE_PREFIX.format(centre="fr-meteofrance")
        objects = [
            (f"{prefix}WIGOS_0-20000-0-07110_20261110T014000.bufr4", "2026-11-10T01:41:00.000Z", 40),
            (f"{prefix}WIGOS_0-20000-0-07145_20261110T004600.bufr4", "2026-11-10T00:47:00.000Z", 30),
            (f"{prefix}WIGOS_0-20000-0-07145_20261106T004600.bufr4", "2026-11-06T00:47:00.000Z", 30),
        ]
        bodies = {key: b"x" * size for key, _, size in objects}
        requested: list[str] = []

        def get(url: str) -> bytes:
            requested.append(url)
            if "list-type=2" in url:
                if "prefix=data%2F&" in url or url.endswith("prefix=data%2F"):
                    if discovery_fails:
                        raise DownloadError("HTTP Error 503: Service Unavailable")
                    return self.listing(
                        prefixes=["data/fr-meteofrance/", "data/kr-kma/", "data/jp-jma-gts-to-wis2/"]
                    )
                if "fr-meteofrance" in url:
                    return self.listing(objects)
                return self.listing()
            key = url[len(BUCKET_BASE_URL) :].replace("%2F", "/")
            return bodies[key]

        return get, requested

    def test_the_selector_fetches_each_node_with_a_topic(self) -> None:
        get, requested = self.bucket()
        issue = utc(2026, 11, 10, 2)
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch)
            results = fetch_sources(raw, issue, ("wis2",), now=utc(2026, 11, 10, 2, 15), get=get)
            self.assertEqual(list(results), ["wis2:fr-meteofrance"])
            result = results["wis2:fr-meteofrance"]
            # A node with no watermark is taken back 48 hours, not to the
            # start of its topic.
            self.assertEqual(
                sorted(result.files),
                ["WIGOS_0-20000-0-07110_20261110T014000.bufr4", "WIGOS_0-20000-0-07145_20261110T004600.bufr4"],
            )
            self.assertEqual(result.arrived["WIGOS_0-20000-0-07110_20261110T014000.bufr4"], "2026-11-10T01:41:00Z")
            root = issue_raw_directory(raw, issue)
            self.assertTrue((root / "wis2" / "fr-meteofrance" / "fetch.json").is_file())
            self.assertFalse((root / "wis2" / "kr-kma").exists())
            self.assertFalse((root / "wis2" / "fetch.json").exists())
            self.assertFalse(any("jp-jma-gts-to-wis2" in url for url in requested[1:]))
            self.assertEqual(fetched_sources(root, DEFAULT_SOURCES), (*SOURCE_IDS, "wis2:fr-meteofrance", "meteofrance"))
            record = read_fetch_record(root, "wis2:fr-meteofrance")
            assert record is not None
            self.assertEqual(record.arrived, result.arrived)
            self.assertNotIn("arrived", record.status.to_json())

    def test_a_failed_discovery_is_recorded_under_the_selector(self) -> None:
        get, _ = self.bucket(discovery_fails=True)
        issue = utc(2026, 11, 10, 2)
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch)
            results = fetch_sources(raw, issue, ("wis2",), now=utc(2026, 11, 10, 2, 15), get=get)
            self.assertFalse(results["wis2"].status.ok)
            self.assertIn("discovery", results["wis2"].status.error or "")
            root = issue_raw_directory(raw, issue)
            self.assertEqual(fetched_sources(root, ("wis2",)), ("wis2",))

    def test_a_node_without_a_watermark_is_bounded_by_the_lookback(self) -> None:
        objects = [
            RemoteObject("data/old", utc(2026, 11, 7, 0), 1),
            RemoteObject("data/new", utc(2026, 11, 9, 12), 1),
        ]
        wanted = objects_to_fetch(objects, None, now=utc(2026, 11, 10, 2), lookback=timedelta(hours=48))
        self.assertEqual([item.key for item in wanted], ["data/new"])

    def test_the_selector_is_not_a_listable_source(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SoundingProductError):
                fetch_gateway("wis2", Path(scratch), watermark=None)
            with self.assertRaises(SoundingProductError):
                fetch_gateway("wis2:Not_A_Node", Path(scratch), watermark=None)


def meteofrance_object(
    direction: str,
    launched: str,
    levels: list[tuple[int, int, float, float, int, float, int]],
    *,
    wmo: str = "07145",
) -> dict[str, object]:
    """One object of a Météo-France file, its levels as (pressure Pa,
    height m, T K, Td K, direction, speed m/s, seconds since launch)."""
    names = ("p_niv", "geop", "t", "td", "dd", "ff", "dur_dep", "dep_lat", "dep_lon")
    return {
        "metadata": {
            "date": launched,
            "station": {"geo_id_wmo": wmo, "name": "TRAPPES", "lat": 48.774333, "lon": 2.009833, "alti": 167},
            "direction": direction,
            "rs_type": 177,
            "ht_sta_mer": 168,
        },
        "levels": {
            "properties": [{"name": name, "index": index} for index, name in enumerate(names)],
            "nb_levels": len(levels),
            "observations": [[*level, 0.0, 0.0] for level in levels],
        },
    }


def climbing(top_pa: int = 1000, step_pa: int = 50, start_s: int = 0) -> list[tuple[int, int, float, float, int, float, int]]:
    """A dense synthetic ascent from 1000 hPa: a level every ``step_pa``,
    two seconds apart."""
    levels = []
    for number, pressure in enumerate(range(100000, top_pa - 1, -step_pa)):
        levels.append((pressure, number * 5, 288.0 - number * 0.01, 280.0 - number * 0.01, 270, 5.0, start_s + 2 * number))
    return levels


class MeteoFranceTests(unittest.TestCase):
    PAGE = (
        '<table id="list"><tbody><tr><td><a href="../">..</a></td></tr>'
        '<tr>\n<td><a href="/meteofrance/data/OBS/RS/2026/11/11/07145.2026111100.json">07145.2026111100.json</a></td>\n'
        "<td>417457</td>\n<td>2026-11-11T01:40:14.000Z</td>\n</tr>"
        '<tr><td><a href="/x/README.txt">README.txt</a></td><td>12</td><td>2026-11-11T01:00:00.000Z</td></tr>'
        "</tbody></table>"
    )

    def test_a_day_index_lists_its_sounding_files(self) -> None:
        files = meteofrance.parse_listing(self.PAGE, "2026/11/11/")
        self.assertEqual(
            files,
            [meteofrance.ListedFile("2026/11/11/07145.2026111100.json", utc(2026, 11, 11, 1, 40, 14), 417457)],
        )
        self.assertEqual(meteofrance.parse_listing("<h1>No Files Available for Listing</h1>", "2026/11/12/"), [])

    def test_the_window_lists_the_days_it_touches(self) -> None:
        self.assertEqual(
            [str(day) for day in meteofrance.days(utc(2026, 11, 11, 6, 15))],
            ["2026-11-09", "2026-11-10", "2026-11-11"],
        )

    def test_the_ascent_is_told_by_its_levels_not_its_label(self) -> None:
        ascent = meteofrance_object("downward", "2026-11-10T11:15:35Z", climbing())
        # A descent: its clock starts at the burst, at the top.
        descent = meteofrance_object(
            "upward",
            "2026-11-10T12:50:10Z",
            [(18770, 12271, 213.88, 191.57, 255, 22.2, 698), (1003, 30310, 221.69, 189.58, 268, 68.1, 0)],
        )
        data = json.dumps([descent, ascent]).encode()
        result = meteofrance.parse_file(data, name="07145.2026111012.json", arrived=utc(2026, 11, 10, 13, 40))
        self.assertEqual((result.ascents, result.descents, result.dropped), (1, 1, 0))
        (sounding,) = result.soundings
        self.assertEqual(sounding.p[0], 100000)
        self.assertEqual(sounding.time, utc(2026, 11, 10, 12))
        self.assertEqual(sounding.launched, utc(2026, 11, 10, 11, 15, 35))
        self.assertEqual((sounding.key, sounding.wmo, sounding.wigos), ("07145", "07145", None))
        self.assertEqual((sounding.gateway, sounding.bulletin, sounding.sonde, sounding.elev), ("meteofrance", "07145.2026111012.json", 177, 168.0))
        self.assertEqual(sounding.arrived, utc(2026, 11, 10, 13, 40))

    def test_the_launch_clock_files_the_ascent_not_the_batch(self) -> None:
        # Rapa's 18Z ascent goes out in the next day's 00 batch.
        data = json.dumps([meteofrance_object("upward", "2026-11-10T17:39:39Z", climbing(), wmo="91958"), {}]).encode()
        result = meteofrance.parse_file(data, name="91958.2026111100.json", arrived=utc(2026, 11, 11, 1, 40))
        self.assertEqual(result.soundings[0].time, utc(2026, 11, 10, 18))
        self.assertEqual(result.descents, 0)

    def test_the_standard_surfaces_survive_the_thinning_unflagged(self) -> None:
        data = json.dumps([meteofrance_object("upward", "2026-11-10T23:15:00Z", climbing(step_pa=30)), {}]).encode()
        (sounding,) = meteofrance.parse_file(data, name="07145.2026111100.json", arrived=utc(2026, 11, 11, 1, 40)).soundings
        self.assertGreater(sounding.reported, 3000)
        self.assertLess(sounding.n, 200)
        # The level nearest each surface is kept as measured, within a
        # hectopascal: 30 Pa steps from 1000 hPa put one exactly on 850
        # hPa and the nearest to 500 hPa at 499.9.
        for surface in (85000, 70000, 50000, 25000):
            self.assertLessEqual(min(abs(pressure - surface) for pressure in sounding.p), 15)
        self.assertIn(85000, sounding.p)
        self.assertIn(49990, sounding.p)
        self.assertTrue(all(flag == bufr.MISSING for flag in sounding.sig))
        levels = {"p": list(sounding.p), "z": list(sounding.z), "t": list(sounding.t), "td": list(sounding.td), "sig": list(sounding.sig)}
        self.assertIsNotNone(derive.headline(levels, derive.derive(levels))["t500"])

    def test_a_relaunch_is_settled_by_the_longer_ascent(self) -> None:
        lost = meteofrance_object("upward", "2026-11-10T11:15:33Z", climbing(top_pa=86000))
        relaunch = meteofrance_object("downward", "2026-11-10T11:41:14Z", climbing())
        result = meteofrance.parse_file(
            json.dumps([lost, relaunch]).encode(), name="07761.2026111012.json", arrived=utc(2026, 11, 10, 13, 40)
        )
        self.assertEqual(result.ascents, 2)
        (kept,) = bufr.deduplicate(list(result.soundings))
        self.assertEqual(kept.launched, utc(2026, 11, 10, 11, 41, 14))

    def test_a_fragment_of_one_level_is_dropped(self) -> None:
        fragment = meteofrance_object("upward", "2026-11-09T12:53:32Z", [(66188, 3500, 270.0, 260.0, 250, 10.0, 1072)])
        result = meteofrance.parse_file(json.dumps([fragment, {}]).encode(), name="61998.2026110912.json", arrived=utc(2026, 11, 9, 13, 40))
        self.assertEqual((result.soundings, result.ascents, result.dropped), ((), 1, 1))

    def test_a_file_that_is_not_the_documented_array_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            meteofrance.parse_file(b"<html>", name="07145.2026111100.json", arrived=utc(2026, 11, 11))
        with self.assertRaises(ValueError):
            meteofrance.parse_file(b'"text"', name="07145.2026111100.json", arrived=utc(2026, 11, 11))

    def site(self, files: dict[str, tuple[str, bytes]]):
        """Météo-France's directory pages and files, ``files`` keyed by
        path under the base URL with their modification time."""
        requested: list[str] = []

        def get(url: str) -> bytes:
            requested.append(url)
            path = url[len(meteofrance.BASE_URL) :]
            if path.endswith("/"):
                rows = "".join(
                    f'<tr><td><a href="/meteofrance/data/OBS/RS/{key}">{key.rsplit("/", 1)[1]}</a></td>'
                    f"<td>{len(body)}</td><td>{modified}</td></tr>"
                    for key, (modified, body) in files.items()
                    if key.startswith(path)
                )
                return f"<table><tbody>{rows}</tbody></table>".encode()
            return files[path][1]

        return get, requested

    def test_the_source_fetches_the_window_and_records_arrivals(self) -> None:
        body = json.dumps([meteofrance_object("upward", "2026-11-10T23:15:00Z", climbing()), {}]).encode()
        get, requested = self.site(
            {
                "2026/11/08/07145.2026110812.json": ("2026-11-08T13:40:00.000Z", body),
                "2026/11/10/07145.2026111012.json": ("2026-11-10T13:40:00.000Z", body),
                "2026/11/11/07145.2026111100.json": ("2026-11-11T01:40:14.000Z", body),
            }
        )
        issue = utc(2026, 11, 11, 6)
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch)
            results = fetch_sources(raw, issue, ("meteofrance",), now=utc(2026, 11, 11, 6, 15), get=get)
            result = results["meteofrance"]
            self.assertTrue(result.status.ok)
            self.assertEqual(result.files, ["07145.2026111012.json", "07145.2026111100.json"])
            self.assertEqual(result.arrived["07145.2026111100.json"], "2026-11-11T01:40:14Z")
            self.assertEqual(result.status.watermark, utc(2026, 11, 11, 1, 40, 14))
            self.assertEqual(result.status.url, meteofrance.BASE_URL)
            self.assertNotIn(meteofrance.BASE_URL + "2026/11/08/", requested)
            root = issue_raw_directory(raw, issue)
            self.assertTrue((root / "meteofrance" / "07145.2026111100.json").is_file())

            # An old watermark is still trusted: the files are kept for good,
            # so nothing behind it has gone.
            later = utc(2026, 11, 13, 6)
            results = fetch_sources(
                raw, later, ("meteofrance",), watermarks={"meteofrance": utc(2026, 11, 11, 1, 40, 14)}, now=utc(2026, 11, 13, 6, 15), get=get
            )
            self.assertEqual(results["meteofrance"].files, [])

    def test_the_build_reads_the_files_it_fetched(self) -> None:
        body = json.dumps([meteofrance_object("upward", "2026-11-10T23:15:00Z", climbing(step_pa=30)), {}]).encode()
        get, _ = self.site({"2026/11/11/07145.2026111100.json": ("2026-11-11T01:40:14.000Z", body)})
        issue = utc(2026, 11, 11, 2)
        with tempfile.TemporaryDirectory() as scratch:
            raw, output = Path(scratch) / "raw", Path(scratch) / "out"
            fetch_sources(raw, issue, ("meteofrance",), now=utc(2026, 11, 11, 2, 15), get=get)
            report = build_product(issue, raw, output, sources=("meteofrance",), now=utc(2026, 11, 11, 2, 16))
            (status,) = report["sources"]
            self.assertEqual((status["id"], status["ok"], status["soundings"]), ("meteofrance", True, 1))
            self.assertEqual(report["watermark"], {"meteofrance": "2026-11-11T01:40:14Z"})
            index = json.loads(Path(report["directory"], "index.json").read_bytes())
            (station,) = index["stations"]
            self.assertEqual((station["id"], station["latest"]), ("0-20000-0-07145", "2026-11-11T00:00:00Z"))
            self.assertIsNotNone(station["headline"]["t500"])

    def test_objects_with_no_retention_trust_an_old_watermark(self) -> None:
        objects = [RemoteObject("a", utc(2026, 11, 9, 13, 40), 1), RemoteObject("b", utc(2026, 11, 11, 1, 40), 1)]
        wanted = objects_to_fetch(objects, utc(2026, 11, 10, 1, 40), now=utc(2026, 11, 12, 6), retention=None)
        self.assertEqual([item.key for item in wanted], ["b"])
        wanted = objects_to_fetch(objects, utc(2026, 11, 10, 1, 40), now=utc(2026, 11, 12, 6))
        self.assertEqual([item.key for item in wanted], ["a", "b"])


class ValidationTests(unittest.TestCase):
    def station(self) -> dict[str, object]:
        """The first line of the golden's soundings file."""
        line = (EXPECTED / SOUNDINGS_FILENAME).read_bytes().split(b"\n")[0]
        return json.loads(line)

    def index(self) -> dict[str, object]:
        return json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))

    def test_the_golden_files_validate(self) -> None:
        validate_station_line(self.station())
        validate_index(self.index())

    def test_a_station_with_a_short_array_is_refused(self) -> None:
        payload = self.station()
        payload["soundings"][0]["t"] = payload["soundings"][0]["t"][:-1]
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)

    def test_pressure_must_be_descending_and_present(self) -> None:
        payload = self.station()
        payload["soundings"][0]["p"] = sorted(payload["soundings"][0]["p"])
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)
        payload = self.station()
        payload["soundings"][0]["p"][0] = bufr.MISSING
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)

    def test_an_out_of_range_value_is_refused_but_missing_is_not(self) -> None:
        payload = self.station()
        payload["soundings"][0]["wd"][0] = 400
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)
        payload = self.station()
        payload["soundings"][0]["wd"][0] = bufr.MISSING
        validate_station_line(payload)

    def test_a_malformed_station_id_is_refused(self) -> None:
        payload = self.station()
        payload["id"] = "47401"
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)
        payload = self.station()
        payload["id"] = "../etc/passwd"
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)

    def test_a_line_needs_at_least_one_sounding(self) -> None:
        payload = self.station()
        payload["soundings"] = []
        with self.assertRaises(SoundingProductError):
            validate_station_line(payload)

    def test_an_index_schema_version_bump_is_refused(self) -> None:
        payload = self.index()
        payload["schemaVersion"] = 2
        with self.assertRaises(SoundingProductError):
            validate_index(payload)

    def test_the_index_must_be_sorted_by_id_and_name_its_files(self) -> None:
        payload = self.index()
        payload["stations"] = list(reversed(payload["stations"]))
        with self.assertRaises(SoundingProductError):
            validate_index(payload)
        payload = self.index()
        payload["soundings"]["path"] = "elsewhere.jsonl"
        with self.assertRaises(SoundingProductError):
            validate_index(payload)
        payload = self.index()
        payload["soundings"]["crc32"] = "NOTHEX12"
        with self.assertRaises(SoundingProductError):
            validate_index(payload)

    def test_the_index_times_are_newest_first_and_agree_with_latest(self) -> None:
        payload = self.index()
        payload["stations"][0]["latest"] = "2026-09-13T12:00:00Z"
        with self.assertRaises(SoundingProductError):
            validate_index(payload)

    def test_a_failed_source_must_say_why(self) -> None:
        payload = self.index()
        del payload["sources"][1]["error"]
        with self.assertRaises(SoundingProductError):
            validate_index(payload)

    def test_an_unknown_gateway_id_is_admitted(self) -> None:
        payload = self.index()
        payload["watermark"]["wis2:jp-jma"] = "2026-09-14T02:00:00Z"
        payload["sources"].append({"id": "wis2:jp-jma", "ok": True})
        validate_index(payload)
        payload["sources"].append({"id": "Not An Id", "ok": True})
        with self.assertRaises(SoundingProductError):
            validate_index(payload)

    def test_the_pointer_names_the_issued_hour(self) -> None:
        index = encode_json({"schemaVersion": 1})
        pointer = build_pointer(ISSUE, "sounding/2026/09/14/sounding.2026091402/index.json", index)
        self.assertEqual(pointer["crc32"], crc32_hex(index))
        self.assertEqual(pointer["product"], "sounding")
        # An issue published before the archive tree sat flat under the root.
        validate_pointer({**pointer, "path": "sounding.2026091402/index.json"})
        for path in (
            "sounding.2026091403/index.json",
            "sounding/2026/09/14/sounding.2026091403/index.json",
            "sounding/2026/10/14/sounding.2026091402/index.json",
            "sounding/2026/09/14/sounding.2026091402/x/index.json",
            "/sounding.2026091402/index.json",
        ):
            with self.assertRaises(SoundingProductError):
                validate_pointer({**pointer, "path": path})
        with self.assertRaises(SoundingProductError):
            parse_issue("2026091402Z")


class BuildFailureTests(unittest.TestCase):
    def test_a_gateway_whose_bulletins_all_fail_is_a_failed_source(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch) / "raw" / "sounding.2026091402" / GATEWAY
            raw.mkdir(parents=True)
            (raw / "fetch.json").write_text(
                json.dumps(
                    {
                        "id": GATEWAY,
                        "ok": True,
                        "fetched": "2026-09-14T02:04:00Z",
                        "watermark": "2026-09-14T02:34:41Z",
                        "files": [RJTD],
                    }
                )
            )
            report = build_product(
                ISSUE,
                Path(scratch) / "raw",
                Path(scratch) / "out",
                sources=(GATEWAY,),
                force=True,
                now=utc(2026, 9, 14, 2, 5),
            )
            status = report["sources"][0]
            self.assertFalse(status["ok"])
            self.assertTrue(status["error"].startswith("parse: "))
            # The watermark is not advanced past bulletins nothing read.
            self.assertIsNone(report["watermark"][GATEWAY])
            self.assertEqual(report["stations"], 0)
            self.assertIsNone(report["pointer"])

    def test_a_nil_bulletin_is_counted_apart_from_what_fails_to_decode(self) -> None:
        # The gateways republish the GTS stream verbatim, so the sounding
        # subtree also carries plain-text "nothing to report" bulletins.
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch) / "raw" / "sounding.2026091402" / GATEWAY
            raw.mkdir(parents=True)
            nil = "A_IUSN02DAMM140000_C_EDZW_20260914004803_51548070"
            (raw / nil).write_bytes(b"IUSN02 DAMM 140000\r\r\n\r\r\nNIL=")
            (raw / "fetch.json").write_text(
                json.dumps(
                    {
                        "id": GATEWAY,
                        "ok": True,
                        "fetched": "2026-09-14T02:04:00Z",
                        "watermark": "2026-09-14T02:34:41Z",
                        "files": [nil],
                    }
                )
            )
            report = build_product(
                ISSUE,
                Path(scratch) / "raw",
                Path(scratch) / "out",
                sources=(GATEWAY,),
                force=True,
                now=utc(2026, 9, 14, 2, 5),
            )
            status = report["sources"][0]
            # Nothing decoded, but nothing failed either: the source is ok
            # and its watermark advances.
            self.assertTrue(status["ok"])
            self.assertEqual((status["nil"], status["unreadable"], status["bulletins"]), (1, 0, 0))
            self.assertEqual(report["watermark"][GATEWAY], "2026-09-14T02:34:41Z")

    def test_a_gateway_that_was_never_fetched_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            report = build_product(
                ISSUE, Path(scratch) / "raw", Path(scratch) / "out", force=True, now=utc(2026, 9, 14, 2, 5)
            )
            # The gateways and Météo-France; the node selector expands into
            # nothing when its discovery never ran.
            self.assertEqual([status["id"] for status in report["sources"]], [*SOURCE_IDS, "meteofrance"])
            self.assertEqual([status["error"] for status in report["sources"]], ["not fetched"] * 3)
            self.assertIsNone(report["pointer"])


class StationNameTableTests(unittest.TestCase):
    """``xuebuild/sounding/stations.json`` holds the cleaned names
    (``docs/sounding.md`` §3), so its hygiene is checked here rather than
    trusted to the one-off cleaning that built it."""

    def test_the_table_is_keyed_by_wmo_number_and_names_are_presentable(self) -> None:
        names = station_names()
        self.assertGreater(len(names), 900)
        for wmo, name in names.items():
            with self.subTest(wmo=wmo):
                self.assertRegex(wmo, r"^\d{5}$")
                self.assertEqual(name, name.strip())
                self.assertNotRegex(name, r"\s{2}|[;,]$|\(\d+-\d\)|\((?:RS|AERO|UA)\)")
                # Title case: a run of five capitals is the list's
                # shouting, not an acronym.
                self.assertNotRegex(name, r"[A-Z]{5}")

    def test_a_station_the_table_lacks_has_no_name(self) -> None:
        self.assertEqual(station_name("47678"), "Hachijojima")
        self.assertIsNone(station_name(None))
        self.assertIsNone(station_name("00000"))


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
class GoldenBuildTests(unittest.TestCase):
    def build(self, output: Path) -> dict[str, object]:
        return build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))

    def test_build_matches_the_golden(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            report = self.build(output)
            directory = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402"
            self.assertEqual(
                sorted(path.name for path in directory.iterdir()),
                ["index.json", "item.json", SOUNDINGS_FILENAME],
            )
            self.assertEqual(
                json.loads((directory / "index.json").read_bytes()),
                json.loads((EXPECTED / "index.json").read_text(encoding="utf-8")),
            )
            # The issue's STAC Item beside the index, derived from it
            # (docs/stac.md); the Collection and the live Item follow the
            # pointer and are held by tests/test_stac.py.
            self.assertEqual(
                json.loads((directory / "item.json").read_bytes()),
                json.loads((EXPECTED / "item.json").read_text(encoding="utf-8")),
            )
            # The soundings file is committed exactly as published, so the
            # golden pins the bytes the index's offsets point into.
            self.assertEqual(
                (directory / SOUNDINGS_FILENAME).read_bytes(),
                (EXPECTED / SOUNDINGS_FILENAME).read_bytes(),
            )
            pointer = json.loads((output / "latest-sounding.json").read_bytes())
            self.assertEqual(pointer, json.loads((EXPECTED / "latest-sounding.json").read_text(encoding="utf-8")))
            index_bytes = (directory / "index.json").read_bytes()
            self.assertEqual((pointer["byteLength"], pointer["crc32"]), (len(index_bytes), crc32_hex(index_bytes)))
            self.assertEqual((report["stations"], report["fresh"], report["copied"]), (20, 20, 0))
            statuses = {status["id"]: status for status in report["sources"]}
            self.assertTrue(statuses["jp-jma-gts-to-wis2"]["ok"])
            self.assertFalse(statuses["de-dwd-gts-to-wis2"]["ok"])
            self.assertIn("503", statuses["de-dwd-gts-to-wis2"]["error"])
            # Météo-France's Rapa file of the 00 batch holds the 18Z ascent
            # of the day before, filed by its launch clock.
            self.assertTrue(statuses["meteofrance"]["ok"])
            rapa = next(row for row in json.loads((directory / "index.json").read_bytes())["stations"] if row["wmo"] == "91958")
            self.assertEqual(rapa["times"], ["2026-09-13T18:00:00Z"])

    def test_every_span_slices_out_its_own_station(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            self.build(output)
            directory = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402"
            index = json.loads((directory / "index.json").read_bytes())
            blob = (directory / SOUNDINGS_FILENAME).read_bytes()
            descriptor = index["soundings"]
            self.assertEqual(descriptor["path"], SOUNDINGS_FILENAME)
            self.assertEqual((descriptor["byteLength"], descriptor["crc32"]), (len(blob), crc32_hex(blob)))
            for entry in index["stations"]:
                with self.subTest(station=entry["id"]):
                    span = blob[entry["offset"] : entry["offset"] + entry["length"]]
                    # The span excludes the newline, so it parses alone —
                    # which is what a Range request hands a reader.
                    station = json.loads(span)
                    self.assertEqual(station["id"], entry["id"])
                    self.assertEqual(station["lat"], entry["lat"])
                    self.assertEqual([s["time"] for s in station["soundings"]], entry["times"])
                    self.assertEqual(blob[entry["offset"] + entry["length"] : entry["offset"] + entry["length"] + 1], b"\n")
                    validate_station_line(station, entry["id"])

    def test_the_spans_are_contiguous_and_cover_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            self.build(output)
            directory = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402"
            index = json.loads((directory / "index.json").read_bytes())
            blob = (directory / SOUNDINGS_FILENAME).read_bytes()
            offset = 0
            for entry in index["stations"]:
                self.assertEqual(entry["offset"], offset)
                offset += entry["length"] + 1
            self.assertEqual(offset, len(blob))
            self.assertEqual(sum(e["length"] + 1 for e in index["stations"]), index["soundings"]["byteLength"])
            self.assertEqual(len(blob.splitlines()), len(index["stations"]))

    def test_the_stations_are_sorted_and_one_line_each(self) -> None:
        blob = (EXPECTED / SOUNDINGS_FILENAME).read_bytes()
        ids = [json.loads(line)["id"] for line in blob.splitlines()]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 20)

    def test_the_watermark_carries_the_working_gateway_and_leaves_the_failed_one(self) -> None:
        index = json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["watermark"]["jp-jma-gts-to-wis2"], "2026-09-14T02:34:41Z")
        self.assertIsNone(index["watermark"]["de-dwd-gts-to-wis2"])

    def test_a_previous_issue_in_the_flat_layout_is_carried_forward(self) -> None:
        # The first build after the archive tree finds the live issue where
        # it was published before it, directly under the root.
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            archived = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402"
            flat = output / "sounding.2026091402"
            shutil.move(archived, flat)
            previous = json.loads((flat / "index.json").read_bytes())
            report = build_product(
                parse_issue("2026091403"),
                Path(scratch) / "empty-raw",
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (0, 20))
            self.assertEqual(
                (flat / SOUNDINGS_FILENAME).read_bytes(),
                (output / "sounding" / "2026" / "09" / "14" / "sounding.2026091403" / SOUNDINGS_FILENAME).read_bytes(),
            )

    def test_an_unchanged_station_is_copied_forward_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402" / "index.json").read_bytes())
            later = parse_issue("2026091403")
            report = build_product(
                later,
                Path(scratch) / "empty-raw",
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (0, 20))
            self.assertIsNotNone(report["pointer"])
            # Nothing new arrived, so the file is rewritten identically —
            # every station's line and its span included.
            self.assertEqual(
                (output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402" / SOUNDINGS_FILENAME).read_bytes(),
                (output / "sounding" / "2026" / "09" / "14" / "sounding.2026091403" / SOUNDINGS_FILENAME).read_bytes(),
            )
            index = json.loads((output / "sounding" / "2026" / "09" / "14" / "sounding.2026091403" / "index.json").read_bytes())
            self.assertEqual(
                [(e["id"], e["offset"], e["length"]) for e in index["stations"]],
                [(e["id"], e["offset"], e["length"]) for e in previous["stations"]],
            )
            self.assertEqual(index["soundings"], previous["soundings"])
            # The failed gateway keeps the watermark it had; nothing new
            # arrived to move it.
            self.assertEqual(index["watermark"], previous["watermark"])

    def test_the_same_bulletins_an_hour_later_rewrite_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            raw = Path(scratch) / "raw"
            shutil.copytree(RAW, raw / "sounding.2026091403")
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402" / "index.json").read_bytes())
            report = build_product(
                parse_issue("2026091403"),
                raw,
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (20, 0))
            self.assertEqual(
                (output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402" / SOUNDINGS_FILENAME).read_bytes(),
                (output / "sounding" / "2026" / "09" / "14" / "sounding.2026091403" / SOUNDINGS_FILENAME).read_bytes(),
            )

    def test_a_station_that_stops_reporting_drops_out_after_two_days(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402" / "index.json").read_bytes())
            stale = parse_issue("2026091702")
            report = build_product(
                stale,
                Path(scratch) / "empty-raw",
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 17, 2, 5),
            )
            self.assertEqual((report["stations"], report["copied"]), (0, 0))
            self.assertIsNone(report["pointer"])

    def test_stations_are_named_by_their_wmo_number(self) -> None:
        index = json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))
        names = {row["id"]: row["name"] for row in index["stations"]}
        self.assertEqual(names["0-20000-0-47401"], "Wakkanai")
        # A station published under its native WIGOS id is named by its
        # WMO number all the same.
        self.assertEqual(names["0-20001-0-51463"], "Wu Lu Mu Qi")
        for line in (EXPECTED / SOUNDINGS_FILENAME).read_bytes().splitlines():
            station = json.loads(line)
            self.assertEqual(station["name"], names[station["id"]])

    def test_a_line_carried_forward_takes_the_table_s_name(self) -> None:
        # An issue written before the names were filled carries nulls; the
        # next hour names its stations though nothing new arrived for them.
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            directory = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091402"
            lines = [json.loads(line) | {"name": None} for line in (directory / SOUNDINGS_FILENAME).read_bytes().splitlines()]
            (directory / SOUNDINGS_FILENAME).write_bytes(b"".join(encode_json(line) + b"\n" for line in lines))
            previous = json.loads((directory / "index.json").read_bytes())
            report = build_product(
                parse_issue("2026091403"),
                Path(scratch) / "empty-raw",
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (0, 20))
            later = output / "sounding" / "2026" / "09" / "14" / "sounding.2026091403"
            rows = {row["id"]: row["name"] for row in json.loads((later / "index.json").read_bytes())["stations"]}
            self.assertEqual(rows["0-20000-0-47401"], "Wakkanai")
            self.assertEqual(
                (later / SOUNDINGS_FILENAME).read_bytes(),
                (EXPECTED / SOUNDINGS_FILENAME).read_bytes(),
            )

    def test_rebuilding_an_issue_needs_force(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            with self.assertRaises(SoundingProductError):
                build_product(ISSUE, SOUNDING_FIXTURES, output, now=utc(2026, 9, 14, 2, 5))


if __name__ == "__main__":
    unittest.main()
