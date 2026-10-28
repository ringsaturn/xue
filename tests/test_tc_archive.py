"""The tropical cyclone archive (``docs/tc.md`` §8): the merge rules, the
month and season files and their indexes, the a-deck backfill reader and
the season-year rule, all offline on constructed input.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xuebuild.errors import TcProductError
from xuebuild.tc import atcf
from xuebuild.tc.archive import (
    ARCHIVE_DIRECTORY,
    Archive,
    BestMatcher,
    rekey,
    start_position,
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
from xuebuild.tc.backfill import deck_records, ecmwf_records, released
from xuebuild.tc.build import ecmwf_key
from xuebuild.tc.harvest import _follow, canonical_storm, clustered_ensemble, storm_records
from xuebuild.tc.identity import dominant_cluster
from xuebuild.tc.score import along_cross, forecast_positions, great_circle_km, mean_position, score_storm, summarise
from xuebuild.tc.track import MISSING
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
        wp = by_id["WP302026"]
        self.assertEqual((wp["first"], wp["last"], wp["vmax"]), ("2026-10-01T00:00:00Z", "2026-10-05T00:00:00Z", 25.0))
        self.assertEqual(wp["sources"], ["gfs", "jtwc"])
        self.assertEqual((by_id["EP222026"]["first"], by_id["EP222026"]["sources"]), (None, ["gfs"]))

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
WP, 22, 2025090606, 03, GDMN,   0, 181N, 1163E,  30, 1003, XX
WP, 22, 2025090606, 03, GDMN,  12, 186N, 1158E,  35, 1000, XX
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
                ("gdm", "model", "2025-09-06T06:00:00Z"),
                ("gefs", "ensemble", "2025-09-06T00:00:00Z"),
            ],
        )
        # AVNO is preferred where both exist; AVNX stands in where it is alone.
        self.assertEqual(records[2]["forecast"]["points"][0]["lat"], 18.2)
        self.assertNotIn("run", records[0]["forecast"])
        self.assertEqual(records[0]["forecast"]["points"][1]["radii"]["34"][0], 74.1)
        ensemble = records[4]["forecast"]
        self.assertEqual(ensemble["members"], [1, 2])
        self.assertEqual(ensemble["leads"], [0, 21600])
        self.assertEqual(ensemble["lat"], [1800, 1820, -32768, 1840])
        self.assertEqual(len(ensemble["mean"]["points"]), 1)
        self.assertTrue(all(record["storm"] == "WP222025" for record in records))

    def test_google_deepmind_tracks_wait_until_they_are_history(self) -> None:
        record = next(r for r in deck_records(DECK, "WP222025", "nhc-atcf") if r["source"] == "gdm")
        last = datetime(2025, 9, 6, 18, tzinfo=UTC)
        self.assertFalse(released(record, last + timedelta(hours=47)))
        self.assertTrue(released(record, last + timedelta(hours=48)))
        self.assertTrue(released(next(r for r in deck_records(DECK, "WP222025", "nhc-atcf") if r["source"] == "gfs"), last))


def _point(time: str, lat: float, lon: float, *, cls: str | None = "TS", vmax: float | None = 25.0, lead: int | None = None) -> dict:
    point = {"time": time}
    if lead is not None:
        point["lead"] = lead
    point.update({"lat": lat, "lon": lon, "vmax": vmax, "pmin": 990.0, "radii": None, "class": cls,
                  "rmw": None, "gust": None, "cone": None})
    return point


class ScoreTest(unittest.TestCase):
    BASE = "2026-10-20T00:00:00Z"

    def _best(self) -> dict:
        # Due north at one degree a day, a storm from start to end.
        return {"usa": {"source": "ibtracs", "provisional": False, "points": [
            _point("2026-10-19T18:00:00Z", 14.75, 140.0),
            _point("2026-10-20T00:00:00Z", 15.0, 140.0),
            _point("2026-10-20T03:00:00Z", 15.1, 140.0),
            _point("2026-10-20T06:00:00Z", 15.25, 140.0),
            _point("2026-10-20T18:00:00Z", 15.75, 140.0),
            _point("2026-10-21T00:00:00Z", 16.0, 140.0),
            _point("2026-10-21T06:00:00Z", 16.25, 140.0),
            _point("2026-10-22T00:00:00Z", 17.0, 140.0, cls="EX"),
        ]}}

    def _record(self, source: str, points: list[dict]) -> dict:
        forecast = {"issued": self.BASE, "base": self.BASE, "points": points}
        return make_record("WP302026", "WP", None, source, "model", "live", forecast)

    def test_geometry(self) -> None:
        self.assertAlmostEqual(great_circle_km(0, 0, 1, 0), 111.19, places=1)
        self.assertAlmostEqual(great_circle_km(10, 179.5, 10, -179.5), great_circle_km(10, 0, 10, 1), places=6)
        along, cross = along_cross((15.0, 140.0), (16.0, 140.0), (0.0, 100.0))
        self.assertAlmostEqual(along, 111.19, places=1)
        self.assertAlmostEqual(cross, 0.0, places=6)
        along, cross = along_cross((15.0, 140.0), (15.0, 141.0), (0.0, 100.0))
        self.assertGreater(cross, 100)  # east of a northward track is to its right
        self.assertEqual(mean_position([10, 10], [179.0, -179.0]), (10.0, 180.0))

    def test_score_storm_verifies_tropical_synoptic_points_only(self) -> None:
        record = self._record("gfs", [
            _point("2026-10-20T00:00:00Z", 15.0, 141.0, lead=0),
            _point("2026-10-20T03:00:00Z", 15.0, 141.0, lead=10800),
            _point("2026-10-21T00:00:00Z", 16.0, 140.0, vmax=30.0, lead=86400),
            _point("2026-10-22T00:00:00Z", 17.0, 140.0, lead=172800),
        ])
        scored = score_storm([record], self._best(), "WP")
        self.assertEqual(list(scored), ["usa"])
        column = scored["usa"]["gfs"]
        self.assertEqual(column["lead"], [0, 24])  # 3 h is no scored lead; 48 h verifies on an extratropical point
        self.assertAlmostEqual(column["track"][0], 107.4, places=0)
        self.assertEqual(column["track"][1], 0.0)
        self.assertEqual(column["vmax"], [0.0, 5.0])
        self.assertEqual(score_storm([record], self._best(), "AL")["usa"]["gfs"]["lead"], [0, 24])
        self.assertEqual(score_storm([record], {"jma": self._best()["usa"]}, "AL"), {})

    def test_a_track_with_only_its_start_is_not_verified(self) -> None:
        lost = self._record("ecmwf", [_point("2026-10-20T00:00:00Z", 13.5, 145.0, lead=0)])
        self.assertEqual(score_storm([lost], self._best(), "WP"), {})

    def test_forecast_starting_outside_the_tropical_life_is_not_verified(self) -> None:
        best = self._best()
        best["usa"]["points"][1]["class"] = "LO"
        record = self._record("gfs", [_point("2026-10-21T00:00:00Z", 16.0, 140.0, lead=86400)])
        self.assertEqual(score_storm([record], best, "WP"), {})

    def test_ensemble_mean_needs_a_quorum_and_one_system(self) -> None:
        forecast = {"issued": self.BASE, "base": self.BASE, "leads": [0, 86400], "members": [1, 2, 3, 4],
                    "lat": [1500, 1600, 1500, MISSING, 3100, 3200, 1500, MISSING],
                    "lon": [14000, 14000, 14000, MISSING, 12600, 12600, 14000, MISSING],
                    "vmax": [250] * 8, "pmin": [9900] * 8, "mean": None}
        record = make_record("WP302026", "WP", None, "ecmwfens", "ensemble", "live", forecast)
        positions = forecast_positions(record)
        self.assertEqual(positions[0]["lat"], 15.0)  # the member at 31°N follows another system
        self.assertNotIn(24, positions)  # one of three members left at 24 h is no quorum

    def test_clustered_ensemble_takes_the_group_near_the_anchor(self) -> None:
        firsts = [(31.0, 126.0), (31.1, 126.1), (13.0, 164.0)]
        self.assertEqual(dominant_cluster(firsts), [0, 1])
        self.assertEqual(dominant_cluster(firsts, (13.2, 164.2)), [2])
        self.assertEqual(dominant_cluster(firsts, (0.0, 0.0)), [])
        forecast = {"leads": [0], "members": [1, 2, 3], "lat": [3100, 3110, 1300], "lon": [12600, 12610, 16400],
                    "vmax": [1, 2, 3], "pmin": [9000, 9001, 9002]}
        kept = clustered_ensemble(forecast, (13.2, 164.2))
        self.assertEqual((kept["members"], kept["lat"], kept["pmin"]), ([3], [1300], [9002]))

    def test_summary_and_homogeneous(self) -> None:
        storms = {
            "WP302026": ("WP", {"usa": {
                "jtwc": {"base": [self.BASE, self.BASE], "lead": [24, 48], "track": [50.0, 100.0],
                         "along": [1.0, 2.0], "cross": [0.0, 0.0], "vmax": [1.0, None], "pmin": [None, None]},
                "gfs": {"base": [self.BASE, "2026-10-20T06:00:00Z"], "lead": [24, 24], "track": [100.0, 80.0],
                        "along": [None, None], "cross": [None, None], "vmax": [-2.0, -4.0], "pmin": [3.0, 1.0]},
            }}),
        }
        summary, homogeneous = summarise(storms)
        gfs24 = next(r for r in summary if r["source"] == "gfs" and r["lead"] == 24)
        self.assertEqual((gfs24["n"], gfs24["track"], gfs24["vmaxBias"], gfs24["vmaxMae"], gfs24["along"]), (2, 90.0, -3.0, 3.0, None))
        pair = [r for r in homogeneous if r["reference"] == "jtwc"]
        self.assertEqual(pair, [{"basin": "WP", "truth": "usa", "reference": "jtwc", "source": "gfs", "lead": 24,
                                 "n": 1, "track": 100.0, "referenceTrack": 50.0, "skill": -1.0}])


class PlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.archive = Archive.open(Path(self.directory.name))
        for storm, lat in (("WP292026", 15.0), ("WP302026", 25.0)):
            track = {"source": "jtwc", "provisional": True, "points": [
                _point("2026-10-25T00:00:00Z", lat, 140.0), _point("2026-10-25T06:00:00Z", lat + 0.2, 140.0)]}
            self.archive.add_best(make_best(storm, None, None, [], {"usa": track}))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_ecmwf_numbers_its_own_storms_in_the_western_pacific(self) -> None:
        base = datetime(2026, 10, 25, tzinfo=UTC)
        self.assertEqual(ecmwf_key("36W", base), ("36W", "WP"))
        self.assertEqual(ecmwf_key("14E", base), ("EP142026", "EP"))
        self.assertEqual(ecmwf_key("03S", base), ("SH032027", "SH"))

    def test_matcher_takes_the_nearest_storm_at_the_start(self) -> None:
        matcher = BestMatcher(self.archive)
        self.assertEqual(matcher.match("WP", "2026-10-25T00:00:00Z", 15.5, 140.5), "WP292026")
        self.assertEqual(matcher.match("WP", "2026-10-25T06:00:00Z", 24.0, 140.0), "WP302026")
        self.assertIsNone(matcher.match("WP", "2026-10-25T00:00:00Z", 20.0, 140.0))
        self.assertIsNone(matcher.match("WP", "2026-10-25T12:00:00Z", 15.0, 140.0))
        self.assertIsNone(matcher.match("EP", "2026-10-25T00:00:00Z", 15.0, 140.0))

    def test_start_position_of_an_ensemble_is_its_centroid_at_the_first_lead_it_has(self) -> None:
        forecast = {"base": "2026-10-25T00:00:00Z", "leads": [0, 21600], "members": [1, 2],
                    "lat": [MISSING, 1500, MISSING, 1600], "lon": [MISSING, 17950, MISSING, -17950]}
        self.assertEqual(start_position("ensemble", forecast), ("2026-10-25T06:00:00Z", 15.5, 180.0))

    def test_ecmwf_records_keep_shared_numbers_and_place_their_own(self) -> None:
        tracks = [
            ("EP142026", "EP", None, "model", _forecast("2026-10-25T00:00:00Z")),
            ("36W", "WP", "CHAMPI", "model", _forecast("2026-10-25T00:00:00Z", 15.2)),
            ("70W", "WP", None, "model", _forecast("2026-10-25T00:00:00Z", 25.1)),
            ("71W", "WP", None, "model", _forecast("2026-10-25T00:00:00Z", 40.0)),
        ]
        records = ecmwf_records(tracks, "ecmwf", BestMatcher(self.archive))
        self.assertEqual([(r["storm"], r["origin"]) for r in records],
                         [("EP142026", "ecmwf-aws"), ("WP292026", "ecmwf-aws"), ("WP302026", "ecmwf-aws")])

    def test_rekey_moves_misfiled_records_and_keeps_the_nearer_on_a_collision(self) -> None:
        base = "2026-10-25T00:00:00Z"
        self.archive.add_records([
            make_record("WP362026", "WP", None, "ecmwf", "model", "live", _forecast(base, 15.1)),
            make_record("WP292026", "WP", None, "ecmwf", "model", "live", _forecast(base, 15.8)),
            make_record("WP372026", "WP", None, "ecmwfens", "model", "live", _forecast(base, 33.0)),
            make_record("WP292026", "WP", None, "gfs", "model", "live", _forecast(base, 40.0)),
        ])
        self.archive.write()
        result = rekey(self.archive, BestMatcher(self.archive), ["2026-10"], {"ecmwf", "ecmwfens"}, {"WP"})
        self.assertEqual(result, {"moved": 1, "dropped": 1})
        records = self.archive.month("2026-10")
        self.assertEqual(sorted((s, src) for s, src, _ in records), [("WP292026", "ecmwf"), ("WP292026", "gfs")])
        self.assertEqual(records[("WP292026", "ecmwf", base)]["forecast"]["points"][0]["lat"], 15.1)


if __name__ == "__main__":
    unittest.main()
