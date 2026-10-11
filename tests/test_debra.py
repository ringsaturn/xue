"""The dust confidence producer and the ancillary fields it reads.

``dustcf`` and ``zhouye`` are the second product of the satellite
producer seam (xuebuild/satellite/producers.py): DEBRA's combined dust
confidence and ZHOUYE's diurnally consistent one on the same chain, two
bundles from one pass, computed per slot from five infrared windows
against a clear-sky background modelled from two fields the imager does
not measure — a GFS skin temperature fetched by byte range, and the
CAMEL emissivity months staged on the bucket
(xuebuild/satellite/ancillary.py). The tests here hold four things: that
the ancillary readers and the regrid are right on the plate carrée grids
the bundles carry (including one that runs past 180°); that the chain
composed here from shachen's per-equation modules equals what
``shachen.pipeline.run_debra`` and ``run_zhouye`` compute on the same
grid, cell for cell (ZHOUYE once its day branch is gated); that by day
the two agree exactly and by night they do not; and the two rules the
products add — where the confidence is defined (water, or land inside a
staged file) and the split-window gate.

``tests/fixtures/debra/`` holds two crops of the staged gobi month
(``camel.gobi.202609.crop.nc``, the Gobi itself, land; ``…pacific.nc``,
a box of water south of Japan inside the Himawari fixture tiles' grid)
and four crops of one cached GFS surface-temperature record
(``gfs.tmpsfc.<region>.grib2``), each what ``gdal_translate -projwin``
leaves of the real files.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from unittest import mock
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tests._support import TempRoot, requires_gdal_warp, requires_shachen
from tests.test_satellite import CHANNELS, DEBRA_FIXTURES, SLOT_0300, SLOT_0310, TILE_GRID, TWO_TILES, bucket, fixture_keys, stage_ancillary
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.idx import ByteRange
from xuebuild.satellite import GOES_EAST, HIMAWARI, METEOSAT, ancillary, assemble
from xuebuild.satellite import fetch as satellite_fetch
from xuebuild.satellite.producers import PRODUCERS, DebraProducer, split_window_gate
from xuebuild.satellite.projector import TargetGrid
from xuebuild.variables import VARIABLES

DEBRA = PRODUCERS["dustcf"]
#: A grid over the Gobi at a tenth of a degree, inside the land crop's
#: box on three sides and past it to the west and north.
GOBI_GRID = TargetGrid(west=98.0, south=36.0, east=112.0, north=47.0, step=0.1)
#: The cached record's own valid time, so a golden through shachen's
#: pipeline sees the same sun: 21:00 at the grid's meridian, night.
GOBI_SLOT = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
#: Ten hours earlier: 11:00 at the meridian, the sun high over the whole
#: grid. The same record is cached under this slot's first candidate too;
#: a skin temperature of the wrong hour is fine for an equality.
GOBI_DAY_SLOT = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)


def gobi_ancillary(root: Path) -> Path:
    """The Gobi crop staged as the month's gobi file, and the record
    cached under the name its first candidate cycle would take."""
    camel = root / "ancillary" / "camel" / "gobi"
    camel.mkdir(parents=True)
    shutil.copy(DEBRA_FIXTURES / "camel.gobi.202609.crop.nc", camel / "202609.nc")
    gfs = root / "ancillary" / "gfs"
    gfs.mkdir(parents=True)
    for slot in (GOBI_SLOT, GOBI_DAY_SLOT):
        cycle, forecast_hour = ancillary.skin_temperature_candidates(slot)[0]
        shutil.copy(DEBRA_FIXTURES / "gfs.tmpsfc.gobi.grib2", gfs / ancillary.skin_temperature_name(cycle, forecast_hour))
    return root / "ancillary"


def synthetic_scene(grid: TargetGrid, *, seed: int = 1) -> dict[str, np.ndarray]:
    """Five windows of a warm, quiet scene with a split-window plume in
    the middle: the 12.3 and 8.6 µm windows read warmer than the 10.4 µm
    one there, which is what lofted dust does, and colder everywhere
    else, which is what clear ground does."""
    rng = np.random.default_rng(seed)
    base = 295.0 + rng.normal(0, 1.5, (grid.height, grid.width))
    scene = {
        "ir104": base.copy(),
        "ir123": base - 1.0,
        "ir086": base - 2.0,
        "ir039": base + 3.0,
        "wv062": base - 50.0,
    }
    rows, columns = slice(grid.height * 2 // 5, grid.height * 3 // 5), slice(grid.width * 2 // 5, grid.width * 3 // 5)
    scene["ir123"][rows, columns] = base[rows, columns] + 1.5
    scene["ir086"][rows, columns] = base[rows, columns] + 1.0
    return scene


class RegridTests(unittest.TestCase):
    def test_a_field_is_interpolated_bilinearly_and_never_extrapolated(self) -> None:
        lats = np.array([12.0, 11.0, 10.0])
        lons = np.array([-70.0, -69.0, -68.0, -67.0])
        values = lats[:, None] * 10.0 + lons[None, :]
        field = ancillary.LatLonField(lats, lons, values)
        grid = TargetGrid(west=-69.5, south=10.5, east=-67.5, north=11.5, step=0.5)
        out = ancillary.regrid(field, grid)
        self.assertEqual(out.shape, (2, 4))
        target_lons = grid.first_longitude + np.arange(grid.width) * grid.step
        target_lats = grid.first_latitude - np.arange(grid.height) * grid.step
        np.testing.assert_allclose(out, target_lats[:, None] * 10.0 + target_lons[None, :])
        # A grid reaching past the field is NaN there, not extrapolated.
        wider = TargetGrid(west=-71.0, south=9.0, east=-66.0, north=13.0, step=0.5)
        out = ancillary.regrid(field, wider)
        self.assertTrue(np.isnan(out[0]).all() and np.isnan(out[-1]).all())
        self.assertTrue(np.isnan(out[:, 0]).all() and np.isnan(out[:, -1]).all())
        self.assertTrue(np.isfinite(out[3:5, 3:8]).all())

    def test_a_global_field_is_rebased_onto_a_grid_past_the_antimeridian_and_wrapped(self) -> None:
        """GDAL hands a GFS record back on −180 … 180; Himawari's grid runs
        80.7 … 200.7. Every longitude is read in the grid's copy of the
        world and the field's first column is repeated a turn later, so
        the cells across 180° interpolate between 179.75 and 180."""
        lons = np.arange(-180.0, 180.0, 0.25)
        lats = np.arange(90.0, -90.01, -0.25)
        values = np.broadcast_to(np.mod(lons, 360.0)[None, :], (lats.size, lons.size)).copy()
        field = ancillary.LatLonField(lats, lons, values)
        grid = TargetGrid(west=80.7, south=-60.0, east=200.7, north=60.0, step=0.4)
        out = ancillary.regrid(field, grid)
        self.assertFalse(np.isnan(out).any())
        target_lons = grid.first_longitude + np.arange(grid.width) * grid.step
        np.testing.assert_allclose(out[100], target_lons, atol=1e-9)
        self.assertEqual(ancillary.rebase_longitudes(np.array([-170.0, 170.0, 190.0]), 80.7).tolist(), [190.0, 170.0, 190.0])
        # And onto GOES-East's, which stays inside −180 … 180.
        east = TargetGrid(west=-135.2, south=-60.0, east=-15.2, north=60.0, step=0.4)
        out = ancillary.regrid(field, east)
        expected = np.mod(east.first_longitude + np.arange(east.width) * east.step, 360.0)
        np.testing.assert_allclose(out[100], expected, atol=1e-9)


@requires_gdal_warp
class AncillaryReaderTests(unittest.TestCase):
    def test_the_skin_temperature_is_kelvin_on_its_own_grid(self) -> None:
        field = ancillary.read_skin_temperature(DEBRA_FIXTURES / "gfs.tmpsfc.gobi.grib2")
        self.assertEqual(field.values.shape, (81, 81))
        self.assertEqual((float(field.latitudes[0]), float(field.latitudes[-1])), (50.0, 30.0))
        self.assertEqual((float(field.longitudes[0]), float(field.longitudes[-1])), (95.0, 115.0))
        # Kelvin, not the Celsius GDAL would normalise the record to.
        self.assertGreater(float(np.nanmin(field.values)), 250.0)
        self.assertLess(float(np.nanmax(field.values)), 330.0)
        with self.assertRaisesRegex(ConversionError, "not the surface temperature|exactly one band"):
            ancillary.read_skin_temperature(DEBRA_FIXTURES / "camel.gobi.202609.crop.nc")

    def test_a_staged_month_is_read_with_what_it_says_of_itself(self) -> None:
        staged = ancillary.read_staged_emissivity(DEBRA_FIXTURES / "camel.gobi.202609.crop.nc")
        self.assertEqual((staged.region, staged.month, staged.source_month), ("gobi", "2026-09", "2023-09"))
        self.assertEqual(tuple(round(edge, 3) for edge in staged.extent), (100.0, 38.0, 110.0, 45.0))
        self.assertEqual(list(staged.fields), list(ancillary.EMISSIVITY_VARIABLES))
        window = staged.fields["emis_tir_104"]
        self.assertEqual(window.values.shape, (140, 200))
        self.assertEqual((float(window.latitudes[0]) > float(window.latitudes[-1])), True)
        self.assertGreater(float(np.nanmin(window.values)), 0.85)
        self.assertLessEqual(float(np.nanmax(window.values)), 1.0)
        # Land: nearly every cell has a retrieval.
        self.assertGreater(float(np.isfinite(window.values).mean()), 0.99)
        planes, covered = ancillary.emissivity_on_grid([staged], GOBI_GRID)
        self.assertAlmostEqual(float(covered.mean()), (10 * 7) / (14 * 11), places=6)
        # Inside the file's extent the emissivity is there but for the
        # outermost half cell (nothing is extrapolated past the cell
        # centres) and the few cells the climatology has no retrieval for;
        # outside it there is nothing at all.
        self.assertGreater(float(np.isfinite(planes["emis_tir_104"][covered]).mean()), 0.98)
        self.assertTrue(np.isnan(planes["emis_tir_104"][~covered]).all())
        interior = covered.copy()
        interior[:, :] = False
        interior[21:89, 21:119] = True
        self.assertGreater(float(np.isfinite(planes["emis_tir_104"][interior]).mean()), 0.995)

    def test_the_month_s_file_is_taken_and_an_unstaged_month_falls_back_with_a_warning(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="xue-debra-camel-"))
        self.addCleanup(shutil.rmtree, root, True)
        camel = root / "camel"
        for region, month in (("gobi", "202608"), ("gobi", "202609"), ("swus", "202609")):
            (camel / region).mkdir(parents=True, exist_ok=True)
            shutil.copy(DEBRA_FIXTURES / "camel.gobi.202609.crop.nc", camel / region / f"{month}.nc")
        (camel / "swus" / "notes.txt").write_text("not a month\n", encoding="utf-8")
        self.assertEqual(ancillary.staged_months(camel), {"gobi": ["202608", "202609"], "swus": ["202609"]})
        september = ancillary.staged_emissivity(camel, datetime(2026, 9, 17, 3, tzinfo=UTC))
        self.assertEqual([(staged.region, staged.path.name) for staged in september], [("gobi", "202609.nc"), ("swus", "202609.nc")])
        with self.assertLogs("xuebuild.satellite.ancillary", level="WARNING") as logs:
            october = ancillary.staged_emissivity(camel, datetime(2026, 10, 1, 0, tzinfo=UTC))
        self.assertEqual([staged.path.name for staged in october], ["202609.nc", "202609.nc"])
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(ancillary.staged_months(root / "nowhere"), {})
        with self.assertRaisesRegex(ConversionError, "pull-r2-ancillary"):
            ancillary.staged_emissivity(root / "nowhere", datetime(2026, 9, 17, 3, tzinfo=UTC))

    def test_the_record_is_the_newest_cycle_that_reaches_the_hour_fetched_by_range(self) -> None:
        """The candidates are the cycles that reach the whole hour nearest
        the slot, newest first; the first whose index the bucket serves
        is taken, its one record located in the index and fetched as a
        byte range, and the cache is by cycle and hour so a later slot of
        the same hour asks nothing."""
        slot = datetime(2026, 9, 17, 3, 10, tzinfo=UTC)
        self.assertEqual(
            ancillary.skin_temperature_candidates(slot),
            [
                (datetime(2026, 9, 17, 0, tzinfo=UTC), 3),
                (datetime(2026, 9, 16, 18, tzinfo=UTC), 9),
                (datetime(2026, 9, 16, 12, tzinfo=UTC), 15),
                (datetime(2026, 9, 16, 6, tzinfo=UTC), 21),
            ],
        )
        self.assertEqual(ancillary.skin_temperature_candidates(datetime(2026, 9, 17, 3, 40, tzinfo=UTC))[0], (datetime(2026, 9, 17, 0, tzinfo=UTC), 4))
        record = (DEBRA_FIXTURES / "gfs.tmpsfc.gobi.grib2").read_bytes()
        index = "1:0:d=2026091618:PRMSL:mean sea level:9 hour fcst:\n2:100:d=2026091618:TMP:surface:9 hour fcst:\n3:%d:d=2026091618:TMP:2 m above ground:9 hour fcst:\n" % (100 + len(record))
        asked: list[tuple[str, object]] = []

        def exists(url: str) -> bool:
            asked.append(("exists", url))
            return "gfs.20260916/18/" in url

        def fetch_text(url: str) -> str:
            asked.append(("index", url))
            return index

        def fetch_range(url: str, byte_range: ByteRange) -> bytes:
            asked.append(("range", url, byte_range))
            self.assertEqual((byte_range.start, byte_range.end), (100, 100 + len(record) - 1))
            return record

        root = Path(tempfile.mkdtemp(prefix="xue-debra-gfs-"))
        self.addCleanup(shutil.rmtree, root, True)
        source = ancillary.fetch_skin_temperature(slot, root, fetch_text=fetch_text, fetch_range=fetch_range, exists=exists)
        self.assertEqual((source.cycle, source.forecast_hour, source.path.name), (datetime(2026, 9, 16, 18, tzinfo=UTC), 9, "gfs_tmpsfc_2026091618_f009.grib2"))
        self.assertEqual(source.valid_time, datetime(2026, 9, 17, 3, tzinfo=UTC))
        self.assertEqual(source.path.read_bytes(), record)
        self.assertEqual([kind for kind, *_ in asked], ["exists", "exists", "index", "range"])
        self.assertEqual(asked[2][1], "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260916/18/atmos/gfs.t18z.pgrb2.0p25.f009.idx")
        self.assertEqual(ancillary.skin_temperature_source(source.path), source)
        # Cached: nothing is asked for the same hour again.
        again = ancillary.fetch_skin_temperature(datetime(2026, 9, 17, 3, 20, tzinfo=UTC), root, fetch_text=fetch_text, fetch_range=fetch_range, exists=exists)
        self.assertEqual((again, len(asked)), (source, 4))
        with self.assertRaisesRegex(DownloadError, "no GFS surface temperature"):
            ancillary.fetch_skin_temperature(slot, root / "empty", fetch_text=fetch_text, fetch_range=fetch_range, exists=lambda url: False)
        self.assertEqual(source.metadata()["validTime"], "2026-09-17T03:00:00Z")


@requires_gdal_warp
class ProducerTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-debra-"

    def setUp(self) -> None:
        super().setUp()
        self.ancillary = gobi_ancillary(self.root)

    def test_the_producer_reads_five_windows_on_every_imager_here(self) -> None:
        self.assertEqual((DEBRA.id, DEBRA.bundle_id, DEBRA.bundle_ids, DEBRA.outputs, DEBRA.ancillaries), ("shachen", "dustcf", ("dustcf", "zhouye"), ("dustcf", "zhouye"), ("camel", "skin")))
        self.assertIs(PRODUCERS["zhouye"], DEBRA)
        for platform in (HIMAWARI, GOES_EAST, METEOSAT):
            self.assertEqual(DEBRA.inputs_for(platform), ("ir039", "wv062", "ir086", "ir104", "ir123"))
        self.assertEqual(DEBRA.version, PRODUCERS["dustrgb"].version)
        with self.assertRaisesRegex(ConversionError, "ancillary root"):
            DEBRA.ancillary_for(HIMAWARI, GOBI_SLOT, None)
        with self.assertRaisesRegex(ConversionError, "pull-r2-ancillary"):
            DEBRA.ancillary_for(HIMAWARI, GOBI_SLOT, self.root / "nothing")
        resolved = DEBRA.ancillary_for(HIMAWARI, GOBI_SLOT, self.ancillary)
        self.assertEqual(resolved, {"camel": self.ancillary / "camel", "skin": self.ancillary / "gfs" / "gfs_tmpsfc_2026091412_f001.grib2"})

    def _references(self, scene: dict[str, np.ndarray], slot: datetime, resolved: dict[str, Path]):
        """shachen's own pipelines on the same plate carrée area: ZHOUYE
        with the day branch gated and re-blended through shachen's
        Eq. 22, and DEBRA (the ABI retune) gated as the product was
        before ZHOUYE — plus the staged coverage and the solar zenith."""
        import xarray as xr  # noqa: PLC0415
        from pyresample.geometry import AreaDefinition  # noqa: PLC0415
        from shachen.confidence import blend_confidence  # noqa: PLC0415
        from shachen.constants import ABI_TUNED  # noqa: PLC0415
        from shachen.pipeline import run_debra, run_zhouye  # noqa: PLC0415

        area = AreaDefinition(
            "gobi", "gobi", "gobi", {"proj": "longlat", "datum": "WGS84"}, GOBI_GRID.width, GOBI_GRID.height,
            (GOBI_GRID.west, GOBI_GRID.south, GOBI_GRID.east, GOBI_GRID.north),
        )
        names = {"ir039": "bt_swir_39", "wv062": "bt_wv_62", "ir086": "bt_tir_86", "ir104": "bt_tir_104", "ir123": "bt_tir_123"}
        reference_scene = xr.Dataset({names[channel_id]: xr.DataArray(plane, dims=("y", "x")) for channel_id, plane in scene.items()})
        reference_scene.attrs.update(area=area, start_time=slot.replace(tzinfo=None))
        skin = ancillary.read_skin_temperature(resolved["skin"])
        skin_da = xr.DataArray(skin.values, dims=("latitude", "longitude"), coords={"latitude": skin.latitudes, "longitude": skin.longitudes})
        staged = ancillary.read_staged_emissivity(self.ancillary / "camel" / "gobi" / "202609.nc")
        emissivity = xr.Dataset(
            {
                name: xr.DataArray(field.values, dims=("latitude", "longitude"), coords={"latitude": field.latitudes, "longitude": field.longitudes})
                for name, field in staged.fields.items()
            }
        )
        zhouye = run_zhouye(reference_scene, skin_da, emissivity)
        self.assertEqual(zhouye.attrs["scheme"], "zhouye")
        cf_day = split_window_gate(np.asarray(zhouye["cf_day"].values), np.asarray(zhouye["dt1"].values), np.asarray(zhouye["dt2"].values))
        expected = np.asarray(
            blend_confidence(xr.DataArray(cf_day, dims=("y", "x")), zhouye["cf_trm"], zhouye["cf_ngt"], zhouye["b_ngt_trm"], zhouye["b_trm_day"]).values
        )
        debra = run_debra(reference_scene, skin_da, emissivity, ABI_TUNED)
        self.assertEqual(debra.attrs["scheme"], "debra")
        debra_gated = split_window_gate(np.asarray(debra["cf_comb"].values), np.asarray(debra["dt1"].values), np.asarray(debra["dt2"].values))
        _, covered = ancillary.emissivity_on_grid([staged], GOBI_GRID)
        return expected, debra_gated, covered, np.asarray(zhouye["zenith_deg"].values)

    @requires_shachen
    def test_the_chain_is_shachen_s_run_debra_and_run_zhouye_cell_for_cell(self) -> None:
        """Composed here from the per-equation modules with this module's
        regrid, zenith and land mask, the two confidences equal what
        ``shachen.pipeline.run_debra`` (gated) and ``run_zhouye`` (its day
        factor gated and the blend redone through shachen's Eq. 22)
        compute on the same plate carrée area with their own — so nothing
        in the composition is this module's arithmetic but the three it
        takes over and the gate. At this slot the grid is in the night,
        where the two part: ZHOUYE's plume reads higher than DEBRA's."""
        scene = synthetic_scene(GOBI_GRID)
        resolved = DEBRA.ancillary_for(HIMAWARI, GOBI_SLOT, self.ancillary)
        produced = DEBRA.run(HIMAWARI, scene, resolved, slot=GOBI_SLOT, grid=GOBI_GRID)
        self.assertEqual(tuple(produced), ("dustcf", "zhouye"))
        actual, dustcf = produced["zhouye"], produced["dustcf"]
        expected, debra_gated, covered, zenith = self._references(scene, GOBI_SLOT, resolved)
        self.assertGreater(float(zenith.min()), 90.0)
        # Inside the staged box both are defined and agree; the Gobi is
        # land, so outside it this producer says nothing while shachen,
        # taking a missing emissivity as unity, still answers.
        self.assertTrue(np.isfinite(actual[covered]).all())
        np.testing.assert_allclose(actual[covered], expected[covered], atol=1e-9)
        np.testing.assert_allclose(dustcf[covered], debra_gated[covered], atol=1e-9)
        self.assertTrue(np.isnan(actual[~covered]).all())
        self.assertTrue(np.isnan(dustcf[~covered]).all())
        self.assertTrue(np.isfinite(expected[~covered]).any())
        # And the plume reads as dust, the quiet ground as nothing.
        plume = actual[GOBI_GRID.height * 2 // 5 : GOBI_GRID.height * 3 // 5, GOBI_GRID.width * 2 // 5 : GOBI_GRID.width * 3 // 5]
        self.assertGreater(float(np.nanmean(plume)), 0.3)
        self.assertLessEqual(float(np.nanmax(actual)), 1.0)
        quiet = actual[covered]
        self.assertGreater(float((quiet == 0.0).mean()), 0.7)
        # By night the two schemes part: the same plume reads higher in
        # ZHOUYE than in DEBRA.
        debra_plume = dustcf[GOBI_GRID.height * 2 // 5 : GOBI_GRID.height * 3 // 5, GOBI_GRID.width * 2 // 5 : GOBI_GRID.width * 3 // 5]
        self.assertGreater(float(np.nanmean(plume)), float(np.nanmean(debra_plume)))
        self.assertFalse(np.allclose(actual[covered], dustcf[covered]))

    @requires_shachen
    def test_by_day_the_two_confidences_are_one(self) -> None:
        """Below a solar zenith of 75° the Eq. 20 weight is 1 and ZHOUYE's
        three hooks touch nothing, so ``zhouye`` is ``dustcf`` bit for
        bit — DEBRA's ABI retune with the split-window gate — and both
        equal shachen's pipelines to the regrid's rounding."""
        scene = synthetic_scene(GOBI_GRID)
        resolved = DEBRA.ancillary_for(HIMAWARI, GOBI_DAY_SLOT, self.ancillary)
        produced = DEBRA.run(HIMAWARI, scene, resolved, slot=GOBI_DAY_SLOT, grid=GOBI_GRID)
        actual, dustcf = produced["zhouye"], produced["dustcf"]
        expected, debra_gated, covered, zenith = self._references(scene, GOBI_DAY_SLOT, resolved)
        self.assertLess(float(zenith.max()), 75.0)
        np.testing.assert_array_equal(actual[covered], dustcf[covered])
        np.testing.assert_allclose(actual[covered], expected[covered], atol=1e-9)
        np.testing.assert_allclose(dustcf[covered], debra_gated[covered], atol=1e-9)
        # The synthetic plume is a faint one on the day interval (its
        # floor at 0.40 of the sum): above the ramp's noise floor, not high.
        plume = actual[GOBI_GRID.height * 2 // 5 : GOBI_GRID.height * 3 // 5, GOBI_GRID.width * 2 // 5 : GOBI_GRID.width * 3 // 5]
        self.assertGreater(float(np.nanmean(plume)), 0.1)

    @requires_shachen
    def test_the_confidence_is_defined_over_water_and_staged_land_and_gated(self) -> None:
        """Water needs no climatology (DEBRA reads its emissivity as
        unity); land is defined inside a staged file and nothing outside;
        a cell any input or the skin temperature lacks is nothing; and a
        cell where neither split-window test responded reads 0."""
        # A grid off the Chinese coast: the Bohai and Yellow seas with the
        # Shandong peninsula, no staged file here.
        grid = TargetGrid(west=117.0, south=34.0, east=125.0, north=41.0, step=0.1)
        scene = synthetic_scene(grid, seed=2)
        scene["ir104"][3, 4] = np.nan
        camel = self.root / "coast" / "camel" / "gobi"
        camel.mkdir(parents=True)
        shutil.copy(DEBRA_FIXTURES / "camel.gobi.202609.crop.nc", camel / "202609.nc")
        gfs = self.root / "coast" / "gfs"
        gfs.mkdir(parents=True)
        # The Gobi record does not reach this grid: everything is nothing.
        cycle, forecast_hour = ancillary.skin_temperature_candidates(GOBI_SLOT)[0]
        shutil.copy(DEBRA_FIXTURES / "gfs.tmpsfc.gobi.grib2", gfs / ancillary.skin_temperature_name(cycle, forecast_hour))
        resolved = DEBRA.ancillary_for(HIMAWARI, GOBI_SLOT, self.root / "coast")
        nothing = DEBRA.run(HIMAWARI, scene, resolved, slot=GOBI_SLOT, grid=grid)["dustcf"]
        self.assertTrue(np.isnan(nothing).all())
        # With a record that does, water is defined and land is not.
        shutil.copy(DEBRA_FIXTURES / "gfs.tmpsfc.yellowsea.grib2", gfs / ancillary.skin_temperature_name(cycle, forecast_hour))
        out = DEBRA.run(HIMAWARI, scene, resolved, slot=GOBI_SLOT, grid=grid)["dustcf"]
        from global_land_mask import globe  # noqa: PLC0415

        lons = grid.first_longitude + np.arange(grid.width) * grid.step
        lats = grid.first_latitude - np.arange(grid.height) * grid.step
        lon2d, lat2d = np.meshgrid(lons, lats)
        land = globe.is_land(lat2d, lon2d)
        self.assertGreater(float(land.mean()), 0.2)
        self.assertLess(float(land.mean()), 0.8)
        self.assertTrue(np.isnan(out[land]).all())
        water = ~land
        water[3, 4] = False
        self.assertTrue(np.isfinite(out[water]).all())
        self.assertTrue(np.isnan(out[3, 4]))
        # The gate, on its own: only a cell with some split-window signal
        # keeps its confidence.
        cf = np.array([0.5, 0.5, 0.5, np.nan, 0.0])
        dt1 = np.array([0.0, 0.2, 0.0, 0.0, 0.0])
        dt2 = np.array([0.0, 0.0, 0.1, 0.0, np.nan])
        self.assertEqual(split_window_gate(cf, dt1, dt2).tolist()[:3], [0.0, 0.5, 0.5])
        self.assertTrue(np.isnan(split_window_gate(cf, dt1, dt2)[3]))
        self.assertEqual(split_window_gate(cf, dt1, dt2)[4], 0.0)
        with self.assertRaisesRegex(ConversionError, "missing"):
            DEBRA.run(HIMAWARI, {"ir104": scene["ir104"]}, resolved, slot=GOBI_SLOT, grid=grid)
        with self.assertRaisesRegex(ConversionError, "needs the 'skin' ancillary"):
            DEBRA.run(HIMAWARI, scene, {"camel": resolved["camel"]}, slot=GOBI_SLOT, grid=grid)


@requires_gdal_warp
class WindowTests(TempRoot, unittest.TestCase):
    """The producer in the fetch stage, on the Himawari fixture tiles."""

    root_prefix = "xue-debra-window-"

    def setUp(self) -> None:
        super().setUp()
        self.listing, self.download = bucket(fixture_keys())
        self.ancillary = stage_ancillary(self.root, (SLOT_0300, SLOT_0310))

    def fetch_window(self, **overrides):
        arguments = dict(
            grid=TILE_GRID,
            raw_root=self.root,
            destination=self.root / "himawari.2026091703",
            series_stem="himawari.2026091703",
            units={channel.id: VARIABLES[channel.id].output_unit for channel in CHANNELS},
            producers=(DEBRA,),
            ancillary_root=self.ancillary,
            fetch=self.listing,
            download=self.download,
        )
        arguments.update(overrides)
        return satellite_fetch.fetch_window(TWO_TILES, CHANNELS, SLOT_0300, 1, **arguments)

    @requires_shachen
    def test_a_slot_s_confidence_is_composed_once_against_the_staged_ancillary(self) -> None:
        window = self.fetch_window()
        self.assertEqual(list(window.series)[-2:], ["dustcf", "zhouye"])
        frame = window.slots[0].frames["dustcf"]
        self.assertEqual(frame, self.root / "himawari-frames" / "dustcf" / "dustcf_20260917030000.tif")
        sidecar = json.loads(assemble.packing_path(frame).read_text(encoding="utf-8"))
        self.assertEqual((sidecar["scale"], sidecar["offset"], sidecar["unit"]), (0.0001, 0.0, "1"))
        self.assertEqual(sidecar["producer"], {"id": "shachen", "version": DEBRA.version})
        self.assertEqual(sidecar["inputs"], [f"{channel_id}_20260917030000.tif" for channel_id in DEBRA.inputs])
        self.assertEqual(sidecar["ancillary"], {"camel": "camel", "skin": "gfs_tmpsfc_2026091700_f003.grib2"})
        plane = assemble.read_frame(frame, TILE_GRID)
        ir104 = assemble.read_frame(window.slots[0].frames["ir104"], TILE_GRID)
        covered = np.isfinite(ir104)
        # Water south of Japan: defined wherever the tiles are but on the
        # islands, which no staged file reaches; in 0–1 where it is.
        from global_land_mask import globe  # noqa: PLC0415

        lons = TILE_GRID.first_longitude + np.arange(TILE_GRID.width) * TILE_GRID.step
        lats = TILE_GRID.first_latitude - np.arange(TILE_GRID.height) * TILE_GRID.step
        lon2d, lat2d = np.meshgrid(lons, lats)
        land = globe.is_land(lat2d, lon2d)
        self.assertGreater(int(land.sum()), 0)
        self.assertEqual(np.isfinite(plane).tolist(), (covered & ~land).tolist())
        defined = np.isfinite(plane)
        self.assertGreaterEqual(float(plane[defined].min()), 0.0)
        self.assertLessEqual(float(plane[defined].max()), 1.0)
        # Composed once: a second window reads the frames back.
        with mock.patch.object(DebraProducer, "run", side_effect=AssertionError("recomposed")):
            again = self.fetch_window()
        self.assertEqual(again.series["dustcf"].read_bytes(), window.series["dustcf"].read_bytes())
        # The series carries the stamp the converter writes as the
        # producer block.
        from xuebuild import observation  # noqa: PLC0415
        from xuebuild.sources import source_spec  # noqa: PLC0415

        series = observation.inspect_observation(self.root / "himawari.2026091703", source_spec("himawari"), ("ir104", "dustcf"))
        self.assertEqual(series.producers, {"dustcf": ("shachen", DEBRA.version)})
        self.assertEqual(series.plane_sources["dustcf"].fill_replacement, -0.004)

    def test_a_window_without_the_ancillary_fails_before_it_composes(self) -> None:
        with self.assertRaisesRegex(ConversionError, "ancillary root"):
            self.fetch_window(ancillary_root=None)
        with self.assertRaisesRegex(ConversionError, "pull-r2-ancillary"):
            self.fetch_window(ancillary_root=self.root / "unstaged")


if __name__ == "__main__":
    unittest.main()
