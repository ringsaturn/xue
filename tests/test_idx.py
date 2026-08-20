import unittest

from xue.errors import DownloadError
from xue.idx import ecmwf_field_byte_range, field_byte_range, parse_index, target_byte_range


INDEX = """1:0:d=2026081406:PRMSL:mean sea level:anl:
2:120:d=2026081406:TMP:2 m above ground:anl:
3:1120:d=2026081406:SPFH:2 m above ground:anl:
"""


class IndexTests(unittest.TestCase):
    def test_parses_records_and_target_offsets(self) -> None:
        records = parse_index(INDEX)
        self.assertEqual(records[1].number, 2)
        self.assertEqual(target_byte_range(INDEX).start, 120)
        self.assertEqual(target_byte_range(INDEX).end, 1119)
        self.assertEqual(target_byte_range(INDEX).length, 1000)

    def test_final_record_uses_file_size(self) -> None:
        index = "1:0:PRMSL:mean sea level:\n2:50:TMP:2 m above ground:\n"
        self.assertEqual(target_byte_range(index, 90).end, 89)

    def test_selects_instantaneous_precipitation_rate(self) -> None:
        index = """1:0:d=x:PRATE:surface:1 hour fcst:
2:100:d=x:PRATE:surface:0-1 hour ave fcst:
3:200:d=x:APCP:surface:0-1 hour acc fcst:
"""
        selected = field_byte_range(
            index,
            ":PRATE:surface:",
            excluded_phrases=("ave fcst",),
        )
        self.assertEqual((selected.start, selected.end), (0, 99))

    def test_rejects_missing_duplicate_and_unordered_target(self) -> None:
        with self.assertRaises(DownloadError):
            target_byte_range("1:0:TMP:surface:\n2:10:WIND:surface:\n")
        with self.assertRaises(DownloadError):
            target_byte_range(INDEX + "4:2000:d=x:TMP:2 m above ground:x:\n")
        with self.assertRaises(DownloadError):
            parse_index("1:20:a\n2:10:b\n")

    def test_ecmwf_index_selects_exact_field(self) -> None:
        index = (
            '{"domain": "g", "step": "3", "levtype": "sfc", "param": "tp", "_offset": 0, "_length": 714444}\n'
            '{"domain": "g", "step": "3", "levtype": "sfc", "param": "10u", "_offset": 12028183, "_length": 870142}\n'
            '{"domain": "g", "step": "3", "levtype": "pl", "param": "2t", "_offset": 90, "_length": 10}\n'
            '{"domain": "g", "step": "3", "levtype": "sfc", "param": "2t", "_offset": 20304843, "_length": 646445}\n'
        )
        selected = ecmwf_field_byte_range(index, "2t")
        self.assertEqual((selected.start, selected.end), (20304843, 20304843 + 646445 - 1))
        self.assertEqual(ecmwf_field_byte_range(index, "tp").length, 714444)

    def test_ecmwf_index_rejects_missing_duplicate_and_invalid(self) -> None:
        line = '{"levtype": "sfc", "param": "2t", "_offset": 0, "_length": 10}\n'
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range(line, "tp")
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range(line + line, "2t")
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range('{"levtype": "sfc", "param": "2t", "_offset": -1, "_length": 10}\n', "2t")
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range("not json\n", "2t")


if __name__ == "__main__":
    unittest.main()
