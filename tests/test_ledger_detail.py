"""F8 ledger detail: next-step guidance, historic segment review endpoint, and
the on-demand per-signal spectrum image (thumbnail + full).

Pure-python next_step_for rules first, then Flask-client coverage of
/api/segments/<id>, /api/signals/<id>/image.svg, and the enriched /api/signals
rows (snr_db + next_step). No radio, synthetic rows throughout.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.nextstep import next_step_for   # noqa: E402
from survey.server import create_app    # noqa: E402


def _sig(**kw):
    base = {"center_mhz": 150.0, "bw_khz": 200.0, "peak_dbm": -60.0,
            "avg_dbm": -70.0, "floor_dbm": -100.0, "persistence": 0.9,
            "classification": "UNVERIFIED", "identity_tier": "ANOMALY",
            "caution": 0, "matched_label": "", "match_reason": "",
            "triage_status": "unreviewed"}
    base.update(kw)
    return base


class TestNextStep(unittest.TestCase):
    def test_triaged_row_is_done(self):
        ns = next_step_for(_sig(triage_status="cleared"))
        self.assertEqual(ns["kind"], "done")

    def test_analyze_verdict_prompts_decision(self):
        ns = next_step_for(_sig(analysis_summary="Narrowband analog voice."))
        self.assertEqual(ns["kind"], "review")
        self.assertIn("Narrowband analog voice.", ns["detail"])

    def test_gps_band_is_urgent(self):
        ns = next_step_for(_sig(center_mhz=1575.42))
        self.assertEqual(ns["kind"], "urgent")

    def test_identified_offers_cross_off(self):
        ns = next_step_for(_sig(classification="VERIFIED",
                                identity_tier="IDENTIFIED",
                                matched_label="WXYZ FM"))
        self.assertEqual(ns["kind"], "clear")
        self.assertIn("WXYZ FM", ns["detail"])

    def test_uplink_gets_phone_correlation_test(self):
        ns = next_step_for(_sig(center_mhz=1712.0))
        self.assertEqual(ns["kind"], "test")
        self.assertIn("airplane", ns["detail"])

    def test_dect_gets_handset_check(self):
        ns = next_step_for(_sig(center_mhz=1921.0))
        self.assertEqual(ns["kind"], "test")
        self.assertIn("DECT", ns["detail"])

    def test_wide_24ghz_points_at_inventory(self):
        ns = next_step_for(_sig(center_mhz=2437.0, bw_khz=20000.0))
        self.assertEqual(ns["kind"], "inventory")

    def test_narrowband_ism_suggests_nfm_listen(self):
        ns = next_step_for(_sig(center_mhz=433.92, bw_khz=12.0))
        self.assertEqual(ns["kind"], "listen")
        self.assertEqual(ns.get("mode"), "nfm")
        self.assertIn("ISM", ns["detail"])

    def test_bursty_suggests_identify(self):
        ns = next_step_for(_sig(persistence=0.1))
        self.assertEqual(ns["kind"], "identify")

    def test_default_is_listen_ladder(self):
        ns = next_step_for(_sig())
        self.assertEqual(ns["kind"], "listen")

    def test_missing_fields_tolerated(self):
        ns = next_step_for({"center_mhz": 100.0})
        self.assertIn("kind", ns)

    def test_blinded_cue_audio_and_client_space(self):
        from survey.nextstep import blinded_cue_for
        # narrow, persistent, analog band, louder inside → bug in client space
        c = blinded_cue_for(_sig(center_mhz=433.9, bw_khz=12, persistence=0.9,
                                 interior_delta_db=14, localization_score=0.7))
        self.assertTrue(c["carries_audio"])
        self.assertEqual(c["space"], "likely_client")
        self.assertFalse(c["listen_permitted"])          # not attributed yet

    def test_blinded_cue_respects_operator_attribution(self):
        from survey.nextstep import blinded_cue_for
        c = blinded_cue_for(_sig(center_mhz=433.9, bw_khz=12, persistence=0.9,
                                 attribution_status="third_party"))
        self.assertEqual(c["space"], "not_client")
        c2 = blinded_cue_for(_sig(center_mhz=433.9, bw_khz=12, persistence=0.9,
                                  attribution_status="client_space"))
        self.assertEqual(c2["space"], "client")
        self.assertTrue(c2["listen_permitted"])          # client space → decode allowed

    def test_blinded_cue_wideband_not_audio(self):
        from survey.nextstep import blinded_cue_for
        c = blinded_cue_for(_sig(center_mhz=2437, bw_khz=20000, persistence=0.3))
        self.assertFalse(c["carries_audio"])

    def test_priority_gps_beats_identified(self):
        # rule order: GPS urgency outranks a baseline match
        ns = next_step_for(_sig(center_mhz=1575.42, classification="VERIFIED",
                                identity_tier="IDENTIFIED"))
        self.assertEqual(ns["kind"], "urgent")


SEG_DATA = {"freqs": [400.0 + i * 0.05 for i in range(40)],
            "avg": [-100.0] * 40,
            "maxpeak": [-100.0 + (30 if 18 <= i <= 21 else 0) for i in range(40)],
            "hits": [5 if 18 <= i <= 21 else 0 for i in range(40)],
            "passes": 5}


class TestLedgerEndpoints(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))
        self.seg = self.db.execute(
            "INSERT INTO segments (sweep_id,low_mhz,high_mhz,noise_floor_dbm,"
            "avg_passes,maxhold_dwell_s,status,data_json) VALUES (?,?,?,?,?,?,?,?)",
            (self.sweep, 400.0, 401.95, -100.0, 5, 4.0, "done",
             json.dumps(SEG_DATA)))
        self.s1 = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,segment_id,center_mhz,bw_khz,"
            "peak_dbm,avg_dbm,floor_dbm,persistence,classification,phase,"
            "bin_start,bin_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.sweep, self.sid, self.seg, 400.95, 200.0, -70.0, -80.0, -100.0,
             1.0, "UNVERIFIED", "interior", 18, 21))
        # a signal with NO segment trace (legacy / erased segment)
        self.s2 = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz,bw_khz,peak_dbm,"
            "floor_dbm,persistence,classification,phase) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.sweep, self.sid, 433.92, 12.0, -60.0, -100.0, 0.9,
             "UNVERIFIED", "interior"))
        self.app = create_app(self.db, self.cfg)
        self.c = self.app.test_client()

    def test_migration_added_columns(self):
        row = self.db.query_one("SELECT bin_start, bin_end, analysis_summary, "
                                "analysis_utc FROM signals WHERE id=?", (self.s1,))
        self.assertEqual((row["bin_start"], row["bin_end"]), (18, 21))
        self.assertEqual(row["analysis_summary"], "")

    def test_set_signal_analysis(self):
        row = self.db.set_signal_analysis(self.s1, self.sid,
                                          "Analog voice.", "listen", "")
        self.assertEqual(row["analysis_summary"], "Analog voice.")
        self.assertIsNotNone(row["analysis_utc"])
        # session-scoped: a wrong session writes nothing
        self.assertIsNone(self.db.set_signal_analysis(self.s1, self.sid + 99,
                                                      "x", "y"))

    def test_signals_rows_enriched(self):
        rows = self.c.get(f"/api/signals?sweep_id={self.sweep}").get_json()
        row = next(r for r in rows if r["id"] == self.s1)
        self.assertEqual(row["snr_db"], 30.0)
        self.assertIn(row["next_step"]["kind"],
                      ("listen", "identify", "test", "inventory", "clear",
                       "urgent", "review", "done"))

    def test_segment_detail_returns_trace_and_signals(self):
        d = self.c.get(f"/api/segments/{self.seg}").get_json()
        self.assertEqual(d["low_mhz"], 400.0)
        self.assertEqual(len(d["data"]["maxpeak"]), 40)
        self.assertEqual([s["id"] for s in d["signals"]], [self.s1])
        self.assertIn("next_step", d["signals"][0])
        self.assertNotIn("data_json", d)   # parsed, not double-shipped

    def test_segment_detail_is_session_scoped(self):
        # a segment belonging to ANOTHER session must 404 for the open one
        other = self.db.create_session("2 B St", 0, 0, "test", 25, 5, "op")
        sw = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (other, "interior", "done", time.time()))
        foreign = self.db.execute(
            "INSERT INTO segments (sweep_id,low_mhz,high_mhz,status,data_json) "
            "VALUES (?,?,?,?,?)", (sw, 500.0, 501.0, "done", json.dumps(SEG_DATA)))
        r = self.c.get(f"/api/segments/{foreign}")
        self.assertEqual(r.status_code, 404)
        # the open session's own segment stays reachable
        self.assertEqual(self.c.get(f"/api/segments/{self.seg}").status_code, 200)

    def test_signal_image_full_and_thumb(self):
        r = self.c.get(f"/api/signals/{self.s1}/image.svg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/svg+xml")
        body = r.get_data(as_text=True)
        self.assertIn("MAX HOLD", body)            # full render has the legend
        rt = self.c.get(f"/api/signals/{self.s1}/image.svg?size=thumb")
        self.assertEqual(rt.status_code, 200)
        self.assertNotIn("MAX HOLD", rt.get_data(as_text=True))  # chrome-free
        self.assertIn("path", rt.get_data(as_text=True))

    def test_signal_image_without_segment_404s(self):
        r = self.c.get(f"/api/signals/{self.s2}/image.svg")
        self.assertEqual(r.status_code, 404)

    def test_signal_image_unknown_id_404s(self):
        r = self.c.get("/api/signals/99999/image.svg")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
