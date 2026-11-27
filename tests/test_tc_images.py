"""The storm pictures (xuebuild/tc/images.py, docs/tc.md §10): what is drawn,
what is kept, how files are named, and a smoke render when matplotlib is
installed (the tcimages group)."""

import io
import json
import re
import tempfile
import unittest
import zlib
from pathlib import Path

from xuebuild.tc import images
from xuebuild.tc.archive import KNOT, SSHS_FLOORS_KT

ROOT = Path(__file__).resolve().parents[1]


def point(time, lat, lon, kt):
    return {"time": time, "lat": lat, "lon": lon, "vmax": None if kt is None else kt * KNOT, "pmin": None}


def record(storm, points, name="TEST", key="usa"):
    return {"storm": storm, "name": name, "basin": storm[:2], "best": {key: {"points": points}}}


RAGASA = record(
    "WP242025",
    [
        point("2025-09-18T12:00:00Z", 15.9, 133.0, 29),
        point("2025-09-18T15:00:00Z", 16.0, 132.6, 31),
        point("2025-09-19T12:00:00Z", 16.3, 130.1, 99),
        point("2025-09-20T12:00:00Z", 19.3, 122.9, 144),
    ],
    name="RAGASA",
)


def line(value):
    return json.dumps(value, separators=(",", ":")).encode()


def season_input(*records, crc="0badc0de"):
    return [(2025, crc, [line(item) for item in records])]


def stub_draw(subject, kind):
    return f"{subject.season}/{subject.key}/{kind}/{subject.source}".encode()


class TableTest(unittest.TestCase):
    def test_colours_are_the_storm_maps(self):
        source = (ROOT / "web/src/tc/intensity.ts").read_text()
        rows = re.findall(r'\{ code: "(\w+)", minKt: (\d+), color: "(#[0-9a-f]{6})" \}', source)
        self.assertEqual([code for code, _, _ in rows], list(images.SSHS_CODES))
        self.assertEqual(tuple(int(kt) for _, kt, _ in rows), SSHS_FLOORS_KT)
        self.assertEqual(tuple(color for _, _, color in rows), images.SSHS_COLORS)

    def test_grades_by_floor(self):
        self.assertEqual(images.grade(None), -1)
        self.assertEqual(images.grade(33), 0)
        self.assertEqual(images.grade(34), 1)
        self.assertEqual(images.grade(137), 6)


class TrackTest(unittest.TestCase):
    def test_the_pages_track(self):
        track = images.synoptic_track(RAGASA)
        # The 15Z point is IBTrACS' interpolation.
        self.assertEqual([p["time"][11:13] for p in track], ["12", "12", "12"])
        other = record("WP012025", [point("2025-01-01T00:00:00Z", 10, 130, 30)] * 2, key="jma")
        other["best"]["usa"] = {"points": [point("2025-01-01T06:00:00Z", 11, 131, 40)] * 2}
        self.assertEqual(images.synoptic_track(other)[0]["lat"], 11)

    def test_a_track_across_the_antimeridian_stays_whole(self):
        track = [point("2025-01-01T00:00:00Z", -15, 178, 40), point("2025-01-01T06:00:00Z", -16, -178, 40)]
        west, east, _, _, _ = images.storm_frame(track)
        self.assertLess(west, 178)
        self.assertGreater(east, 182)

    def test_labels(self):
        title, sub = images.storm_label(RAGASA, images.synoptic_track(RAGASA))
        self.assertEqual(title, "Ragasa · WP242025")
        self.assertEqual(sub, "2025-09-18 → 2025-09-20 · peak 144 kt H5")
        self.assertIn("July 2024 to June 2025", images.season_label(2025, 3, True)[1])
        self.assertNotIn("July", images.season_label(2025, 3, False)[1])


class PlanTest(unittest.TestCase):
    def test_draws_everything_the_first_time_and_nothing_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first = images.run(season_input(RAGASA), None, out, stub_draw)
            self.assertEqual(first["drawn"], ["2025/WP242025", "2025/season"])
            self.assertEqual(first["written"][-1], "tc-images/index.json")
            self.assertEqual(len(first["written"]), 7)
            for relative in first["written"][:-1]:
                self.assertRegex(relative.removeprefix("tc-images/"), images.PATH_RE)
                data = (out / relative).read_bytes()
                self.assertIn(f"{zlib.crc32(data):08x}", relative)
            index = json.loads((out / "tc-images/index.json").read_text())
            self.assertEqual(index["storms"]["WP242025"]["source"], images.storm_source(line(RAGASA)))
            self.assertEqual(index["seasons"]["2025"]["source"], images.season_source("0badc0de"))

            second = images.run(season_input(RAGASA), index, out, stub_draw)
            self.assertEqual(second["drawn"], [])
            self.assertEqual(second["written"], [])

    def test_redraws_what_changed_and_drops_what_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            gone = record("WP012025", [point("2025-01-01T00:00:00Z", 10, 130, 30), point("2025-01-01T06:00:00Z", 11, 131, 35)])
            images.run(season_input(RAGASA, gone), None, out, stub_draw)
            index = json.loads((out / "tc-images/index.json").read_text())
            moved = json.loads(json.dumps(RAGASA))
            moved["best"]["usa"]["points"][-1]["lat"] = 19.4
            report = images.run(season_input(moved, crc="feedface"), index, out, stub_draw)
            self.assertEqual(report["drawn"], ["2025/WP242025", "2025/season"])
            after = json.loads((out / "tc-images/index.json").read_text())
            self.assertNotIn("WP012025", after["storms"])

    def test_a_style_change_or_a_malformed_entry_draws_again(self):
        wanted = images.subjects(season_input(RAGASA))
        _, kept = images.plan(None, wanted)
        self.assertEqual(kept, {"storms": {}, "seasons": {}})
        live = {
            "storms": {"WP242025": {"source": images.storm_source(line(RAGASA)), "light": {"path": "../x"}}},
            "seasons": {"2025": {"source": "1:0badc0de"}},
        }
        draw, _ = images.plan(live, wanted)
        self.assertEqual([s.key for s in draw], ["WP242025", "season"])

    def test_a_storm_the_page_cannot_draw_gets_no_picture(self):
        lone = record("WP022025", [point("2025-02-01T00:00:00Z", 10, 130, 30)])
        self.assertEqual([s.key for s in images.subjects(season_input(lone))], ["season"])


try:
    import matplotlib  # noqa: F401
    import PIL  # noqa: F401

    HAVE_PAINTER = True
except ImportError:
    HAVE_PAINTER = False


@unittest.skipUnless(HAVE_PAINTER, "the tcimages dependency group is not installed")
class PainterTest(unittest.TestCase):
    def test_draws_both_pictures_at_card_size(self):
        from PIL import Image

        land = images.Land([(120.0, 125.0, [(120.0, 10.0), (125.0, 10.0), (125.0, 20.0), (120.0, 20.0)])])
        painter = images.Painter(land)
        draw = images.painter_draw(painter)
        for subject in images.subjects(season_input(RAGASA)):
            for kind in images.KINDS:
                data = draw(subject, kind)
                picture = Image.open(io.BytesIO(data))
                self.assertEqual(picture.size, (images.WIDTH, images.HEIGHT))
                self.assertEqual(picture.format, "PNG" if kind == "card" else "WEBP")


if __name__ == "__main__":
    unittest.main()
