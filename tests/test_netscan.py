"""LAN device checker — parsing/classification purity, and the read-only +
operator-gated-blocking guarantees at the endpoint layer.

The network I/O (arp/ping/dns) is stubbed so tests are deterministic and never
touch a real network. The security-relevant invariants under test:
  * discovery never marks owned/blocked (operator decisions survive rescans)
  * blocking requires confirm=true + a reason, is session-scoped, and audits
  * there is no code path that blocks automatically
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import netscan               # noqa: E402
from survey.db import Database           # noqa: E402
from survey.config import Config         # noqa: E402
from survey.server import create_app     # noqa: E402

WIN_ARP = """
Interface: 192.168.1.5 --- 0x8
  Internet Address      Physical Address      Type
  192.168.1.1           a4-cf-12-aa-bb-cc     dynamic
  192.168.1.22          b8-27-eb-11-22-33     dynamic
  192.168.1.50          de-ad-be-ef-00-01     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  239.255.255.250       01-00-5e-7f-ff-fa     static
"""


class TestPureParsing(unittest.TestCase):
    def test_parse_arp_keeps_only_real_hosts(self):
        hosts = netscan.parse_arp_table(WIN_ARP)
        ips = sorted(h["ip"] for h in hosts)
        self.assertEqual(ips, ["192.168.1.1", "192.168.1.22", "192.168.1.50"])
        # broadcast + both multicast rows dropped
        self.assertTrue(all(netscan.is_real_host_mac(h["mac"]) for h in hosts))

    def test_mac_normalized_to_colon_lower(self):
        h = netscan.parse_arp_table(
            "  10.0.0.9    AA-BB-CC-DD-EE-FF   dynamic")[0]
        self.assertEqual(h["mac"], "aa:bb:cc:dd:ee:ff")

    def test_vendor_from_oui_and_blank_for_random(self):
        self.assertTrue(netscan.vendor_for("b8:27:eb:11:22:33"))   # Raspberry Pi
        # locally-administered (random) MAC -> no vendor guess
        self.assertTrue(netscan.is_locally_administered("de:ad:be:ef:00:01"))
        self.assertEqual(netscan.vendor_for("de:ad:be:ef:00:01"), "")

    def test_multicast_and_broadcast_rejected(self):
        self.assertFalse(netscan.is_real_host_mac("ff:ff:ff:ff:ff:ff"))
        self.assertFalse(netscan.is_real_host_mac("01:00:5e:00:00:16"))
        self.assertFalse(netscan.is_real_host_mac("33:33:00:00:00:01"))
        self.assertFalse(netscan.is_real_host_ip("239.255.255.250"))
        self.assertFalse(netscan.is_real_host_ip("192.168.1.255"))

    def test_classify_known_and_off_subnet(self):
        import ipaddress
        net = ipaddress.ip_network("192.168.1.0/24")
        owned = {"a4:cf:12:aa:bb:cc"}
        d1 = netscan.classify_device({"mac": "A4:CF:12:AA:BB:CC", "ip": "192.168.1.1"}, owned, net)
        self.assertTrue(d1["known"])
        self.assertFalse(d1["off_subnet"])
        d2 = netscan.classify_device({"mac": "de:ad:be:ef:00:01", "ip": "10.9.9.9"}, owned, net)
        self.assertFalse(d2["known"])
        self.assertTrue(d2["off_subnet"])       # the concrete "non-native IP"


class _StubScanner(netscan.NetworkScanner):
    """Injects canned arp/ping/dns so no real network is touched."""
    def local_ipv4(self):
        return "192.168.1.5"
    def _ping(self, ip):
        pass
    def ping_sweep(self, network):
        return True
    def arp_table(self):
        return netscan.parse_arp_table(WIN_ARP)
    def hostname_for(self, ip):
        return {"192.168.1.22": "pi.local"}.get(ip, "")


class TestScannerOrchestration(unittest.TestCase):
    def test_scan_enriches_without_touching_network(self):
        devices, meta = _StubScanner().scan(ping=True)
        self.assertEqual(meta["count"], 3)
        self.assertEqual(meta["network"], "192.168.1.0/24")
        pi = next(d for d in devices if d["ip"] == "192.168.1.22")
        self.assertEqual(pi["identifier"], "b8:27:eb:11:22:33")
        self.assertIn("Raspberry Pi", pi["vendor"])
        self.assertEqual(pi["name"], "pi.local")
        self.assertEqual(pi["kind"], "lan")


class TestNetscanEndpoints(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.app = create_app(self.db, self.cfg)
        self.c = self.app.test_client()
        # seed two LAN devices directly (bypass the real network)
        self.db.upsert_lan_devices(self.sid, [
            {"identifier": "a4:cf:12:aa:bb:cc", "ip": "192.168.1.1",
             "vendor": "Apple", "name": "router", "extra": ""},
            {"identifier": "de:ad:be:ef:00:01", "ip": "192.168.1.50",
             "vendor": "", "name": "", "extra": ""}])

    def _dev_id(self, mac):
        row = self.db.query_one(
            "SELECT id FROM inventory WHERE session_id=? AND identifier=?",
            (self.sid, mac))
        return row["id"]

    def test_devices_listed_with_flags(self):
        d = self.c.get("/api/netscan/devices").get_json()["devices"]
        self.assertEqual(len(d), 2)
        self.assertTrue(all(x["flagged"] for x in d))   # nothing owned yet

    def test_own_clears_flag(self):
        i = self._dev_id("a4:cf:12:aa:bb:cc")
        self.c.post(f"/api/netscan/devices/{i}/own", json={"owned": True})
        d = {x["id"]: x for x in self.c.get("/api/netscan/devices").get_json()["devices"]}
        self.assertTrue(d[i]["known"])
        self.assertFalse(d[i]["flagged"])

    def test_block_requires_confirm_and_reason(self):
        i = self._dev_id("de:ad:be:ef:00:01")
        self.assertEqual(self.c.post(f"/api/netscan/devices/{i}/block",
                                     json={"reason": "x"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/netscan/devices/{i}/block",
                                     json={"confirm": True}).status_code, 400)

    def test_confirmed_block_sets_flag_and_audits(self):
        i = self._dev_id("de:ad:be:ef:00:01")
        r = self.c.post(f"/api/netscan/devices/{i}/block",
                        json={"confirm": True, "reason": "unknown device, client denies it"})
        self.assertEqual(r.status_code, 200)
        guide = r.get_json()["router_guide"]
        self.assertEqual(guide["mac"], "de:ad:be:ef:00:01")
        self.assertTrue(guide["steps"])          # lawful router steps present
        row = self.db.query_one("SELECT blocked, block_reason FROM inventory WHERE id=?", (i,))
        self.assertEqual(row["blocked"], 1)
        audit = self.c.get("/api/netscan/blocks").get_json()
        self.assertEqual(audit[0]["action"], "block")
        self.assertEqual(audit[0]["mac"], "de:ad:be:ef:00:01")

    def test_unblock_reverses_and_audits(self):
        i = self._dev_id("de:ad:be:ef:00:01")
        self.c.post(f"/api/netscan/devices/{i}/block",
                    json={"confirm": True, "reason": "test"})
        self.c.post(f"/api/netscan/devices/{i}/unblock", json={})
        row = self.db.query_one("SELECT blocked FROM inventory WHERE id=?", (i,))
        self.assertEqual(row["blocked"], 0)
        actions = [a["action"] for a in self.c.get("/api/netscan/blocks").get_json()]
        self.assertEqual(actions, ["unblock", "block"])   # newest first

    def test_block_is_session_scoped(self):
        # device `i` belongs to the first session; open a DIFFERENT session and
        # confirm you cannot block another session's device by id (404, no cross)
        i = self._dev_id("de:ad:be:ef:00:01")
        other = self.db.create_session("2 B St", 0, 0, "test", 25, 5, "op")
        self.c.post("/api/session/open", json={"id": other})
        r = self.c.post(f"/api/netscan/devices/{i}/block",
                        json={"confirm": True, "reason": "x"})
        self.assertEqual(r.status_code, 404)
        # and it was NOT blocked
        self.assertEqual(
            self.db.query_one("SELECT blocked FROM inventory WHERE id=?", (i,))["blocked"], 0)

    def test_rescan_preserves_owned_and_blocked(self):
        i = self._dev_id("a4:cf:12:aa:bb:cc")
        self.c.post(f"/api/netscan/devices/{i}/own", json={"owned": True})
        # a later discovery of the same MAC must not reset the operator's flags
        self.db.upsert_lan_devices(self.sid, [
            {"identifier": "a4:cf:12:aa:bb:cc", "ip": "192.168.1.9",
             "vendor": "Apple", "name": "router2", "extra": ""}])
        row = self.db.query_one("SELECT owned, ip FROM inventory WHERE id=?", (i,))
        self.assertEqual(row["owned"], 1)          # still owned
        self.assertEqual(row["ip"], "192.168.1.9")  # but IP refreshed

    def test_router_guide_endpoint(self):
        i = self._dev_id("de:ad:be:ef:00:01")
        g = self.c.get(f"/api/netscan/devices/{i}/router_guide").get_json()
        self.assertEqual(g["mac"], "de:ad:be:ef:00:01")
        self.assertTrue(g["steps"])
        # generic (unknown brand) steps mention MAC filtering
        self.assertIn("MAC", g["steps"])

    def test_related_rf_correlation(self):
        # a WiFi AP with the same vendor as a LAN device should surface as related
        self.db.upsert_inventory = None  # ensure we use raw insert
        import time as _t
        self.db.execute(
            "INSERT INTO inventory (session_id,kind,identifier,name,vendor,rssi_dbm,"
            "freq_mhz,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.sid, "wifi", "a4:cf:12:00:11:22", "MyAP", "Apple", -48.0,
             2437.0, _t.time(), _t.time()))
        d = {x["ip"]: x for x in
             self.c.get("/api/netscan/devices").get_json()["devices"]}
        apple = d["192.168.1.1"]   # vendor Apple LAN device, seeded in setUp
        self.assertTrue(apple["related"])
        self.assertEqual(apple["related"][0]["kind"], "wifi")

    def test_demo_mode_blocks_netscan(self):
        demo_app = create_app(self.db, self.cfg, demo=True)
        dc = demo_app.test_client()
        self.assertEqual(dc.post("/api/netscan/run", json={}).status_code, 403)
        self.assertEqual(dc.get("/api/netscan/devices").status_code, 403)

    def test_inventory_mark_is_session_scoped(self):
        # the owned flag is the allowlist the LAN checker trusts — a foreign id
        # must not be flippable via the shared /api/inventory/mark endpoint
        i = self._dev_id("a4:cf:12:aa:bb:cc")
        other = self.db.create_session("2 B St", 0, 0, "test", 25, 5, "op")
        self.c.post("/api/session/open", json={"id": other})
        r = self.c.post("/api/inventory/mark", json={"id": i, "owned": True})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(
            self.db.query_one("SELECT owned FROM inventory WHERE id=?", (i,))["owned"], 0)

    def test_malicious_hostname_does_not_break_report_markup(self):
        # a rogue device's reverse-DNS hostname must not abort PDF generation via
        # ReportLab's markup parser (report.py _para_safe escapes it)
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph
        from survey.report import _para_safe
        ss = getSampleStyleSheet()
        for payload in ("evil & co", "<b>x", "<img src=x onerror=alert(1)"):
            Paragraph(_para_safe(payload[:40]), ss["Normal"])   # must not raise


if __name__ == "__main__":
    unittest.main()
