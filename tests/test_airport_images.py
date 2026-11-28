"""The airport pages' share cards (xuebuild/airport/images.py, docs/airport.md
§8): the frame, what is drawn, what is kept, how files are named, and a
smoke render when matplotlib is installed (the tcimages group)."""

import io
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from xuebuild.airport import images
from xuebuild.errors import XueError

ROOT = Path(__file__).resolve().parents[1]


def airport(icao, lat, lon, iata="XXX", name="Test Airport", city="Test"):
    return {"icao": icao, "iata": iata, "name": name, "city": city, "lat": lat, "lon": lon}


HANEDA = airport("RJTT", 35.552, 139.78, "HND", "Tokyo Haneda International Airport", "Tokyo")
NARITA = airport("RJAA", 35.765, 140.386, "NRT", "Narita International Airport", "Narita")
KEFLAVIK = airport("BIKF", 63.987, -22.614, "KEF", "Keflavik International Airport", "Reykjavík")
PAGO = airport("NSTU", -14.331, -170.721, "PPG", "Pago Pago International Airport", "Pago Pago")
NADI = airport("NFFN", -17.755, 177.443, "NAN", "Nadi International Airport", "Nadi")


def stub_draw(subject):
    return f"{subject.icao}/{subject.source}".encode()


class FrameTest(unittest.TestCase):
    def test_the_airport_sits_right_of_centre_inside_the_frame(self):
        west, east, south, north, _ = images.frame(HANEDA)
        self.assertLess(west, HANEDA["lon"])
        self.assertLess(HANEDA["lon"], east)
        self.assertGreater(HANEDA["lon"], (west + east) / 2)
        self.assertLess(south, HANEDA["lat"])
        self.assertLess(HANEDA["lat"], north)

    def test_the_frame_widens_with_latitude(self):
        tokyo = images.frame(HANEDA)
        iceland = images.frame(KEFLAVIK)
        self.assertGreater(iceland[1] - iceland[0], tokyo[1] - tokyo[0])

    def test_the_frame_stays_inside_the_poles(self):
        _, _, south, north, _ = images.frame(airport("XPOL", 85.0, 0.0))
        self.assertEqual(north, 90.0)
        self.assertGreater(south, -90.0)

    def test_neighbours_across_the_antimeridian(self):
        others = images.neighbours(PAGO, [PAGO, NADI, HANEDA])
        self.assertEqual(len(others), 1)
        lon, lat = others[0]
        # Nadi drawn west of Pago Pago, on Pago Pago's side of the line.
        self.assertAlmostEqual(lon, NADI["lon"] - 360, places=3)
        self.assertAlmostEqual(lat, NADI["lat"], places=3)

    def test_the_card_names_the_codes_and_the_city(self):
        self.assertEqual(images.card_label(HANEDA), ("Tokyo Haneda International Airport", "RJTT · HND · Tokyo"))
        self.assertEqual(images.card_label({**HANEDA, "iata": None})[1], "RJTT · Tokyo")


class ListTest(unittest.TestCase):
    def test_reads_the_checked_in_list(self):
        airports = images.load_list(ROOT / "web/tooling/airports.json")
        self.assertGreater(len(airports), 200)
        self.assertIn("RJTT", {item["icao"] for item in airports})

    def test_refuses_a_malformed_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "airports.json"
            for body in ({"airports": []}, {"airports": [{"icao": "rjtt"}]}, {"airports": [{**HANEDA, "lat": "35"}]}):
                path.write_text(json.dumps(body))
                with self.assertRaises(XueError):
                    images.load_list(path)


class PlanTest(unittest.TestCase):
    def test_draws_everything_the_first_time_and_nothing_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first = images.run([HANEDA, NARITA, KEFLAVIK], None, out, stub_draw)
            self.assertEqual(first["drawn"], ["BIKF", "RJAA", "RJTT"])
            self.assertEqual(first["written"][-1], "airport-images/index.json")
            self.assertEqual(len(first["written"]), 4)
            for relative in first["written"][:-1]:
                self.assertRegex(relative.removeprefix("airport-images/"), images.PATH_RE)
                data = (out / relative).read_bytes()
                self.assertIn(f"{zlib.crc32(data):08x}", relative)
            index = json.loads((out / "airport-images/index.json").read_text())
            self.assertEqual(index["product"], "airport-images")
            self.assertEqual((index["width"], index["height"]), (1200, 630))
            self.assertEqual(list(index["airports"]), ["BIKF", "RJAA", "RJTT"])

            second = images.run([HANEDA, NARITA, KEFLAVIK], index, out, stub_draw)
            self.assertEqual(second["drawn"], [])
            self.assertEqual(second["written"], [])

    def test_a_neighbour_moving_redraws_and_a_dropped_airport_leaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            images.run([HANEDA, NARITA, KEFLAVIK], None, out, stub_draw)
            index = json.loads((out / "airport-images/index.json").read_text())
            report = images.run([HANEDA, {**NARITA, "lat": 35.8}], index, out, stub_draw)
            # Narita moved: its own card and Haneda's, which shows it, are drawn again.
            self.assertEqual(report["drawn"], ["RJAA", "RJTT"])
            after = json.loads((out / "airport-images/index.json").read_text())
            self.assertNotIn("BIKF", after["airports"])

    def test_a_style_change_or_a_malformed_entry_draws_again(self):
        wanted = images.subjects([HANEDA, KEFLAVIK])
        _, kept = images.plan(None, wanted)
        self.assertEqual(kept, {})
        sources = {subject.icao: subject.source for subject in wanted}
        live = {
            "airports": {
                "RJTT": {"source": sources["RJTT"], "card": {"path": "../x.png"}},
                "BIKF": {"source": "0:" + sources["BIKF"].split(":")[1], "card": {"path": "BIKF/card-00000000.png"}},
            }
        }
        draw, _ = images.plan(live, wanted)
        self.assertEqual([subject.icao for subject in draw], ["BIKF", "RJTT"])
        draw, _ = images.plan({"airports": {"RJTT": {"source": sources["RJTT"], "card": {"path": "RJTT/card-00000000.png"}}}}, wanted, force=True)
        self.assertEqual(len(draw), 2)


try:
    import matplotlib  # noqa: F401
    import PIL  # noqa: F401

    HAVE_PAINTER = True
except ImportError:
    HAVE_PAINTER = False


@unittest.skipUnless(HAVE_PAINTER, "the tcimages dependency group is not installed")
class PainterTest(unittest.TestCase):
    def test_draws_a_card_at_card_size(self):
        from PIL import Image

        from xuebuild.tc.images import Land

        painter = images.AirportPainter(Land([(135.0, 141.0, [(135.0, 34.0), (141.0, 34.0), (141.0, 37.0), (135.0, 37.0)])]))
        draw = images.painter_draw(painter)
        for subject in images.subjects([HANEDA, NARITA, PAGO, NADI]):
            picture = Image.open(io.BytesIO(draw(subject)))
            self.assertEqual(picture.size, (1200, 630))
            self.assertEqual(picture.format, "PNG")


if __name__ == "__main__":
    unittest.main()
