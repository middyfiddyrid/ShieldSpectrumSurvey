"""RadioReference (optional, premium account) — public safety / trunked / local
government frequencies for the county. Minimal hand-rolled SOAP client so we
don't drag in a SOAP dependency for an optional module. Without credentials the
source reports 'not_configured' and the app carries on."""

import re

import requests

from .base import BaselineSource

SOAP_URL = "http://api.radioreference.com/soap2/"
APPKEY = "88339278"  # RadioReference's documented public test appkey; user creds gate access
TIMEOUT = 30


def _envelope(method: str, inner: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:ns1="http://api.radioreference.com/soap2">
  <SOAP-ENV:Body><ns1:{method}>{inner}</ns1:{method}></SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


class RadioReferenceSource(BaselineSource):
    name = "RadioReference"
    cache_namespace = "radioref"

    def __init__(self, cache, username: str, password: str, zipcode: str = ""):
        super().__init__(cache)
        self.username = username
        self.password = password
        self.zipcode = zipcode

    def configured(self):
        if not (self.username and self.password):
            return False, "RadioReference: not configured (optional premium module)"
        if not self.zipcode:
            return False, "RadioReference: no ZIP code available from the session address"
        return True, ""

    def _auth(self) -> str:
        return (f"<authInfo><appKey>{APPKEY}</appKey><username>{self.username}</username>"
                f"<password>{self.password}</password><version>latest</version>"
                f"<style>rpc</style></authInfo>")

    def _call(self, method: str, args_xml: str) -> str:
        body = _envelope(method, args_xml + self._auth())
        r = requests.post(SOAP_URL, data=body.encode(),
                          headers={"Content-Type": "text/xml; charset=utf-8",
                                   "SOAPAction": method},
                          timeout=TIMEOUT)
        r.raise_for_status()
        if "<faultstring>" in r.text:
            fault = re.search(r"<faultstring>(.*?)</faultstring>", r.text, re.S)
            raise RuntimeError(f"RadioReference fault: {fault.group(1) if fault else 'unknown'}")
        return r.text

    def _query(self, lat, lon, radius_mi):
        # ZIP -> county id, then county frequency list
        zip_resp = self._call("getZipcodeInfo", f"<zipcode>{self.zipcode}</zipcode>")
        ctid_m = re.search(r"<ctid[^>]*>(\d+)</ctid>", zip_resp)
        if not ctid_m:
            raise RuntimeError("could not resolve county id from ZIP")
        ctid = ctid_m.group(1)
        freq_resp = self._call("getCountyFreqs", f"<ctid>{ctid}</ctid>")

        rows = []
        # parse <item>...<out>freq</out>...<descr>...</descr>...</item> blocks defensively
        for item in re.findall(r"<item[^>]*>(.*?)</item>", freq_resp, re.S):
            f_m = re.search(r"<out[^>]*>([\d.]+)</out>", item)
            if not f_m:
                continue
            try:
                f_mhz = float(f_m.group(1))
            except ValueError:
                continue
            if not (0.05 <= f_mhz <= 7500):
                continue
            descr = re.search(r"<descr[^>]*>(.*?)</descr>", item, re.S)
            alpha = re.search(r"<alpha[^>]*>(.*?)</alpha>", item, re.S)
            label = (descr.group(1) if descr else "").strip()[:120] or "County frequency"
            rows.append({"source": self.name, "freq_low_mhz": f_mhz, "freq_high_mhz": f_mhz,
                         "service": "LMR", "label": label,
                         "callsign": (alpha.group(1).strip()[:20] if alpha else ""),
                         "licensee": "County/public safety", "erp": None,
                         "distance_mi": None, "bearing_deg": None,
                         "confidence": "record", "channel_bw_khz": 12.5,
                         "category": "land_mobile", "raw": {"ctid": ctid}})
        return rows
