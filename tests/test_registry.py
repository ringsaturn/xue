"""The registry document (docs/registry.md): the committed snapshot
describes this tree's tables, and the document is well formed."""

from __future__ import annotations

import json
import unittest

from tests._support import FIXTURES
from xuebuild.registry import REGISTRY_SCHEMA, document, document_json

# Inside the crate, so the native encoder can embed it; one copy only.
SNAPSHOT = FIXTURES.parents[1] / "rust" / "xue" / "registry.json"

CONVERSION_RULES = {
    "none",
    "celsius",
    "soil-temperature",
    "precipitation-rate",
    "precipitation-accumulation",
    "pascal-to-hectopascal",
    "metre-to-kilometre",
    "mass-ratio-to-g-per-kg",
    "proportion-to-percent",
    "cloud-cover",
    "degrees-true",
    "geopotential-to-metres",
    "code-table",
}
MATCH_RULES = {
    "surface",
    "height",
    "entire-atmosphere",
    "mean-sea-level",
    "isobaric",
    "cloud-layer",
    "template",
    "template-aerosol",
    "template-rank",
    "template-mrms",
    "total-precipitation",
    "local-parameter",
    "phrase",
    "geopotential-surface",
}
DERIVATIONS = {
    "vapour-flux",
    "wave-vector",
    "theta-e",
    "precipitation-type",
    "clear-air-turbulence",
    "precipitation-rate",
}


class RegistryDocumentTests(unittest.TestCase):
    def test_the_committed_snapshot_still_describes_this_tree(self) -> None:
        self.assertEqual(
            SNAPSHOT.read_text(encoding="utf-8"),
            document_json(),
            "the tables moved: regenerate with `xue registry --output rust/xue/registry.json` "
            "and say so in the commit",
        )

    def test_the_snapshot_is_the_document_round_tripped(self) -> None:
        self.assertEqual(json.loads(SNAPSHOT.read_text(encoding="utf-8")), document())

    def test_every_name_is_in_its_closed_set(self) -> None:
        doc = document()
        self.assertEqual(doc["registrySchema"], REGISTRY_SCHEMA)
        for variable_id, entry in doc["variables"].items():
            with self.subTest(variable=variable_id):
                self.assertIn(entry["conversion"]["rule"], CONVERSION_RULES)
                for rule in entry["match"]["gdal"]:
                    self.assertIn(rule["rule"], MATCH_RULES)
                self.assertIn(entry["predictor"], {"delta", "raw"})
                self.assertIn(entry["decimation"], {"sample", "peak"})
                self.assertTrue(set(entry["companions"]) <= {"poster", "video"})
                for profile, codebook in entry.get("quantization", {}).items():
                    self.assertIn(profile, {"quality", "compact", "balanced"})
                    self.assertIn(codebook["type"], {"linear", "log1p"})
        for bundle_id, entry in doc["bundles"].items():
            with self.subTest(bundle=bundle_id):
                self.assertIn(entry["kind"], {"scalar", "vector", "composite", "volume"})
                if "derivation" in entry:
                    self.assertIn(entry["derivation"]["kind"], DERIVATIONS)

    def test_every_reference_resolves(self) -> None:
        doc = document()
        variables = doc["variables"]
        bundles = doc["bundles"]
        for bundle_id, entry in bundles.items():
            with self.subTest(bundle=bundle_id):
                for member in entry.get("members", ()):
                    self.assertIn(member, variables)
                for input_id in entry.get("derivation", {}).get("inputs", ()):
                    self.assertIn(input_id, variables)
                for input_id in entry.get("inputs", ()):
                    self.assertIn(input_id, variables)
                if entry["kind"] == "vector":
                    self.assertEqual(len(entry["members"]), 2)
        for source_id, source in doc["sources"].items():
            with self.subTest(source=source_id):
                for key in ("bundleScalarIds", "bundleVectorIds", "bundleCompositeIds", "bundleVolumeIds"):
                    for bundle_id in source[key]:
                        self.assertTrue(bundle_id in variables or bundle_id in bundles, bundle_id)
                for variable_id in source["inputVariableIds"]:
                    self.assertIn(variable_id, variables)

    def test_a_published_variable_carries_every_profile(self) -> None:
        for variable_id, entry in document()["variables"].items():
            if "quantization" in entry:
                with self.subTest(variable=variable_id):
                    self.assertEqual(set(entry["quantization"]), {"quality", "compact", "balanced"})


if __name__ == "__main__":
    unittest.main()
