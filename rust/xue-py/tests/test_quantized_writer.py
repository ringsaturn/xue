"""Public xuepy writer contract, independent of the site's xuebuild pipeline."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import xue


class QuantizedWriterTest(unittest.TestCase):
    def test_custom_variable_round_trip_in_both_prediction_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {
                "schemaVersion": 1,
                "grid": {"width": 256, "height": 1},
                "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 8},
                "variables": [{"numericId": 48, "id": "custom", "quantization": {"nodataCode": 255}}],
            }
            frames = []
            for offset in range(8):
                source = root / f"{offset}.u8"
                (np.arange(256, dtype=np.uint8) + offset).tofile(source)
                frames.append((offset, source))
            for grouped in (False, True):
                with self.subTest(grouped=grouped):
                    output = root / "custom.xue"
                    size = xue.write_quantized_bundle(
                        output, json.dumps(metadata), frames, grouped=grouped, zstd_level=3
                    )
                    self.assertEqual(size, output.stat().st_size)
                    bundle = xue.Bundle.open(output)
                    self.assertIsNone(bundle.verify())
                    self.assertEqual(bundle.metadata, metadata)
                    for offset, source in reversed(frames):
                        np.testing.assert_array_equal(bundle.decode(48, offset), np.fromfile(source, dtype=np.uint8))

            previous = output.read_bytes()
            frames[-1][1].write_bytes(b"bad length")
            with self.assertRaisesRegex(RuntimeError, "plane length"):
                xue.write_quantized_bundle(output, json.dumps(metadata), frames)
            self.assertEqual(output.read_bytes(), previous)

            damaged = bytearray(previous)
            damaged[int.from_bytes(damaged[56:64], "little")] ^= 1
            with self.assertRaises(ValueError):
                xue.Bundle(bytes(damaged)).verify()


if __name__ == "__main__":
    unittest.main()
