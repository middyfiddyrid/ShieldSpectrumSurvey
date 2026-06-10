"""Static US national frequency allocation table.

This is the zero-internet baseline: every band the app knows about without
querying anything. Each entry carries the metadata the correlation engine and
the sweep planner need:

  band_tag        -- key into config sweep.band_dwell_weights (risk-weighted dwell)
  channel_bw_khz  -- expected channel bandwidth, used for match tolerance
  caution         -- True for allocations where surveillance devices commonly hide;
                     a PLAUSIBLE match here gets a caution marker, never a clean pass
  conditional     -- e.g. 'airport' entries only apply within 10 mi of an airport
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Allocation:
    low_mhz: float
    high_mhz: float
    service: str
    label: str
    category: str                  # broadcast | land_mobile | ism | cellular | aviation | gps | ham | other
    band_tag: str = "default"
    channel_bw_khz: float = 25.0
    caution: bool = False
    uplink: bool = False           # cellular uplink — where a GSM/LTE bug transmits
    conditional: Optional[str] = None
    notes: str = ""

    def contains(self, freq_mhz: float) -> bool:
        return self.low_mhz <= freq_mhz <= self.high_mhz


def _tv_channels():
    """Post-repack US TV channel -> RF range. Ch 2-36 (37 reserved, 38+ repacked away)."""
    out = []
    for ch in range(2, 37):
        if ch == 37:
            continue
        if 2 <= ch <= 4:
            lo = 54 + 6 * (ch - 2)
        elif 5 <= ch <= 6:
            lo = 76 + 6 * (ch - 5)
        elif 7 <= ch <= 13:
            lo = 174 + 6 * (ch - 7)
        else:  # 14-36
            lo = 470 + 6 * (ch - 14)
        out.append(Allocation(lo, lo + 6, "TV", f"TV broadcast ch {ch}", "broadcast",
                              band_tag="broadcast", channel_bw_khz=6000.0))
    return out


STATIC_ALLOCATIONS: list[Allocation] = [
    # --- Broadcast ---
    Allocation(0.530, 1.705, "AM", "AM broadcast band", "broadcast", "broadcast", 10.0),
    Allocation(88.0, 108.0, "FM", "FM broadcast band", "broadcast", "broadcast", 200.0),
    *_tv_channels(),

    # --- Public / utility / licensed voice ---
    Allocation(26.965, 27.405, "CB", "Citizens Band", "land_mobile", "default", 10.0),
    Allocation(29.7, 50.0, "LMR", "VHF low-band land mobile", "land_mobile", "default", 20.0),
    Allocation(49.82, 49.90, "Part15", "49 MHz baby monitors / legacy cordless", "ism", "default", 50.0,
               caution=True, notes="Cheap legacy audio devices and some analog bugs"),
    Allocation(108.0, 118.0, "AERO-NAV", "Aviation VOR/ILS nav", "aviation", "broadcast", 50.0,
               conditional="airport"),
    Allocation(118.0, 137.0, "AERO-COM", "Aviation voice (AM)", "aviation", "broadcast", 25.0,
               conditional="airport"),
    Allocation(137.0, 138.0, "SAT", "Weather satellite downlink (NOAA APT)", "other", "default", 40.0),
    Allocation(144.0, 148.0, "HAM", "Amateur 2 m", "ham", "default", 16.0),
    Allocation(150.8, 162.0, "LMR", "VHF high-band land mobile / marine", "land_mobile", "default", 12.5),
    Allocation(151.82, 151.94, "MURS", "MURS 151 MHz channels", "land_mobile", "default", 11.25),
    Allocation(154.57, 154.60, "MURS", "MURS 154 MHz channels", "land_mobile", "default", 11.25),
    Allocation(156.0, 157.45, "MARINE", "VHF marine", "land_mobile", "default", 25.0),
    Allocation(162.400, 162.550, "NOAA", "NOAA weather radio", "broadcast", "broadcast", 25.0),
    Allocation(162.0, 174.0, "FED", "Federal VHF land mobile", "land_mobile", "default", 12.5,
               notes="Federal users — not in public license databases"),
    Allocation(174.0, 216.0, "TV", "TV VHF-high (also wireless mics)", "broadcast", "broadcast", 6000.0),
    Allocation(222.0, 225.0, "HAM", "Amateur 1.25 m", "ham", "default", 16.0),
    Allocation(225.0, 400.0, "MIL", "Military air band (UHF AM)", "other", "default", 25.0,
               notes="Military — not in public license databases"),
    Allocation(406.0, 420.0, "FED", "Federal UHF land mobile", "land_mobile", "default", 12.5,
               notes="Federal users — not in public license databases"),
    Allocation(420.0, 450.0, "HAM", "Amateur 70 cm", "ham", "default", 25.0),
    Allocation(450.0, 470.0, "LMR", "UHF land mobile / GMRS region", "land_mobile", "default", 12.5),
    Allocation(462.550, 462.725, "FRS/GMRS", "FRS/GMRS main channels", "land_mobile", "default", 12.5),
    Allocation(467.550, 467.725, "FRS/GMRS", "FRS interstitial / GMRS repeater inputs", "land_mobile",
               "default", 12.5),
    Allocation(470.0, 608.0, "TV", "TV UHF (also wireless mics in gaps)", "broadcast", "broadcast", 6000.0),
    Allocation(929.0, 932.0, "PAGING", "Paging transmitters", "land_mobile", "default", 25.0),

    # --- Unlicensed / ISM — where bugs live. All caution=True. ---
    Allocation(300.0, 348.0, "Part15", "Remote controls / garage doors / 315 keyfobs", "ism",
               "default", 100.0, caution=True),
    Allocation(433.05, 434.79, "ISM", "433 MHz ISM (keyfobs, sensors, cheap audio bugs)", "ism",
               "ism_433", 100.0, caution=True,
               notes="Top hiding spot for low-cost analog/digital bugs"),
    Allocation(863.0, 870.0, "ISM-EU", "EU 868 MHz ISM (imported devices)", "ism", "ism_433", 100.0,
               caution=True, notes="Not a US allocation — any hit here is interesting"),
    Allocation(902.0, 928.0, "ISM", "902-928 ISM (LoRa, Z-Wave 908, meters, ham 33 cm)", "ism",
               "ism_915", 200.0, caution=True),
    Allocation(1240.0, 1300.0, "HAM", "Amateur 23 cm (also video senders)", "ham", "default", 6000.0,
               caution=True, notes="Analog video transmitters exist here"),
    Allocation(1880.0, 1930.0, "DECT", "DECT cordless (US DECT 6.0 = 1920-1930; imports below)",
               "ism", "dect", 1728.0, caution=True,
               notes="1880-1920 hits = imported/non-US DECT device — investigate"),
    Allocation(2400.0, 2483.5, "ISM", "2.4 GHz ISM (WiFi 1-11, Bluetooth, Zigbee, cameras)", "ism",
               "ism_2400", 20000.0, caution=True),
    Allocation(2483.5, 2495.0, "ISM", "2.4 GHz upper edge (BLE ch 39 / Globalstar)", "ism",
               "ism_2400", 2000.0, caution=True),
    Allocation(5150.0, 5350.0, "U-NII", "5 GHz WiFi U-NII-1/2A", "ism", "ism_5800", 20000.0, caution=True),
    Allocation(5470.0, 5725.0, "U-NII", "5 GHz WiFi U-NII-2C", "ism", "ism_5800", 20000.0, caution=True),
    Allocation(5725.0, 5875.0, "ISM", "5.8 GHz ISM (WiFi U-NII-3, video senders, drones)", "ism",
               "ism_5800", 20000.0, caution=True),
    Allocation(5925.0, 6100.0, "U-NII", "6 GHz WiFi lower edge (U-NII-5, within analyzer range)",
               "ism", "ism_5800", 20000.0, caution=True),

    # --- GPS: legitimate GPS is below the thermal noise floor. A *detectable*
    #     carrier here means a repeater, jammer, or spoofer. ---
    Allocation(1565.42, 1585.42, "GPS", "GPS L1 (1575.42)", "gps", "gps", 2000.0, caution=True,
               notes="GPS should NOT be detectable — energy here suggests repeater/jammer"),
    Allocation(1217.6, 1237.6, "GPS", "GPS L2 (1227.60)", "gps", "gps", 2000.0, caution=True,
               notes="GPS should NOT be detectable — energy here suggests repeater/jammer"),

    # --- Ham (HF bands within analyzer range start at 50 kHz; include for completeness) ---
    Allocation(1.8, 2.0, "HAM", "Amateur 160 m", "ham", "default", 3.0),
    Allocation(3.5, 4.0, "HAM", "Amateur 80 m", "ham", "default", 3.0),
    Allocation(7.0, 7.3, "HAM", "Amateur 40 m", "ham", "default", 3.0),
    Allocation(14.0, 14.35, "HAM", "Amateur 20 m", "ham", "default", 3.0),
    Allocation(18.068, 18.168, "HAM", "Amateur 17 m", "ham", "default", 3.0),
    Allocation(21.0, 21.45, "HAM", "Amateur 15 m", "ham", "default", 3.0),
    Allocation(24.89, 24.99, "HAM", "Amateur 12 m", "ham", "default", 3.0),
    Allocation(28.0, 29.7, "HAM", "Amateur 10 m", "ham", "default", 10.0),
    Allocation(50.0, 54.0, "HAM", "Amateur 6 m", "ham", "default", 16.0),
]


# --- US cellular carrier bands. Uplink blocks are where a cellular bug transmits. ---
def _cell(lo, hi, name, uplink):
    return Allocation(lo, hi, "CELL", name, "cellular",
                      band_tag="cellular_uplink" if uplink else "default",
                      channel_bw_khz=5000.0, caution=uplink, uplink=uplink,
                      notes="Cellular uplink — a GSM/LTE bug transmits here" if uplink else "")


CELLULAR_BANDS: list[Allocation] = [
    _cell(617.0, 652.0, "600 MHz downlink (B71/n71)", False),
    _cell(663.0, 698.0, "600 MHz UPLINK (B71/n71)", True),
    _cell(698.0, 716.0, "700 MHz UPLINK (B12/B17)", True),
    _cell(717.0, 728.0, "700 MHz downlink (B29 SDL)", False),
    _cell(728.0, 746.0, "700 MHz downlink (B12/B17)", False),
    _cell(746.0, 757.0, "700 MHz downlink (B13)", False),
    _cell(758.0, 768.0, "700 MHz downlink (B14 FirstNet)", False),
    _cell(777.0, 787.0, "700 MHz UPLINK (B13)", True),
    _cell(788.0, 798.0, "700 MHz UPLINK (B14 FirstNet)", True),
    _cell(824.0, 849.0, "850 MHz UPLINK (B5/B26)", True),
    _cell(869.0, 894.0, "850 MHz downlink (B5/B26)", False),
    _cell(1710.0, 1780.0, "AWS UPLINK (B4/B66)", True),
    _cell(1850.0, 1915.0, "PCS UPLINK (B2/B25)", True),
    _cell(1930.0, 1995.0, "PCS downlink (B2/B25)", False),
    _cell(2110.0, 2200.0, "AWS downlink (B4/B66)", False),
    _cell(2305.0, 2315.0, "WCS UPLINK (B30)", True),
    _cell(2350.0, 2360.0, "WCS downlink (B30)", False),
    _cell(2496.0, 2690.0, "2.5 GHz TDD (B41/n41) — UL and DL share", True),
    _cell(3550.0, 3700.0, "CBRS TDD (n48) — UL and DL share", True),
    _cell(3700.0, 3980.0, "C-band TDD (n77)", False),
]

ALL_ALLOCATIONS: list[Allocation] = STATIC_ALLOCATIONS + CELLULAR_BANDS


# MCC/MNC -> carrier name (majors + common MVNO cores; OpenCellID reports the host network)
MCC_MNC_CARRIERS = {
    ("310", "410"): "AT&T", ("310", "030"): "AT&T", ("310", "070"): "AT&T",
    ("310", "150"): "AT&T", ("310", "170"): "AT&T", ("310", "280"): "AT&T",
    ("310", "380"): "AT&T", ("310", "560"): "AT&T", ("310", "680"): "AT&T",
    ("310", "260"): "T-Mobile", ("310", "160"): "T-Mobile", ("310", "200"): "T-Mobile",
    ("310", "210"): "T-Mobile", ("310", "220"): "T-Mobile", ("310", "230"): "T-Mobile",
    ("310", "240"): "T-Mobile", ("310", "250"): "T-Mobile", ("310", "270"): "T-Mobile",
    ("310", "310"): "T-Mobile", ("310", "120"): "T-Mobile (ex-Sprint)",
    ("312", "530"): "T-Mobile (ex-Sprint)",
    ("311", "480"): "Verizon", ("311", "270"): "Verizon", ("310", "012"): "Verizon",
    ("310", "010"): "Verizon", ("310", "013"): "Verizon",
    ("311", "490"): "T-Mobile (ex-Sprint)", ("310", "053"): "Verizon",
    ("313", "100"): "FirstNet (AT&T)",
    ("311", "882"): "US Cellular", ("311", "580"): "US Cellular",
    ("313", "340"): "DISH/Boost", ("313", "460"): "DISH/Boost",
}

# Carrier -> band labels they actively use (drives "owned spectrum" rows in the baseline)
CARRIER_BANDS = {
    "AT&T": ["700 MHz downlink (B12/B17)", "700 MHz UPLINK (B12/B17)",
             "850 MHz UPLINK (B5/B26)", "850 MHz downlink (B5/B26)",
             "PCS UPLINK (B2/B25)", "PCS downlink (B2/B25)",
             "AWS UPLINK (B4/B66)", "AWS downlink (B4/B66)",
             "WCS UPLINK (B30)", "WCS downlink (B30)",
             "C-band TDD (n77)"],
    "FirstNet (AT&T)": ["700 MHz downlink (B14 FirstNet)", "700 MHz UPLINK (B14 FirstNet)"],
    "Verizon": ["700 MHz downlink (B13)", "700 MHz UPLINK (B13)",
                "850 MHz UPLINK (B5/B26)", "850 MHz downlink (B5/B26)",
                "PCS UPLINK (B2/B25)", "PCS downlink (B2/B25)",
                "AWS UPLINK (B4/B66)", "AWS downlink (B4/B66)",
                "CBRS TDD (n48) — UL and DL share", "C-band TDD (n77)"],
    "T-Mobile": ["600 MHz downlink (B71/n71)", "600 MHz UPLINK (B71/n71)",
                 "700 MHz downlink (B12/B17)", "700 MHz UPLINK (B12/B17)",
                 "PCS UPLINK (B2/B25)", "PCS downlink (B2/B25)",
                 "AWS UPLINK (B4/B66)", "AWS downlink (B4/B66)",
                 "2.5 GHz TDD (B41/n41) — UL and DL share"],
    "T-Mobile (ex-Sprint)": ["2.5 GHz TDD (B41/n41) — UL and DL share",
                             "PCS UPLINK (B2/B25)", "PCS downlink (B2/B25)"],
    "US Cellular": ["600 MHz downlink (B71/n71)", "600 MHz UPLINK (B71/n71)",
                    "700 MHz downlink (B12/B17)", "700 MHz UPLINK (B12/B17)",
                    "850 MHz UPLINK (B5/B26)", "850 MHz downlink (B5/B26)",
                    "PCS UPLINK (B2/B25)", "PCS downlink (B2/B25)"],
    "DISH/Boost": ["600 MHz downlink (B71/n71)", "600 MHz UPLINK (B71/n71)",
                   "AWS UPLINK (B4/B66)", "AWS downlink (B4/B66)",
                   "CBRS TDD (n48) — UL and DL share"],
}


def carrier_for_cell(mcc, mnc) -> str:
    mnc = str(mnc).zfill(3)
    return MCC_MNC_CARRIERS.get((str(mcc), mnc), f"Unknown carrier MCC{mcc}/MNC{mnc}")


def bands_for_carrier(carrier: str) -> list[Allocation]:
    labels = set(CARRIER_BANDS.get(carrier, []))
    return [a for a in CELLULAR_BANDS if a.label in labels]


def allocations_containing(freq_mhz: float, include_conditional: bool = True) -> list[Allocation]:
    return [a for a in ALL_ALLOCATIONS
            if a.contains(freq_mhz) and (include_conditional or a.conditional is None)]


_TAG_PRIORITY = ("ism_433", "ism_915", "ism_2400", "dect", "cellular_uplink", "ism_5800", "gps")


def band_tag_for(freq_mhz: float) -> str:
    """Highest-priority band tag at a frequency, for dwell weighting."""
    return band_tag_for_range(freq_mhz, freq_mhz)


def band_tag_for_range(low_mhz: float, high_mhz: float) -> str:
    """Highest-priority band tag overlapping a segment — a sweep segment gets
    the risk weight of the riskiest band anywhere inside it, not just at its
    center frequency."""
    tags = {a.band_tag for a in ALL_ALLOCATIONS
            if a.low_mhz < high_mhz and a.high_mhz > low_mhz}
    for priority in _TAG_PRIORITY:
        if priority in tags:
            return priority
    if tags == {"broadcast"}:
        return "broadcast"
    return "default"
