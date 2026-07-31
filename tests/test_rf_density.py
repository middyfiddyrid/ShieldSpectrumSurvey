"""rf_density: broadcast density + cellular tower proximity classification."""
import json
import unittest

from survey.report import rf_density


def fm(dist):
    return {"confidence": "record", "service": "FM", "distance_mi": dist,
            "category": "broadcast"}


def cell(carrier, towers, dist=None, source="OpenCellID"):
    return {"confidence": "block", "service": "CELL", "category": "cellular",
            "licensee": carrier, "distance_mi": dist, "source": source,
            "raw_json": json.dumps({"tower_count": towers})}


class TestRfDensity(unittest.TestCase):
    def test_broadcast_tiers(self):
        self.assertEqual(rf_density([fm(5)] * 25)["tier"], "high")
        self.assertEqual(rf_density([fm(5)] * 10)["tier"], "moderate")
        self.assertEqual(rf_density([fm(5)] * 3)["tier"], "low")

    def test_no_cell_data_is_unknown(self):
        d = rf_density([fm(5)] * 3)
        self.assertEqual(d["cell_tier"], "unknown")
        self.assertEqual(d["cell_count"], 0)
        self.assertIsNone(d["cell_nearest_mi"])

    def test_close_tower_is_high_and_recommends(self):
        d = rf_density([fm(5)] * 3 + [cell("Verizon", 4, dist=0.3,
                                           source="CellMapper (import)")])
        self.assertEqual(d["cell_tier"], "high")
        self.assertTrue(d["recommend"])
        self.assertAlmostEqual(d["cell_nearest_mi"], 0.3)

    def test_band_rows_not_double_counted(self):
        # one carrier emits a row per band block, all carrying the same count
        rows = [cell("Verizon", 12) for _ in range(6)]
        self.assertEqual(rf_density(rows)["cell_count"], 12)

    def test_per_carrier_max_across_sources(self):
        rows = [cell("Verizon", 12, source="OpenCellID"),
                cell("Verizon", 20, dist=2.0, source="CellMapper (import)"),
                cell("T-Mobile", 5, source="OpenCellID")]
        d = rf_density(rows)
        self.assertEqual(d["cell_count"], 25)
        self.assertEqual(d["cell_tier"], "moderate")  # 25 >= 15, nearest 2.0

    def test_low_cellular_does_not_recommend(self):
        d = rf_density([fm(5)] * 3 + [cell("AT&T", 2, dist=3.0)])
        self.assertEqual(d["cell_tier"], "low")
        self.assertFalse(d["recommend"])


if __name__ == "__main__":
    unittest.main()
