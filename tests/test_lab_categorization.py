"""A LAB/BENCH session must not drown the operator in anomalies.

The operator declared the space their own bench, so unexplained emitters are
expected gear, not threats: they downgrade from the anomaly buckets
(priority_anomaly / unexplained_watch) to known_environment. This must be
HARD-GATED on is_lab — a FIELD sweep is byte-for-byte unchanged — and it must
never touch inventory categories or hide anything.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from survey.post_sweep import _category_for


def _group(**row):
    return {"best": row}


class LabCategorizationTest(unittest.TestCase):
    def test_field_substantive_is_priority_anomaly(self):
        g = _group()
        self.assertEqual(_category_for(g, True, is_lab=False), "priority_anomaly")

    def test_lab_substantive_downgrades_to_known_environment(self):
        g = _group()
        self.assertEqual(_category_for(g, True, is_lab=True), "known_environment")

    def test_field_unexplained_is_watch(self):
        g = _group(classification="UNVERIFIED")
        self.assertEqual(_category_for(g, False, is_lab=False), "unexplained_watch")

    def test_lab_unexplained_downgrades_to_known_environment(self):
        g = _group(classification="UNVERIFIED")
        self.assertEqual(_category_for(g, False, is_lab=True), "known_environment")

    def test_lab_leaves_client_inventory_alone(self):
        # a client-owned device stays client_inventory even in a lab session
        g = _group(inventory_matches=[{"owned": True}])
        self.assertEqual(_category_for(g, True, is_lab=True), "client_inventory")

    def test_field_behavior_is_unchanged_default(self):
        # default is_lab=False must reproduce the historical result exactly
        self.assertEqual(_category_for(_group(), True), "priority_anomaly")
        self.assertEqual(_category_for(_group(classification="UNVERIFIED"), False),
                         "unexplained_watch")


if __name__ == "__main__":
    unittest.main()
