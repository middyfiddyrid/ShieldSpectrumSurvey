"""FCC FM / AM / TV queries (transition.fcc.gov/fcc-bin/{fmq,amq,tvq}).

These legacy CGI endpoints return pipe-delimited text (list=4) and accept a
point + distance search in degrees/minutes/seconds. The column layout is old
and has shifted over the years, so the parser is deliberately heuristic: it
scans tokens for the frequency ("98.5   MHz"), callsign, ERP ("100. kW"),
distance ("12.3 km" / "7.6 mi") and bearing rather than trusting fixed column
indices. Anything unparseable is skipped, not fatal.
"""

import re

import requests

from .base import BaselineSource

USER_AGENT = "ShieldTSCM-SpectrumSurvey/0.1"
TIMEOUT = 30

_CALL_RE = re.compile(r"^(?:[KWN][A-Z0-9]{1,6}(?:-(?:FM|TV|LP|LD|CD|CA|DT))?|NEW)$")
_FREQ_RE = re.compile(r"^([\d.]+)\s*(MHz|kHz)$", re.I)
_ERP_RE = re.compile(r"^([\d.]+)\s*k?W", re.I)
_DIST_RE = re.compile(r"^([\d.]+)\s*(km|mi)\.?$", re.I)
_BRG_RE = re.compile(r"^([\d.]+)\s*deg$", re.I)


def _dms(value: float):
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60, 2)
    return d, m, s


def _point_params(lat: float, lon: float, radius_mi: float) -> dict:
    dlat, mlat, slat = _dms(lat)
    dlon, mlon, slon = _dms(lon)
    return {
        "dist": str(int(radius_mi)),
        "dlat2": dlat, "mlat2": mlat, "slat2": slat, "NS": "N" if lat >= 0 else "S",
        "dlon2": dlon, "mlon2": mlon, "slon2": slon, "EW": "W" if lon <= 0 else "E",
    }


def _parse_pipe_rows(text: str) -> list[dict]:
    """Heuristic parse of one pipe-delimited record per line."""
    out = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        tokens = [t.strip() for t in line.split("|")]
        rec = {"callsign": None, "freq": None, "freq_unit": None, "erp": None,
               "distance_mi": None, "bearing_deg": None, "city_state": [],
               "service": None, "channel": None, "status": None, "tokens": tokens}
        seen_dist_units = {}
        for i, t in enumerate(tokens):
            if not t:
                continue
            m = _FREQ_RE.match(t)
            if m and rec["freq"] is None:
                rec["freq"] = float(m.group(1))
                rec["freq_unit"] = m.group(2).lower()
                continue
            if rec["callsign"] is None and _CALL_RE.match(t) and not t.isdigit():
                rec["callsign"] = t
                continue
            m = _DIST_RE.match(t)
            if m:
                seen_dist_units[m.group(2).lower()] = float(m.group(1))
                continue
            m = _BRG_RE.match(t)
            if m and rec["bearing_deg"] is None:
                rec["bearing_deg"] = float(m.group(1))
                continue
            m = _ERP_RE.match(t)
            if m and rec["erp"] is None and "kW" in t:
                rec["erp"] = t
                continue
            if t in ("LIC", "CP", "APP", "STA", "LIC/STA"):
                rec["status"] = t
                continue
            if re.match(r"^(FM|AM|TV|DT|FX|FB|FL|LD|CD|CA|LPT)\w*$", t) and rec["service"] is None:
                rec["service"] = t
                continue
            if re.match(r"^[A-Z][A-Za-z .'-]{2,}$", t):
                rec["city_state"].append(t)
        if "mi" in seen_dist_units:
            rec["distance_mi"] = seen_dist_units["mi"]
        elif "km" in seen_dist_units:
            rec["distance_mi"] = seen_dist_units["km"] * 0.621371
        # TV records carry a channel number instead of a frequency token, so a
        # missing frequency is not a reason to drop a row — each source filters.
        if rec["callsign"] not in (None, "NEW"):
            out.append(rec)
    return out


def _fetch_pipe(url: str, params: dict) -> list[dict]:
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return _parse_pipe_rows(r.text)


class FccFmSource(BaselineSource):
    name = "FCC FM"
    cache_namespace = "fcc_fm"
    URL = "https://transition.fcc.gov/fcc-bin/fmq"

    def _query(self, lat, lon, radius_mi):
        params = {"call": "", "arn": "", "state": "", "city": "", "freq": "87.9",
                  "fre2": "107.9", "serv": "", "vac": "", "facid": "", "asrn": "",
                  "class": "", "list": "4", "size": "9", **_point_params(lat, lon, radius_mi)}
        rows = []
        for rec in _fetch_pipe(self.URL, params):
            if rec["freq"] is None or (rec["status"] and rec["status"] != "LIC"):
                continue  # no freq parsed, or CP/application not on the air
            f = rec["freq"]
            rows.append({"source": self.name, "freq_low_mhz": f, "freq_high_mhz": f,
                         "service": "FM", "label": f"FM {f:.1f}",
                         "callsign": rec["callsign"],
                         "licensee": ", ".join(rec["city_state"][:2]),
                         "erp": rec["erp"], "distance_mi": rec["distance_mi"],
                         "bearing_deg": rec["bearing_deg"], "confidence": "record",
                         "channel_bw_khz": 200.0, "category": "broadcast",
                         "raw": {"tokens": rec["tokens"][:30]}})
        return rows


class FccAmSource(BaselineSource):
    name = "FCC AM"
    cache_namespace = "fcc_am"
    URL = "https://transition.fcc.gov/fcc-bin/amq"

    def _query(self, lat, lon, radius_mi):
        params = {"call": "", "arn": "", "state": "", "city": "", "freq": "530",
                  "fre2": "1700", "serv": "", "vac": "", "list": "4", "size": "9",
                  **_point_params(lat, lon, radius_mi)}
        rows = []
        for rec in _fetch_pipe(self.URL, params):
            if rec["freq"] is None or (rec["status"] and rec["status"] != "LIC"):
                continue
            f_mhz = rec["freq"] / 1000.0 if (rec["freq_unit"] == "khz" or rec["freq"] > 200) \
                else rec["freq"]
            rows.append({"source": self.name, "freq_low_mhz": f_mhz, "freq_high_mhz": f_mhz,
                         "service": "AM", "label": f"AM {f_mhz * 1000:.0f} kHz",
                         "callsign": rec["callsign"],
                         "licensee": ", ".join(rec["city_state"][:2]),
                         "erp": rec["erp"], "distance_mi": rec["distance_mi"],
                         "bearing_deg": rec["bearing_deg"], "confidence": "record",
                         "channel_bw_khz": 10.0, "category": "broadcast",
                         "raw": {"tokens": rec["tokens"][:30]}})
        return rows


def tv_channel_to_range(ch: int):
    """Post-repack US TV RF channel -> (low_mhz, high_mhz)."""
    if 2 <= ch <= 4:
        lo = 54 + 6 * (ch - 2)
    elif 5 <= ch <= 6:
        lo = 76 + 6 * (ch - 5)
    elif 7 <= ch <= 13:
        lo = 174 + 6 * (ch - 7)
    elif 14 <= ch <= 36:
        lo = 470 + 6 * (ch - 14)
    else:
        return None
    return float(lo), float(lo + 6)


class FccTvSource(BaselineSource):
    name = "FCC TV"
    cache_namespace = "fcc_tv"
    URL = "https://transition.fcc.gov/fcc-bin/tvq"

    def _query(self, lat, lon, radius_mi):
        params = {"call": "", "arn": "", "state": "", "city": "", "chan": "2",
                  "cha2": "36", "serv": "", "vac": "", "list": "4", "size": "9",
                  **_point_params(lat, lon, radius_mi)}
        rows = []
        for rec in _fetch_pipe(self.URL, params):
            if rec["status"] and rec["status"] != "LIC":
                continue
            # tvq reports channel, not frequency: first standalone integer 2..36
            ch = None
            for t in rec["tokens"]:
                if t.strip().isdigit() and 2 <= int(t) <= 36:
                    ch = int(t)
                    break
            if ch is None:
                continue
            rng = tv_channel_to_range(ch)
            if rng is None:
                continue
            rows.append({"source": self.name, "freq_low_mhz": rng[0], "freq_high_mhz": rng[1],
                         "service": "TV", "label": f"TV RF ch {ch}",
                         "callsign": rec["callsign"],
                         "licensee": ", ".join(rec["city_state"][:2]),
                         "erp": rec["erp"], "distance_mi": rec["distance_mi"],
                         "bearing_deg": rec["bearing_deg"], "confidence": "record",
                         "channel_bw_khz": 6000.0, "category": "broadcast",
                         "raw": {"tokens": rec["tokens"][:30]}})
        return rows
