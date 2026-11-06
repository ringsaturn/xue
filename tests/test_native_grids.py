"""Grids published as the model computes them (docs/format.md, "Regional
Grids" and "Projected Sources"; docs/zarr-profile.md, "Coordinates"): every
reader here, and the schemas, accept a schemaVersion 4 projected grid and a
version 3 ``gaussian`` block, refuse each malformed field, and keep reading
what they read before.
"""

from __future__ import annotations

import copy
import json
import math
import unittest
from typing import Any

import numpy as np
from jsonschema import Draft202012Validator

from tests.jsonschemas import _registry, _schema, check, errors
from xuebuild import zarrstore
from xuebuild.binformat import BundleMetadata
from xuebuild.errors import BundleError, ManifestError
from xuebuild.manifest import _validate_grid

PROJECTION = {
    "kind": "lambert-conformal",
    "radius": 6371229.0,
    "standardParallel1": 38.5,
    "standardParallel2": 38.5,
    "latitudeOfOrigin": 38.5,
    "centralMeridian": -97.5,
    "x0": -2697520.143,
    "y0": 1586693.847,
    "dx": 3000.0,
    "dy": -3000.0,
}
REGULAR_FIELDS = {
    "firstLongitude": -180.0,
    "firstLatitude": 90.0,
    "longitudeStep": 90.0,
    "latitudeStep": -90.0,
}
SHAPE = {
    "width": 4,
    "height": 3,
    "layout": "row-major",
    "rowOrder": "north-to-south",
    "columnOrder": "west-to-east",
    "wrapLongitude": False,
}
GAUSSIAN = {"n": 95, "firstRow": 2, "rowStride": 2}


def regular_grid(**extra: Any) -> dict[str, Any]:
    return {**SHAPE, **REGULAR_FIELDS, **extra}


def projected_grid(**edits: Any) -> dict[str, Any]:
    return {**SHAPE, "projection": {**PROJECTION, **edits}}


def metadata(version: int, grid: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": version,
        "model": "HRRR",
        "product": "natlev",
        "runTime": "2026-11-05T00:00:00Z",
        "time": {"unitSeconds": 3600, "firstFrameOffset": 0, "frameStep": 1, "frameCount": 2},
        "grid": grid,
        "variables": [
            {
                "numericId": 1,
                "id": "tmp2m",
                "label": "2 meter temperature",
                "unit": "°C",
                "parameter": {
                    "discipline": 0,
                    "parameterCategory": 0,
                    "parameterNumber": 0,
                    "typeOfFirstFixedSurface": 103,
                    "scaleFactorOfFirstFixedSurface": 0,
                    "scaledValueOfFirstFixedSurface": 2,
                },
                "quantization": {
                    "type": "linear",
                    "offset": -60.0,
                    "scale": 0.5,
                    "minimumCode": 0,
                    "maximumCode": 220,
                    "nodataCode": 255,
                },
            }
        ],
    }


def read(document: dict[str, Any]) -> BundleMetadata:
    return BundleMetadata(json.dumps(document).encode("utf-8"))


def without(block: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in block.items() if name != key}


MALFORMED_PROJECTIONS: list[tuple[str, dict[str, Any]]] = [
    ("unknown kind", {"kind": "octahedral-gaussian"}),
    ("null kind", {"kind": None}),
    ("zero radius", {"radius": 0.0}),
    ("string radius", {"radius": "6371229"}),
    ("boolean x0", {"x0": True}),
    ("null y0", {"y0": None}),
    ("polar parallel", {"standardParallel1": 95.0}),
    ("origin below the pole", {"latitudeOfOrigin": -91.0}),
    ("centralMeridian out of range", {"centralMeridian": 262.5}),
    ("westward dx", {"dx": -3000.0}),
    ("zero dx", {"dx": 0.0}),
    ("northward dy", {"dy": 3000.0}),
    ("unknown field", {"zone": 12}),
]
# What only the readers' own rules refuse: a schema's minimum and maximum are
# inclusive, and it cannot say a sum.
READER_ONLY_PROJECTIONS: list[tuple[str, dict[str, Any]]] = [
    ("parallel at a pole", {"standardParallel1": 90.0}),
    ("origin at a pole", {"latitudeOfOrigin": -90.0}),
    ("mirrored parallels", {"standardParallel2": -38.5}),
    ("zero dy", {"dy": 0.0}),
]
MALFORMED_GAUSSIANS: list[tuple[str, Any]] = [
    ("null", None),
    ("list", [95, 0, 1]),
    ("missing rowStride", {"n": 95, "firstRow": 0}),
    ("unknown field", {"n": 95, "firstRow": 0, "rowStride": 1, "rows": 190}),
    ("zero n", {"n": 0, "firstRow": 0, "rowStride": 1}),
    ("n too large", {"n": 32769, "firstRow": 0, "rowStride": 1}),
    ("fractional n", {"n": 95.5, "firstRow": 0, "rowStride": 1}),
    ("boolean n", {"n": True, "firstRow": 0, "rowStride": 1}),
    ("negative firstRow", {"n": 95, "firstRow": -1, "rowStride": 1}),
    ("zero rowStride", {"n": 95, "firstRow": 0, "rowStride": 0}),
]
# The grid's three rows run off the 190 of the Gaussian grid.
READER_ONLY_GAUSSIANS: list[tuple[str, Any]] = [
    ("past the last row", {"n": 95, "firstRow": 188, "rowStride": 1}),
    ("stride past the last row", {"n": 95, "firstRow": 0, "rowStride": 95}),
]


class BundleMetadataTest(unittest.TestCase):
    def test_a_projected_grid_is_version_4(self) -> None:
        parsed = read(metadata(4, projected_grid()))
        self.assertEqual((parsed.width, parsed.height, parsed.plane_length), (4, 3, 12))

    def test_the_version_and_the_projection_go_together(self) -> None:
        with self.assertRaisesRegex(BundleError, "must declare schemaVersion 4"):
            read(metadata(3, projected_grid()))
        with self.assertRaisesRegex(BundleError, "must declare schemaVersion 3"):
            read(metadata(4, regular_grid()))
        with self.assertRaisesRegex(BundleError, "unsupported"):
            read(metadata(5, projected_grid()))

    def test_neither_shape_on_version_4_is_rejected(self) -> None:
        with self.assertRaisesRegex(BundleError, "must declare schemaVersion 3"):
            read(metadata(4, dict(SHAPE)))

    def test_a_projected_grid_carries_nothing_of_the_regular_kind(self) -> None:
        for field in REGULAR_FIELDS:
            with self.subTest(field), self.assertRaisesRegex(BundleError, "regular grid fields"):
                read(metadata(4, {**projected_grid(), field: 1.0}))
            with self.subTest(f"null {field}"), self.assertRaisesRegex(BundleError, "regular grid fields"):
                read(metadata(4, {**projected_grid(), field: None}))
        with self.assertRaisesRegex(BundleError, "gaussian"):
            read(metadata(4, {**projected_grid(), "gaussian": GAUSSIAN}))

    def test_a_projected_grid_does_not_wrap(self) -> None:
        with self.assertRaisesRegex(BundleError, "wrapLongitude"):
            read(metadata(4, {**projected_grid(), "wrapLongitude": True}))
        with self.assertRaisesRegex(BundleError, "wrapLongitude"):
            read(metadata(4, without(projected_grid(), "wrapLongitude")))

    def test_malformed_projections_are_rejected(self) -> None:
        for label, edit in MALFORMED_PROJECTIONS + READER_ONLY_PROJECTIONS:
            with self.subTest(label), self.assertRaises(BundleError):
                read(metadata(4, projected_grid(**edit)))
        for field in PROJECTION:
            with self.subTest(f"without {field}"), self.assertRaises(BundleError):
                read(metadata(4, {**SHAPE, "projection": without(PROJECTION, field)}))
        with self.assertRaises(BundleError):
            read(metadata(4, {**SHAPE, "projection": None}))

    def test_json_non_finite_numbers_are_rejected(self) -> None:
        # Python's parser takes NaN and Infinity, which no other reader's does.
        for literal in ("NaN", "Infinity", "1e999"):
            text = json.dumps(metadata(4, projected_grid())).replace("-2697520.143", literal)
            with self.subTest(literal), self.assertRaisesRegex(BundleError, "x0"):
                BundleMetadata(text.encode("utf-8"))

    def test_a_gaussian_block_stays_on_version_3_and_is_validated(self) -> None:
        self.assertEqual(read(metadata(3, regular_grid(gaussian=GAUSSIAN))).height, 3)
        for label, block in MALFORMED_GAUSSIANS + READER_ONLY_GAUSSIANS:
            with self.subTest(label), self.assertRaises(BundleError):
                read(metadata(3, regular_grid(gaussian=block)))
        with self.assertRaises(BundleError):
            read(metadata(4, regular_grid(gaussian=GAUSSIAN)))

    def test_a_gaussian_block_needs_version_3(self) -> None:
        document = {
            "schemaVersion": 1,
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 2},
            "grid": regular_grid(gaussian=GAUSSIAN),
            "variables": [{"numericId": 1, "id": "tmp2m"}],
        }
        with self.assertRaisesRegex(BundleError, "requires schemaVersion 3"):
            read(document)

    def test_older_documents_keep_reading(self) -> None:
        version_1 = {
            "schemaVersion": 1,
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 2},
            "grid": {"width": 4, "height": 3},
            "variables": [{"numericId": 1, "id": "tmp2m"}],
        }
        self.assertEqual(read(version_1).frame_offsets, [0, 1])
        version_2 = copy.deepcopy(version_1)
        version_2["schemaVersion"] = 2
        version_2["time"] = {"firstForecastHour": 0, "hours": [0, 1, 3], "frameCount": 3}
        self.assertEqual(read(version_2).frame_offsets, [0, 1, 3])
        self.assertEqual(read(metadata(3, regular_grid())).frame_count, 2)
        # A version 3 document that never wrote the regular fields is an old
        # one's business, not a new rule's.
        self.assertEqual(read(metadata(3, dict(SHAPE))).width, 4)


class ManifestGridTest(unittest.TestCase):
    def test_both_kinds_are_admitted(self) -> None:
        _validate_grid(regular_grid(), "tmp2m")
        _validate_grid(regular_grid(gaussian=GAUSSIAN), "tmp2m")
        _validate_grid(projected_grid(), "tmp2m")

    def test_both_or_neither_is_rejected(self) -> None:
        with self.assertRaisesRegex(ManifestError, "regular grid fields"):
            _validate_grid({**projected_grid(), "firstLongitude": 0.0}, "tmp2m")
        with self.assertRaisesRegex(ManifestError, "firstLongitude"):
            _validate_grid(dict(SHAPE), "tmp2m")
        with self.assertRaisesRegex(ManifestError, "gaussian"):
            _validate_grid({**projected_grid(), "gaussian": GAUSSIAN}, "tmp2m")

    def test_malformed_blocks_are_rejected(self) -> None:
        for label, edit in MALFORMED_PROJECTIONS + READER_ONLY_PROJECTIONS:
            with self.subTest(label), self.assertRaises(ManifestError):
                _validate_grid(projected_grid(**edit), "tmp2m")
        for label, block in MALFORMED_GAUSSIANS + READER_ONLY_GAUSSIANS:
            with self.subTest(label), self.assertRaises(ManifestError):
                _validate_grid(regular_grid(gaussian=block), "tmp2m")
        with self.assertRaisesRegex(ManifestError, "wrapLongitude"):
            _validate_grid({**projected_grid(), "wrapLongitude": True}, "tmp2m")


class SchemaTest(unittest.TestCase):
    def manifest_grid_errors(self, grid: dict[str, Any]) -> list[Any]:
        reference = {"$ref": f"{_schema('manifest')['$id']}#/$defs/Grid"}
        return list(Draft202012Validator(reference, registry=_registry()).iter_errors(grid))

    def test_both_kinds_match(self) -> None:
        check("bundle-metadata", metadata(3, regular_grid()))
        check("bundle-metadata", metadata(3, regular_grid(gaussian=GAUSSIAN)))
        check("bundle-metadata", metadata(4, projected_grid()))
        self.assertEqual(self.manifest_grid_errors(regular_grid(gaussian=GAUSSIAN)), [])
        self.assertEqual(self.manifest_grid_errors(projected_grid()), [])

    def test_the_version_and_the_projection_go_together(self) -> None:
        self.assertTrue(errors("bundle-metadata", metadata(3, projected_grid())))
        self.assertTrue(errors("bundle-metadata", metadata(4, regular_grid())))
        self.assertTrue(errors("bundle-metadata", metadata(5, projected_grid())))

    def test_both_or_neither_does_not_match(self) -> None:
        for version in (3, 4):
            self.assertTrue(errors("bundle-metadata", metadata(version, dict(SHAPE))))
            self.assertTrue(errors("bundle-metadata", metadata(version, {**projected_grid(), **REGULAR_FIELDS})))
        self.assertTrue(errors("bundle-metadata", metadata(4, {**projected_grid(), "gaussian": GAUSSIAN})))
        self.assertTrue(errors("bundle-metadata", metadata(4, {**projected_grid(), "wrapLongitude": True})))
        # null is no way around the rule, in either direction.
        self.assertTrue(errors("bundle-metadata", metadata(3, regular_grid(projection=None))))
        self.assertTrue(errors("bundle-metadata", metadata(4, {**projected_grid(), "firstLongitude": None})))
        self.assertTrue(self.manifest_grid_errors(dict(SHAPE)))
        self.assertTrue(self.manifest_grid_errors({**projected_grid(), **REGULAR_FIELDS}))

    def test_malformed_blocks_do_not_match(self) -> None:
        for label, edit in MALFORMED_PROJECTIONS:
            with self.subTest(label):
                self.assertTrue(errors("bundle-metadata", metadata(4, projected_grid(**edit))))
                self.assertTrue(self.manifest_grid_errors(projected_grid(**edit)))
        for label, block in MALFORMED_GAUSSIANS:
            with self.subTest(label):
                self.assertTrue(errors("bundle-metadata", metadata(3, regular_grid(gaussian=block))))
                self.assertTrue(self.manifest_grid_errors(regular_grid(gaussian=block)))
        for field in PROJECTION:
            with self.subTest(f"without {field}"):
                grid = {**SHAPE, "projection": without(PROJECTION, field)}
                self.assertTrue(errors("bundle-metadata", metadata(4, grid)))

    def test_a_scalar_crs_array_and_projected_coordinates_match(self) -> None:
        check("store-array", crs_document())
        check("store-array", coordinate_document("y", "projection_y_coordinate"))
        check("store-array", coordinate_document("x", "projection_x_coordinate"))


def crs_document() -> dict[str, Any]:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [],
        "data_type": "int32",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": []}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "attributes": {
            "grid_mapping_name": "lambert_conformal_conic",
            "latitude_of_projection_origin": 38.5,
            "longitude_of_central_meridian": -97.5,
            "standard_parallel": [38.5, 38.5],
            "earth_radius": 6371229.0,
            "crs_wkt": 'PROJCS["Lambert_Conformal_Conic"]',
        },
        "dimension_names": [],
    }


def coordinate_document(name: str, standard_name: str) -> dict[str, Any]:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [3],
        "data_type": "float64",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": "NaN",
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "attributes": {"units": "m", "standard_name": standard_name},
        "dimension_names": [name],
    }


def bundle_entry(
    bundle: str, grid: dict[str, Any], dimensions: tuple[str, str, str], *, mapping: str | None = None, crs: bool = True
) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
    attributes = {"grid_mapping": mapping} if mapping else {}
    documents: dict[str, dict[str, Any]] = {
        bundle: {"dimension_names": list(dimensions), "attributes": attributes},
    }
    for name in dimensions:
        documents[name] = {"dimension_names": [name]}
    if crs:
        documents["crs"] = crs_document()
    return bundle, {"grid": grid, "variables": [{"id": bundle}]}, documents


class DimensionsTest(unittest.TestCase):
    def test_the_profile_admits_both_families_of_names(self) -> None:
        for names in (
            ("time", "latitude", "longitude"),
            ("time_prate", "latitude_pgb", "longitude_pgb"),
            ("time", "y", "x"),
            ("time_prate", "y", "x"),
            ("time", "y_wave", "x_wave"),
        ):
            self.assertEqual(zarrstore._check_dimensions("prate", names), names)

    def test_anything_else_is_refused(self) -> None:
        for names in (
            ("time", "y", "longitude"),
            ("time", "latitude", "x"),
            ("time", "x", "y"),
            ("time", "y_a", "x_b"),
            ("time", "y_Wave", "x_Wave"),
            ("time_tmp2m", "y", "x"),
        ):
            with self.subTest(names), self.assertRaises(BundleError):
                zarrstore._check_dimensions("prate", names)

    def test_the_names_must_agree_with_the_grid(self) -> None:
        zarrstore._check_dimensions("prate", ("time", "y", "x"), projected=True)
        zarrstore._check_dimensions("prate", ("time", "latitude", "longitude"), projected=False)
        with self.assertRaises(BundleError):
            zarrstore._check_dimensions("prate", ("time", "y", "x"), projected=False)
        with self.assertRaises(BundleError):
            zarrstore._check_dimensions("prate", ("time", "latitude", "longitude"), projected=True)


class GroupDocumentTest(unittest.TestCase):
    PROJECTED = ("time", "y", "x")
    REGULAR = ("time", "latitude", "longitude")

    def test_a_projected_bundle_brings_its_coordinates_and_crs(self) -> None:
        document = zarrstore.group_document(
            [bundle_entry("tmp2m", projected_grid(), self.PROJECTED, mapping="crs")],
        )
        self.assertEqual(list(document["consolidated_metadata"]["metadata"]), ["tmp2m", "time", "y", "x", "crs"])

    def test_a_regular_bundle_is_listed_as_it_always_was(self) -> None:
        document = zarrstore.group_document([bundle_entry("tmp2m", regular_grid(), self.REGULAR, crs=False)])
        self.assertEqual(
            list(document["consolidated_metadata"]["metadata"]),
            ["tmp2m", "time", "latitude", "longitude"],
        )

    def test_the_merge_keeps_the_crs(self) -> None:
        group = zarrstore.group_document(
            [bundle_entry("tmp2m", projected_grid(), self.PROJECTED, mapping="crs")],
        )
        merged = zarrstore.merge_group_documents(["tmp2m"], [group])
        self.assertEqual(merged, group)

    def test_names_and_mapping_must_agree_with_the_grid(self) -> None:
        for entry in (
            bundle_entry("tmp2m", regular_grid(), self.PROJECTED),
            bundle_entry("tmp2m", projected_grid(), self.REGULAR, mapping="crs"),
            bundle_entry("tmp2m", projected_grid(), self.PROJECTED),
            bundle_entry("tmp2m", regular_grid(), self.REGULAR, mapping="crs"),
            bundle_entry("tmp2m", projected_grid(), self.PROJECTED, mapping="crs", crs=False),
        ):
            with self.assertRaises(BundleError):
                zarrstore.group_document([entry])

    def test_the_crs_must_be_a_scalar_shared_by_its_bundles(self) -> None:
        entry = bundle_entry("tmp2m", projected_grid(), self.PROJECTED, mapping="crs")
        entry[2]["crs"]["shape"] = [1]
        with self.assertRaisesRegex(BundleError, "scalar"):
            zarrstore.group_document([entry])
        first = bundle_entry("tmp2m", projected_grid(), self.PROJECTED, mapping="crs")
        second = bundle_entry("prate", projected_grid(), self.PROJECTED, mapping="crs")
        second[2]["crs"]["attributes"]["earth_radius"] = 6371000.0
        with self.assertRaisesRegex(BundleError, "differs"):
            zarrstore.group_document([first, second])


def gaussian_latitude(n: int, row: int) -> float:
    """docs/zarr-profile.md, "Coordinates", operation for operation, the same
    as ``rust/xue/src/projection.rs``."""
    order = 2 * n
    degree = float(order)
    x = math.cos(math.pi * (row + 0.75) / (degree + 0.5))
    for _ in range(100):
        previous, current = 1.0, x
        for k in range(2, order + 1):
            previous, current = current, ((2.0 * k - 1.0) * x * current - (k - 1.0) * previous) / k
        derivative = degree * (x * current - previous) / (x * x - 1.0)
        step = current / derivative
        x -= step
        if abs(step) < 1e-15:
            break
    return math.degrees(math.asin(x))


GOLDEN_LATITUDES = {
    95: {
        0: 89.27671287810583,
        1: 88.33975425118209,
        2: 87.3972623773714,
        94: 0.4724393175759951,
        95: -0.4724393175759951,
        187: -87.3972623773714,
        188: -88.33975425118209,
        189: -89.27671287810583,
    },
    768: {
        0: 89.91032453466268,
        1: 89.79415738840316,
        2: 89.67730421235662,
        767: 0.058574679619089555,
        768: -0.058574679619089506,
        1533: -89.67730421235662,
        1534: -89.79415738840316,
        1535: -89.91032453466268,
    },
}
# One unit in the last place of the iterate is up to 3.6e-12 degrees next to a
# pole at n = 768, and the C libraries behind the first guess and the final
# asin differ across platforms. The two encoders' bit-for-bit agreement on one
# machine is the parity tests' to hold.
GOLDEN_TOLERANCE = 1e-11


class GaussianLatitudesTest(unittest.TestCase):
    def test_the_iteration_lands_on_the_goldens(self) -> None:
        for n, rows in GOLDEN_LATITUDES.items():
            for row, latitude in rows.items():
                with self.subTest(n=n, row=row):
                    self.assertAlmostEqual(gaussian_latitude(n, row), latitude, delta=GOLDEN_TOLERANCE)

    def test_the_goldens_are_the_legendre_roots(self) -> None:
        for n, rows in GOLDEN_LATITUDES.items():
            nodes, _ = np.polynomial.legendre.leggauss(2 * n)
            roots = np.degrees(np.arcsin(nodes))[::-1]
            for row, latitude in rows.items():
                with self.subTest(n=n, row=row):
                    self.assertAlmostEqual(float(roots[row]), latitude, delta=GOLDEN_TOLERANCE)


if __name__ == "__main__":
    unittest.main()
