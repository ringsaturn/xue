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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from xuebuild.errors import DownloadError, SoundingProductError
from xuebuild.sounding import bufr, derive
from xuebuild.sounding.build import KEEP_TIMES, build_product, merge_soundings
from xuebuild.sounding.fetch import (
    BUCKET_BASE_URL,
    GATEWAY_PREFIX,
    SOURCE_IDS,
    RemoteObject,
    fetch_gateway,
    list_objects,
    objects_to_fetch,
)
from xuebuild.sounding.schema import (
    build_pointer,
    crc32_hex,
    encode_json,
    parse_issue,
    validate_index,
    validate_pointer,
    validate_station,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOUNDING_FIXTURES = FIXTURES / "sounding"
RAW = SOUNDING_FIXTURES / "sounding.2026091402"
EXPECTED = SOUNDING_FIXTURES / "expected"
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
        self.assertEqual(urumqi.n, 166)
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

    def test_a_level_whose_pressure_is_out_of_range_is_dropped(self) -> None:
        entries = self.header() + self.level(0.0, t=283.15) + self.level(100000.0, t=284.15)
        item = self.parse(entries).soundings[0]
        self.assertEqual(item.p, (100000,))
        self.assertEqual(item.t, (28415,))

    def test_the_parser_never_emits_what_the_validator_refuses(self) -> None:
        self.assertEqual(sorted(bufr.VALUE_BOUNDS), sorted(("p", "z", "t", "td", "wd", "ws", "sig")))

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

    def test_merge_keeps_the_newest_four_nominal_times(self) -> None:
        entries = [
            {"time": f"2026-09-{day:02d}T00:00:00Z", "n": 10, "arrived": "2026-09-14T01:00:00Z"}
            for day in range(8, 15)
        ]
        merged = merge_soundings(entries)
        self.assertEqual(len(merged), KEEP_TIMES)
        self.assertEqual(merged[0]["time"], "2026-09-14T00:00:00Z")
        self.assertEqual(merged[-1]["time"], "2026-09-11T00:00:00Z")

    def test_merge_prefers_the_longer_sounding_then_the_later_arrival(self) -> None:
        short = {"time": "2026-09-14T00:00:00Z", "n": 15, "arrived": "2026-09-14T03:00:00Z"}
        long = {"time": "2026-09-14T00:00:00Z", "n": 186, "arrived": "2026-09-14T01:00:00Z"}
        self.assertEqual(merge_soundings([short, long]), [long])
        early = {"time": "2026-09-14T00:00:00Z", "n": 186, "arrived": "2026-09-14T00:30:00Z"}
        self.assertEqual(merge_soundings([early, long]), [long])


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

    def test_the_listing_follows_the_continuation_token(self) -> None:
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        pages = {
            f"{BUCKET_BASE_URL}?list-type=2&prefix={prefix.replace('/', '%2F')}": self.page(
                [(f"{prefix}A/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755)], token="next+token"
            ),
        }

        requested: list[str] = []

        def get(url: str) -> bytes:
            requested.append(url)
            if "continuation-token" in url:
                return self.page([(f"{prefix}N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636)])
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

    def test_the_sources_are_the_two_gateways(self) -> None:
        self.assertEqual(SOURCE_IDS, ("jp-jma-gts-to-wis2", "de-dwd-gts-to-wis2"))

    def gateway(self, scratch: Path, watermark: datetime | None, **kwargs: object):
        prefix = GATEWAY_PREFIX.format(gateway=GATEWAY)
        objects = [
            (f"{prefix}C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755),
            (f"{prefix}N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636),
            (f"{prefix}N/01/BABJ/20260914005246_38.04896", "2026-09-14T02:35:00.000Z", 99),
        ]
        bodies = {key: b"x" * size for key, _, size in objects}

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.page(objects)
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
            (f"{prefix}C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755),
            (f"{prefix}N/01/BABJ/{BABJ}", "2026-09-14T02:34:41.000Z", 51636),
        ]

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.page(objects)
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
        objects = [(f"{prefix}C/01/RJTD/{RJTD}", "2026-09-14T01:24:01.000Z", 1755)]

        def get(url: str) -> bytes:
            if "list-type=2" in url:
                return self.page(objects)
            raise DownloadError("HTTP Error 503: Service Unavailable")

        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(DownloadError):
                fetch_gateway(GATEWAY, Path(scratch), watermark=None, now=utc(2026, 9, 14, 3), get=get)

    def test_an_unknown_gateway_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SoundingProductError):
                fetch_gateway("somewhere-else", Path(scratch), watermark=None)


class ValidationTests(unittest.TestCase):
    def station(self) -> dict[str, object]:
        return json.loads((EXPECTED / "0-20000-0-47401.json").read_text(encoding="utf-8"))

    def index(self) -> dict[str, object]:
        return json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))

    def test_the_golden_files_validate(self) -> None:
        validate_station(self.station())
        validate_index(self.index())

    def test_a_station_with_a_short_array_is_refused(self) -> None:
        payload = self.station()
        payload["soundings"][0]["t"] = payload["soundings"][0]["t"][:-1]
        with self.assertRaises(SoundingProductError):
            validate_station(payload)

    def test_pressure_must_be_descending_and_present(self) -> None:
        payload = self.station()
        payload["soundings"][0]["p"] = sorted(payload["soundings"][0]["p"])
        with self.assertRaises(SoundingProductError):
            validate_station(payload)
        payload = self.station()
        payload["soundings"][0]["p"][0] = bufr.MISSING
        with self.assertRaises(SoundingProductError):
            validate_station(payload)

    def test_an_out_of_range_value_is_refused_but_missing_is_not(self) -> None:
        payload = self.station()
        payload["soundings"][0]["wd"][0] = 400
        with self.assertRaises(SoundingProductError):
            validate_station(payload)
        payload = self.station()
        payload["soundings"][0]["wd"][0] = bufr.MISSING
        validate_station(payload)

    def test_a_malformed_station_id_is_refused(self) -> None:
        payload = self.station()
        payload["id"] = "47401"
        with self.assertRaises(SoundingProductError):
            validate_station(payload)
        payload = self.station()
        payload["id"] = "../etc/passwd"
        with self.assertRaises(SoundingProductError):
            validate_station(payload)

    def test_a_schema_version_bump_is_refused(self) -> None:
        payload = self.station()
        payload["schemaVersion"] = 2
        with self.assertRaises(SoundingProductError):
            validate_station(payload)

    def test_the_index_must_be_sorted_by_id_and_name_its_files(self) -> None:
        payload = self.index()
        payload["stations"] = list(reversed(payload["stations"]))
        with self.assertRaises(SoundingProductError):
            validate_index(payload)
        payload = self.index()
        payload["stations"][0]["path"] = "elsewhere.json"
        with self.assertRaises(SoundingProductError):
            validate_index(payload)
        payload = self.index()
        payload["stations"][0]["crc32"] = "NOTHEX12"
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
        pointer = build_pointer(ISSUE, "sounding.2026091402/index.json", index)
        self.assertEqual(pointer["crc32"], crc32_hex(index))
        self.assertEqual(pointer["product"], "sounding")
        with self.assertRaises(SoundingProductError):
            validate_pointer({**pointer, "path": "sounding.2026091403/index.json"})
        with self.assertRaises(SoundingProductError):
            validate_pointer({**pointer, "path": "/sounding.2026091402/index.json"})
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
            self.assertEqual([status["error"] for status in report["sources"]], ["not fetched"] * 2)
            self.assertIsNone(report["pointer"])


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
class GoldenBuildTests(unittest.TestCase):
    def test_build_matches_the_golden(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            report = build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            directory = output / "sounding.2026091402"
            built = {path.name: json.loads(path.read_bytes()) for path in directory.iterdir()}
            expected = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in EXPECTED.iterdir()
                if path.name != "latest-sounding.json"
            }
            self.assertEqual(sorted(built), sorted(expected))
            for name in sorted(expected):
                with self.subTest(file=name):
                    self.assertEqual(built[name], expected[name])
            index = built["index.json"]
            for entry in index["stations"]:
                payload = (directory / entry["path"]).read_bytes()
                self.assertEqual((len(payload), crc32_hex(payload)), (entry["byteLength"], entry["crc32"]))
            pointer = json.loads((output / "latest-sounding.json").read_bytes())
            self.assertEqual(pointer, json.loads((EXPECTED / "latest-sounding.json").read_text(encoding="utf-8")))
            index_bytes = (directory / "index.json").read_bytes()
            self.assertEqual((pointer["byteLength"], pointer["crc32"]), (len(index_bytes), crc32_hex(index_bytes)))
            self.assertEqual((report["stations"], report["fresh"], report["copied"]), (19, 19, 0))
            statuses = {status["id"]: status for status in report["sources"]}
            self.assertTrue(statuses["jp-jma-gts-to-wis2"]["ok"])
            self.assertFalse(statuses["de-dwd-gts-to-wis2"]["ok"])
            self.assertIn("503", statuses["de-dwd-gts-to-wis2"]["error"])

    def test_the_watermark_carries_the_working_gateway_and_leaves_the_failed_one(self) -> None:
        index = json.loads((EXPECTED / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["watermark"]["jp-jma-gts-to-wis2"], "2026-09-14T02:34:41Z")
        self.assertIsNone(index["watermark"]["de-dwd-gts-to-wis2"])

    def test_an_unchanged_station_is_copied_forward_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding.2026091402" / "index.json").read_bytes())
            later = parse_issue("2026091403")
            report = build_product(
                later,
                Path(scratch) / "empty-raw",
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (0, 19))
            self.assertIsNotNone(report["pointer"])
            for entry in previous["stations"]:
                first = (output / "sounding.2026091402" / entry["path"]).read_bytes()
                second = (output / "sounding.2026091403" / entry["path"]).read_bytes()
                with self.subTest(station=entry["id"]):
                    self.assertEqual(first, second)
            index = json.loads((output / "sounding.2026091403" / "index.json").read_bytes())
            self.assertEqual(
                [(e["id"], e["crc32"]) for e in index["stations"]],
                [(e["id"], e["crc32"]) for e in previous["stations"]],
            )
            # The failed gateway keeps the watermark it had; nothing new
            # arrived to move it.
            self.assertEqual(index["watermark"], previous["watermark"])

    def test_the_same_bulletins_an_hour_later_rewrite_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            raw = Path(scratch) / "raw"
            shutil.copytree(RAW, raw / "sounding.2026091403")
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding.2026091402" / "index.json").read_bytes())
            report = build_product(
                parse_issue("2026091403"),
                raw,
                output,
                previous_index=previous,
                force=True,
                now=utc(2026, 9, 14, 3, 5),
            )
            self.assertEqual((report["fresh"], report["copied"]), (19, 0))
            for entry in previous["stations"]:
                first = (output / "sounding.2026091402" / entry["path"]).read_bytes()
                second = (output / "sounding.2026091403" / entry["path"]).read_bytes()
                with self.subTest(station=entry["id"]):
                    self.assertEqual(first, second)

    def test_a_station_that_stops_reporting_drops_out_after_two_days(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            previous = json.loads((output / "sounding.2026091402" / "index.json").read_bytes())
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

    def test_rebuilding_an_issue_needs_force(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build_product(ISSUE, SOUNDING_FIXTURES, output, force=True, now=utc(2026, 9, 14, 2, 5))
            with self.assertRaises(SoundingProductError):
                build_product(ISSUE, SOUNDING_FIXTURES, output, now=utc(2026, 9, 14, 2, 5))


if __name__ == "__main__":
    unittest.main()
