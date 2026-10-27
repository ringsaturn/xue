"""The tropical cyclone archive (``docs/tc.md`` §8): the merge rules, the
month and season files and their indexes, the a-deck backfill reader and
the season-year rule, all offline on constructed input.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.errors import TcProductError
from xuebuild.tc import atcf
from xuebuild.tc.archive import (
    ARCHIVE_DIRECTORY,
    Archive,
    choose_track,
    make_best,
    make_record,
    merge_records,
    month_directory,
    record_key,
    validate_catalog,
    validate_month_index,
    validate_season_index,
)
from xuebuild.tc.backfill import deck_records
from xuebuild.tc.harvest import _follow, canonical_storm, storm_records
from xuebuild.tc.registry import season_of


def _forecast(base: str, lat: float = 15.0, *, run: bool = True) -> dict:
    payload = {"issued": base, "base": base}
    if run:
        payload["run"] = base[:4] + base[5:7] + base[8:10] + base[11:13]
    payload["points"] = [
        {"time": base, "lead": 0, "lat": lat, "lon": 140.0, "vmax": 20.0, "pmin": 995.0,
         "radii": None, "class": None, "rmw": None, "gust": None, "cone": None},
    ]
    return payload


def _track(source: str, last: str, *, provisional: bool) -> dict:
    return {
        "source": source,
        "provisional": provisional,
        "points": [
            {"time": "2026-10-01T00:00:00Z", "lat": 10.0, "lon": 140.0, "vmax": 15.0, "pmin": 1000.0,
             "radii": None, "class": None, "rmw": None, "gust": None, "cone": None},
            {"time": last, "lat": 12.0, "lon": 139.0, "vmax": 25.0, "pmin": 990.0,
             "radii": None, "class": None, "rmw": None, "gust": None, "cone": None},
        ],
    }


class SeasonTest(unittest.TestCase):
    def test_southern_season_is_named_after_the_year_it_ends(self) -> None:
        self.assertEqual(season_of("SH", datetime(2025, 7, 1, tzinfo=UTC)), 2026)
        self.assertEqual(season_of("SH", datetime(2026, 6, 30, tzinfo=UTC)), 2026)
        self.assertEqual(season_of("WP", datetime(2025, 12, 31, tzinfo=UTC)), 2025)

    def test_atcf_record_takes_the_season(self) -> None:
        line = "SH, 03, 2025111200, 03, AVNO,   0, 120S,  850E,  35, 1000, XX"
        record = atcf.parse_line(line)
        assert record is not None
        self.assertEqual(record.storm_id, "SH032026")


    def test_tracker_sub_basins_fold_into_the_jtwc_basin(self) -> None:
        record = atcf.parse_line("BB, 02, 2026102500, 03, AVNO,   0, 150N,  880E,  35, 1000, XX")
        assert record is not None
        self.assertEqual(record.storm_id, "IO022026")
        record = atcf.parse_line("SI, 02, 2026102500, 03, AVNO,   0, 150S,  880E,  35, 1000, XX")
        assert record is not None
        self.assertEqual(record.storm_id, "SH022027")

    def test_published_ids_are_canonicalised(self) -> None:
        self.assertEqual(canonical_storm("BB022026", "2026-10-25T00:00:00Z"), "IO022026")
        self.assertEqual(canonical_storm("SI022026", "2026-10-25T00:00:00Z"), "SH022027")
        self.assertEqual(canonical_storm("SH012026", "2026-10-25T00:00:00Z"), "SH012027")
        self.assertEqual(canonical_storm("SH022027", "2026-10-25T00:00:00Z"), "SH022027")
        self.assertEqual(canonical_storm("SH152026", "2026-03-01T00:00:00Z"), "SH152026")
        self.assertEqual(canonical_storm("WP302026", "2027-01-02T00:00:00Z"), "WP302026")


class MergeTest(unittest.TestCase):
    def test_later_replaces_but_live_is_never_replaced_by_a_backfill(self) -> None:
        live = make_record("WP302026", "WP", None, "gfs", "model", "live", _forecast("2026-10-20T00:00:00Z"))
        backfilled = make_record("WP302026", "WP", None, "gfs", "model", "ucar-atcf", _forecast("2026-10-20T00:00:00Z", 16))
        records: dict = {}
        self.assertEqual(merge_records(records, [backfilled]), 1)
        self.assertEqual(merge_records(records, [live]), 1)
        self.assertEqual(merge_records(records, [backfilled]), 0)
        self.assertEqual(records[record_key(live)]["origin"], "live")
        newer = make_record("WP302026", "WP", "X", "gfs", "model", "live", _forecast("2026-10-20T00:00:00Z", 17))
        self.assertEqual(merge_records(records, [newer]), 1)
        self.assertEqual(records[record_key(live)]["name"], "X")

    def test_choose_track(self) -> None:
        working = _track("jtwc", "2026-10-05T00:00:00Z", provisional=True)
        stale = _track("ibtracs", "2026-10-03T00:00:00Z", provisional=True)
        final = _track("ibtracs", "2026-10-02T00:00:00Z", provisional=False)
        self.assertIs(choose_track([stale, working]), working)
        self.assertIs(choose_track([working, final]), final)
        self.assertIsNone(choose_track([None, {"points": []}]))

    def test_follow_the_crosswalk(self) -> None:
        crosswalk = {"x-wp-2026101000-1": "x-wp-2026101100-2", "x-wp-2026101100-2": "WP302026"}
        self.assertEqual(_follow(crosswalk, "x-wp-2026101000-1"), "WP302026")
        self.assertEqual(_follow({"a": "b", "b": "a"}, "a"), "b")

    def test_storm_records(self) -> None:
        storm = {
            "name": "FUNG-WONG",
            "agencies": {"jtwc": _forecast("2026-10-20T00:00:00Z", run=False)},
            "models": {
                "gfs": _forecast("2026-10-20T00:00:00Z"),
                "gefs": {"issued": "2026-10-20T00:00:00Z", "base": "2026-10-20T00:00:00Z", "leads": [0],
                         "members": [0], "lat": [1500], "lon": [14000], "vmax": [200], "pmin": [9950], "mean": None},
            },
        }
        records = storm_records(storm, "WP302026")
        self.assertEqual([(r["source"], r["kind"]) for r in records], [("jtwc", "agency"), ("gfs", "model"), ("gefs", "ensemble")])
        self.assertTrue(all(r["storm"] == "WP302026" and r["origin"] == "live" for r in records))


class TreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _fill(self, archive: Archive) -> None:
        archive.add_records(
            [
                make_record("WP302026", "WP", "FUNG-WONG", "gfs", "model", "live", _forecast("2026-10-20T00:00:00Z")),
                make_record("WP302026", "WP", None, "jtwc", "agency", "live", _forecast("2026-10-20T00:00:00Z", run=False)),
                make_record("EP222026", "EP", None, "gfs", "model", "nhc-atcf", _forecast("2026-10-31T18:00:00Z")),
                make_record("EP222026", "EP", None, "gfs", "model", "nhc-atcf", _forecast("2026-11-01T00:00:00Z")),
                make_record("SH022026", "SH", None, "ecmwf", "model", "ecmwf-aws", _forecast("2025-11-01T00:00:00Z")),
            ]
        )
        archive.add_best(make_best("WP302026", "FUNG-WONG", "2026290N10140", [], {"usa": _track("ibtracs", "2026-10-05T00:00:00Z", provisional=True)}))

    def test_months_seasons_and_catalog(self) -> None:
        archive = Archive.open(self.root)
        self._fill(archive)
        catalog = archive.write()
        validate_catalog(catalog)
        self.assertEqual([m["month"] for m in catalog["months"]], ["2025-11", "2026-10", "2026-11"])
        self.assertEqual([s["season"] for s in catalog["seasons"]], [2026])
        self.assertEqual(archive.written[-1], f"{ARCHIVE_DIRECTORY}/catalog.json")

        october = self.root / month_directory("2026-10")
        index = json.loads((october / "index.json").read_text())
        validate_month_index(index)
        self.assertEqual([s["id"] for s in index["storms"]], ["EP222026", "WP302026"])
        body = (october / "forecasts.jsonl").read_bytes()
        wp = index["storms"][1]
        lines = body[wp["offset"] : wp["offset"] + wp["length"]].splitlines()
        self.assertEqual([json.loads(line)["source"] for line in lines], ["gfs", "jtwc"])
        self.assertEqual(wp["name"], "FUNG-WONG")

        season = json.loads((self.root / ARCHIVE_DIRECTORY / "2026" / "index.json").read_text())
        validate_season_index(season)
        by_id = {s["id"]: s for s in season["storms"]}
        self.assertEqual(by_id["SH022026"]["months"], ["2025-11"])
        self.assertEqual(by_id["EP222026"]["months"], ["2026-10", "2026-11"])
        self.assertIsNone(by_id["EP222026"]["offset"])
        self.assertEqual(by_id["WP302026"]["best"], ["usa"])

    def test_reopening_and_rewriting_the_same_records_writes_nothing(self) -> None:
        archive = Archive.open(self.root)
        self._fill(archive)
        archive.write()
        again = Archive.open(self.root)
        self._fill(again)
        again.write()
        self.assertEqual(again.written, [])

    def test_a_record_reaching_an_old_month_rewrites_only_that_month(self) -> None:
        archive = Archive.open(self.root)
        self._fill(archive)
        archive.write()
        later = Archive.open(self.root)
        later.add_records([make_record("WP302026", "WP", None, "ecmwf", "model", "ecmwf-aws", _forecast("2026-10-19T12:00:00Z"))])
        later.write()
        self.assertEqual(
            later.written,
            [
                f"{month_directory('2026-10')}/forecasts.jsonl",
                f"{month_directory('2026-10')}/index.json",
                f"{ARCHIVE_DIRECTORY}/catalog.json",
            ],
        )
        self.assertEqual(len(later.month("2025-11")), 1)

    def test_rejects_malformed_records(self) -> None:
        archive = Archive.open(self.root)
        with self.assertRaises(TcProductError):
            archive.add_records([make_record("x-wp-2026101000-1", "WP", None, "gfs", "model", "live", _forecast("2026-10-20T00:00:00Z"))])
        with self.assertRaises(TcProductError):
            archive.add_records([make_record("WP302026", "WP", None, "gfs", "model", "elsewhere", _forecast("2026-10-20T00:00:00Z"))])


DECK = """\
WP, 22, 2025090600, 03, OFCL,   0, 181N, 1165E,  30, 1002, TD
WP, 22, 2025090600, 03, OFCL,  12, 185N, 1160E,  35, 1000, TS,  34, NEQ,   40,   40,   30,   30
WP, 22, 2025090600, 01, CARQ,   0, 181N, 1165E,  30, 1002, XX
WP, 22, 2025090600, 03, AVNX,   0, 181N, 1165E,  30, 1002, XX
WP, 22, 2025090600, 03, AVNX,   6, 183N, 1163E,  32, 1001, XX
WP, 22, 2025090600, 03, AP01,   0, 180N, 1165E,  30, 1003, XX
WP, 22, 2025090600, 03, AP01,   6, 182N, 1162E,  31, 1002, XX
WP, 22, 2025090600, 03, AP02,   6, 184N, 1164E,  33, 1000, XX
WP, 22, 2025090600, 03, AEMN,   6, 183N, 1163E,  32, 1001, XX
WP, 22, 2025090606, 03, AVNO,   0, 182N, 1164E,  30, 1002, XX
WP, 22, 2025090606, 03, AVNX,   0, 179N, 1160E,  30, 1002, XX
"""


class DeckTest(unittest.TestCase):
    def test_deck_records(self) -> None:
        records = deck_records(DECK, "WP222025", "ucar-atcf")
        summary = [(r["source"], r["kind"], r["forecast"]["base"]) for r in records]
        self.assertEqual(
            summary,
            [
                ("nhc", "agency", "2025-09-06T00:00:00Z"),
                ("gfs", "model", "2025-09-06T00:00:00Z"),
                ("gfs", "model", "2025-09-06T06:00:00Z"),
                ("gefs", "ensemble", "2025-09-06T00:00:00Z"),
            ],
        )
        # AVNO is preferred where both exist; AVNX stands in where it is alone.
        self.assertEqual(records[2]["forecast"]["points"][0]["lat"], 18.2)
        self.assertNotIn("run", records[0]["forecast"])
        self.assertEqual(records[0]["forecast"]["points"][1]["radii"]["34"][0], 74.1)
        ensemble = records[3]["forecast"]
        self.assertEqual(ensemble["members"], [1, 2])
        self.assertEqual(ensemble["leads"], [0, 21600])
        self.assertEqual(ensemble["lat"], [1800, 1820, -32768, 1840])
        self.assertEqual(len(ensemble["mean"]["points"]), 1)
        self.assertTrue(all(record["storm"] == "WP222025" for record in records))


if __name__ == "__main__":
    unittest.main()
