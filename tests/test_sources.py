"""Every source's identity strings, in one table.

A manifest names its source by ``model`` and ``product``, the live pointer
by its file name, and a live run must carry the source's core bundles; the
shell and the STAC catalog key on the same strings, so a change to any of
them is a deploy-order change, not a rename. The per-source modules test
what is particular to each source.
"""

from __future__ import annotations

import unittest

from xuebuild.binconvert import published_bundle_ids
from xuebuild.sources import MODEL_CORE_BUNDLES, MODEL_PRODUCTS, SOURCES, source_spec

FORECAST_CORE = ("tmp2m", "prate")

#: source id -> (manifest model, product, live pointer, core bundles)
IDENTITIES = {
    "gfs": ("GFS", "pgrb2.0p25", "latest.json", FORECAST_CORE),
    "ecmwf": ("ECMWF", "ifs-0p25", "latest-ecmwf.json", FORECAST_CORE),
    "aifs": ("AIFS", "aifs-single-0p25", "latest-aifs.json", FORECAST_CORE),
    "ifshres": ("ECMWF-HRES", "ifs-hres-0p1", "latest-ifshres.json", FORECAST_CORE),
    "sflux": ("GFS-SFLUX", "sfluxgrb", "latest-sflux.json", FORECAST_CORE),
    "hrrr": ("HRRR", "wrfsfc", "latest-hrrr.json", FORECAST_CORE),
    "gefsaero": ("GEFS-AEROSOLS", "chem-a2d-0p25", "latest-gefsaero.json", ("aod",)),
    "cfs": ("CFSv2", "time-grib-01", "latest-cfs.json", FORECAST_CORE),
    # The manifest identity outlived the source id's change from `radar`.
    "cma": ("CMA-RADAR", "l3-mst-cref", "latest-cma.json", ("cref",)),
    "mrms": ("NOAA-MRMS", "conus-cref", "latest-mrms.json", ("cref",)),
    "mrms3d": ("NOAA-MRMS3D", "conus-refl3d", "latest-mrms3d.json", ("refl3d",)),
    "jma": ("JMA-HRPNS", "japan-prate", "latest-jma.json", ("prate",)),
    "himawari": ("HIMAWARI", "ahi-fldk-0p04", "latest-himawari.json", ("ir104",)),
    "goeseast": ("GOES-EAST", "abi-fldk-0p04", "latest-goeseast.json", ("ir104",)),
    "goeswest": ("GOES-WEST", "abi-fldk-0p04", "latest-goeswest.json", ("ir104",)),
    "meteosat": ("METEOSAT", "fci-fldk-0p04", "latest-meteosat.json", ("ir104",)),
    "aurora": ("SWPC-AURORA", "ovation-aurora-1p00", "latest-aurora.json", ("aurora",)),
}


class SourceIdentityTests(unittest.TestCase):
    def test_the_table_is_every_registered_source(self) -> None:
        self.assertEqual(set(IDENTITIES), set(SOURCES))

    def test_each_source_carries_its_identity_strings(self) -> None:
        for source_id, (model, product, pointer, core) in IDENTITIES.items():
            with self.subTest(source=source_id):
                spec = source_spec(source_id)
                self.assertEqual(spec.id, source_id)
                self.assertEqual((spec.manifest_model, spec.product, spec.latest_filename), (model, product, pointer))
                self.assertEqual(spec.core_bundle_ids, core)

    def test_the_manifest_tables_follow_the_registry(self) -> None:
        for source_id, (model, product, _, core) in IDENTITIES.items():
            with self.subTest(source=source_id):
                self.assertEqual(MODEL_PRODUCTS[model], product)
                self.assertEqual(MODEL_CORE_BUNDLES[model], core)
        self.assertEqual(set(MODEL_PRODUCTS), {model for model, *_ in IDENTITIES.values()})
        self.assertEqual(set(MODEL_CORE_BUNDLES), set(MODEL_PRODUCTS))

    def test_series_companions_mirror_published_bundles(self) -> None:
        for source_id, spec in SOURCES.items():
            with self.subTest(source=source_id):
                self.assertLessEqual(set(spec.series_bundle_ids), set(published_bundle_ids(spec)))
        self.assertEqual(source_spec("sflux").series_bundle_ids, ("tmp2m", "prate", "dswrf", "wind10m"))

    def test_long_cycles_extend_past_the_published_axis(self) -> None:
        for source_id, spec in SOURCES.items():
            with self.subTest(source=source_id):
                self.assertEqual(bool(spec.long_cycles), bool(spec.long_cycle_steps))
                if spec.long_cycle_steps:
                    self.assertGreater(spec.long_cycle_steps[0][0], spec.steps[-1][0])
                    self.assertTrue(all(0 <= hour < 24 and hour % spec.cycle_hours == 0 for hour in spec.long_cycles))
        self.assertEqual([source_id for source_id, spec in SOURCES.items() if spec.long_cycles], ["hrrr"])


if __name__ == "__main__":
    unittest.main()
