"""LAN device checker — enumerate every device on the client's own network, so
the operator can rule out client-owned gear and flag anything unrecognized that
has joined the network. Correlates with the RF/BLE inventory: a device that is
BOTH a loud unowned 2.4 GHz emitter AND an unrecognized host on the LAN is a far
stronger lead than either alone.

DISCOVERY IS READ-ONLY AND LOCAL-SUBNET-ONLY.
=============================================
We do exactly three passive things, and only against the subnet THIS machine is
already on (the network the operator is authorized to sweep):
  * ARP cache read      — `arp -a` (who has this laptop already talked to)
  * ICMP echo (ping)    — one small ping per host to populate the ARP cache
  * reverse DNS         — best-effort hostname per IP
There is deliberately NO ARP spoofing, NO deauth, NO port scanning, NO traffic
interception. This module cannot, by design, attack or intercept a network — it
lists what is there. "Native" is defined as the operator's OWNED allowlist (the
existing inventory.owned flag), NOT a raw IP, because DHCP reassigns IPs. A
device is flagged when it is not owned; an owned device is never flagged.

BLOCKING lives in the server/DB, not here, and is ALWAYS operator-confirmed
per device (never automatic) and fully audited. This module only observes.

Pure-ish: the parse/classify/vendor helpers are pure and unit-tested; the
network calls (arp/ping/dns) are isolated in NetworkScanner so tests inject
canned output.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from . import ble

# never treat these as real hosts
_BROADCAST_MACS = {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}
_MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

# ping sweeping more than this many hosts is a big office network — skip the
# active sweep and read whatever the ARP cache already holds (still read-only,
# just no self-generated probes). Keeps us off large/unknown segments.
MAX_SWEEP_HOSTS = 1024


def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def is_real_host_mac(mac: str) -> bool:
    """False for broadcast, all-zero, IPv4 multicast (01:00:5e), IPv6
    multicast (33:33) — none of which are individual devices."""
    m = normalize_mac(mac)
    if m in _BROADCAST_MACS:
        return False
    if m.startswith("01:00:5e") or m.startswith("33:33"):
        return False
    return bool(re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m))


def is_real_host_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_multicast or a.is_unspecified or a.is_loopback:
        return False
    # directed broadcast (.255) shows up in the ARP cache as ff:ff:ff… and is
    # filtered by the MAC check, but guard the address too
    return not str(a).endswith(".255")


def is_locally_administered(mac: str) -> bool:
    """True when the 2nd-least-significant bit of the first octet is set — a
    randomized / locally-assigned MAC (modern phones rotate these for privacy),
    so its leading bytes are NOT a real vendor OUI."""
    try:
        first = int(normalize_mac(mac).split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)


def vendor_for(mac: str) -> str:
    """Manufacturer from the OUI, reusing the curated BLE table. Empty for a
    randomized MAC (no real OUI) or an unknown prefix — never guessed."""
    if is_locally_administered(mac):
        return ""
    oui = normalize_mac(mac).replace(":", "").upper()[:6]
    return ble.OUI_VENDORS.get(oui, "")


def parse_arp_table(text: str) -> list[dict]:
    """Parse `arp -a` output (Windows dash-MACs or Unix colon-MACs) into
    [{ip, mac}], keeping only real individual hosts, deduped by MAC (last IP
    wins — the current lease)."""
    by_mac: dict[str, dict] = {}
    for line in text.splitlines():
        ipm = _IP_RE.search(line)
        macm = _MAC_RE.search(line)
        if not ipm or not macm:
            continue
        ip, mac = ipm.group(1), normalize_mac(macm.group(0))
        if not is_real_host_mac(mac) or not is_real_host_ip(ip):
            continue
        by_mac[mac] = {"ip": ip, "mac": mac}
    return list(by_mac.values())


def classify_device(dev: dict, owned_macs: set, network=None) -> dict:
    """Annotate a device with known / in_subnet / randomized flags. `owned_macs`
    is the operator's allowlist (lowercased). Never mutates ownership — purely
    derived so the UI can flag, and the operator decides."""
    mac = normalize_mac(dev.get("mac") or dev.get("identifier") or "")
    out = dict(dev)
    out["mac"] = mac
    out["known"] = mac in owned_macs
    out["randomized_mac"] = is_locally_administered(mac)
    in_subnet = None
    if network is not None and dev.get("ip"):
        try:
            in_subnet = ipaddress.ip_address(dev["ip"]) in network
        except ValueError:
            in_subnet = None
    out["in_subnet"] = in_subnet
    # off-subnet IP is the concrete reading of a "non-native" address
    out["off_subnet"] = (in_subnet is False)
    return out


class NetworkScanner:
    """Isolates the actual network I/O so the parsing/classification above stays
    pure and testable. Only ever touches the subnet this machine is on."""

    def __init__(self, ping_timeout_ms: int = 350, workers: int = 64):
        self.ping_timeout_ms = ping_timeout_ms
        self.workers = workers

    # -- overridable seams for tests --
    def _run(self, cmd: list, timeout: float) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def local_ipv4(self) -> str | None:
        """This machine's primary LAN IPv4 (no packets sent — a connected UDP
        socket just picks the outbound interface)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))   # TEST-NET-1, never routed anywhere
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()

    def local_network(self):
        """The /24 this machine sits on — the ONLY range we ever sweep. A /24
        (254 hosts) matches essentially every home/small-office LAN and bounds
        the sweep; we never expand to an operator-supplied range."""
        ip = self.local_ipv4()
        if not ip:
            return None
        try:
            return ipaddress.ip_network(ip + "/24", strict=False)
        except ValueError:
            return None

    def default_gateway(self) -> str | None:
        """The LAN's default gateway (the router). Parsed from ipconfig; falls
        back to the .1 of the local /24. This is the box the operator logs into
        to actually block a device — receive-only, we only READ its address."""
        out = self._run(["ipconfig"], timeout=10)
        for line in out.splitlines():
            if "Default Gateway" in line:
                m = _IP_RE.search(line)
                if m and m.group(1) != "0.0.0.0":
                    return m.group(1)
        net = self.local_network()
        if net is not None:
            try:
                return str(next(net.hosts()))   # .1
            except StopIteration:
                return None
        return None

    def _ping(self, ip: str):
        # one echo request; -n/-w are Windows, harmless to fail elsewhere
        self._run(["ping", "-n", "1", "-w", str(self.ping_timeout_ms), ip],
                  timeout=self.ping_timeout_ms / 1000.0 + 1.5)

    def ping_sweep(self, network):
        hosts = list(network.hosts())
        if len(hosts) > MAX_SWEEP_HOSTS:
            return False   # too large — arp-only, no self-probes
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            pool.map(lambda h: self._ping(str(h)), hosts)
        return True

    def arp_table(self) -> list[dict]:
        return parse_arp_table(self._run(["arp", "-a"], timeout=15))

    def hostname_for(self, ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            return ""

    def scan(self, ping: bool = True) -> tuple[list[dict], dict]:
        """Discover LAN devices. Returns (devices, meta). Each device:
        {kind:'lan', identifier:mac, ip, name(hostname), vendor,
         rssi_dbm:None, channel:'', freq_mhz:None, extra}. Read-only."""
        network = self.local_network()
        swept = False
        if ping and network is not None:
            swept = self.ping_sweep(network)
        rows = self.arp_table()
        # reverse-DNS in parallel (best-effort, bounded)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            names = list(pool.map(lambda r: self.hostname_for(r["ip"]), rows))
        gateway = self.default_gateway()
        devices = []
        for r, host in zip(rows, names):
            mac = r["mac"]
            vendor = vendor_for(mac)
            bits = [b for b in (vendor, host) if b]
            if is_locally_administered(mac) and not host:
                bits.append("randomized MAC (privacy)")
            devices.append({
                "kind": "lan", "identifier": mac, "ip": r["ip"],
                "name": host, "vendor": vendor,
                "rssi_dbm": None, "channel": "", "freq_mhz": None,
                "is_gateway": (r["ip"] == gateway),
                "extra": " · ".join(bits)})
        # the gateway (router) brand, read from its own MAC OUI — this is the box
        # you log into to actually block a device, so naming its brand lets us
        # give brand-specific steps
        gw_mac = next((d["identifier"] for d in devices if d.get("is_gateway")), "")
        meta = {
            "network": str(network) if network else None,
            "swept": swept, "arp_only": (network is None or not swept),
            "count": len(devices), "scanned_utc": time.time(),
            "gateway": gateway,
            "router_brand": (vendor_for(gw_mac) if gw_mac else "") or "",
            "router_admin_url": ("http://" + gateway) if gateway else ""}
        return devices, meta


# ---------------------------------------------------------------------------
# Guided router block — the LEGAL way to force a device off the network. We
# never transmit and never touch the router without the operator; we hand them
# the exact target + steps for the router they actually have.
# ---------------------------------------------------------------------------

# Brand-specific "where the block control lives" hints. Generic fallback covers
# any router. Purely informational — the operator does the click in their router.
_ROUTER_STEPS = {
    "netgear": "Advanced → Security → Access Control (or Block Sites/Devices) → "
               "block the device by MAC, then Apply.",
    "tp-link": "Advanced → Security → Access Control (or Parental Controls) → "
               "add the MAC to the Black List / block list, then Save.",
    "asus": "AiProtection / Network Map → Clients → click the device → Block "
            "Internet Access; or WAN/Firewall → MAC filter.",
    "linksys": "Security → Apps and Gaming / MAC Filtering → add the MAC to the "
               "deny list, then Save Settings.",
    "google": "Google Home / Wifi app → device → pause or remove from network.",
    "eero": "eero app → the device → Block or move to a blocked profile.",
    "ubiquiti": "UniFi/Console → Client Devices → the client → Block.",
    "d-link": "Advanced → Network Filter / MAC Address Filter → deny the MAC, Save.",
}


def router_block_guide(gateway: str, router_brand: str, mac: str,
                       device_label: str = "") -> dict:
    """What the operator needs to block one device at the router: the admin URL,
    the MAC to block, and brand-specific (or generic) steps. Legal, receive-only
    — this only tells the operator what to do in a router they control."""
    brand = (router_brand or "").lower()
    steps = next((v for k, v in _ROUTER_STEPS.items() if k in brand), None)
    if not steps:
        steps = ("Open the router's admin page, find MAC Filtering / Access "
                 "Control / Client Blocking, add this MAC to the block/deny "
                 "list, and save. (Wi-Fi devices: also 'kick'/'deauth' the "
                 "client if the router offers it, so it reconnects and is "
                 "refused.)")
    return {
        "admin_url": ("http://" + gateway) if gateway else "",
        "gateway": gateway or "",
        "router_brand": router_brand or "unknown",
        "mac": mac,
        "device_label": device_label,
        "steps": steps,
        "note": ("This blocks the device at the router — the lawful way. The app "
                 "never transmits or jams. If the device is not on this router, "
                 "or you want it gone physically, use LOCATE to DF it."),
    }
