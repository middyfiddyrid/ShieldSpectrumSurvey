#!/usr/bin/env python3
"""Shield Sweep — iPhone / iPad (iOS) integrity check.  STANDALONE, READ-ONLY.

The iOS counterpart to droid_check.py.  Plug the client's iPhone into this laptop
by USB, tap "Trust This Computer" on the phone, and this reads the device's own
configuration to surface the mechanisms real iOS surveillance relies on.  It does
NOT give a "clean/infected" verdict — no tool honestly can, and iOS is sandboxed
so you cannot enumerate app behaviour the way you can on Android.  It lists
INDICATORS for the operator to walk through with the client.

WHY THIS IS THE RIGHT iOS ANGLE
===============================
On a non-jailbroken iPhone you can't dump app permissions or traffic.  But every
practical way to remotely monitor, mirror, or intercept an iPhone leaves a
CONFIGURATION PROFILE, and profiles are readable:
  * MDM ............. remote management: locate, install apps, see a lot.
  * VPN / proxy ..... redirect the phone's traffic through someone else.
  * Root cert ....... lets that someone DECRYPT the phone's HTTPS (MITM).
  * Supervision ..... device set up as "managed" by an organisation.
Plus: enterprise/developer PROVISIONING profiles = apps side-loaded outside the
App Store (a spyware delivery route), and the installed-app list.

READ-ONLY BY DESIGN
===================
This shells out to the pymobiledevice3 CLI, and ONLY to these read verbs:
    usbmux list, lockdown info, profile list, provision list, apps list.
Every command string is built from the _CLI_OK allowlist below; anything that is
not a read verb raises before it can run.  There is NO profile install/remove, NO
app install/uninstall, NO erase, NO backup here.  It cannot alter the phone; the
only thing written anywhere is the normal USB "Trust" pairing record on THIS
laptop.

For KNOWN mercenary spyware (Pegasus/Predator/etc.), the deeper, still-read-only
step is an encrypted local backup analysed with Amnesty International's MVT
against IOC feeds — see the note this prints at the end.

Requires: pymobiledevice3 (pip) and Apple's USB support (the "Apple Devices"
app or iTunes).  Nothing here needs the Shield Sweep app running or restarted.

Usage:   python iphone_check.py            (console report)
         python iphone_check.py --html     (also write an HTML report next to it)
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time

# ---- read-only guard: only these (group, verb) CLI commands may ever run ----
_CLI_OK = {
    ("usbmux", "list"),
    ("lockdown", "info"),
    ("profile", "list"),
    ("provision", "list"),
    ("apps", "list"),
}

UDID = None  # set once a device is picked
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# profile name/text hints that deserve an extra call-out
_PROFILE_HINTS = {
    "mdm": "MDM / remote management",
    "mobile device management": "MDM / remote management",
    "management": "management payload",
    "supervis": "supervision",
    "proxy": "HTTP proxy (can route/inspect web traffic)",
    "vpn": "VPN (can route the phone's traffic)",
    "certificate": "installed certificate (a root cert can decrypt HTTPS)",
    "root ca": "installed root CA (can decrypt HTTPS)",
}


class ReadOnlyViolation(RuntimeError):
    pass


# ---- pretty print (self-contained, mirrors droid_check.py) ------------------
_LINES = []  # (level, text): h/ok/warn/info/raw


def out(text="", level="info"):
    _LINES.append((level, text))
    print(text)


def hdr(text):
    out()
    out("=" * 66, "raw")
    out(text, "h")
    out("=" * 66, "raw")


def flag(text):
    out("  [!] " + text, "warn")


def note(text):
    out("      " + text, "info")


# ---- CLI plumbing -----------------------------------------------------------
def _cli(group, verb, *extra, timeout=60, use_udid=True):
    """Run `python -m pymobiledevice3 <group> <verb> ...`, read-only enforced.
    Returns (rc, parsed_json_or_None, stderr_text)."""
    if (group, verb) not in _CLI_OK:
        raise ReadOnlyViolation(f"blocked non-read command: {group} {verb}")
    cmd = [sys.executable, "-m", "pymobiledevice3", group, verb, *extra]
    if use_udid and UDID:
        cmd += ["--udid", UDID]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, None, "timeout"
    except OSError as e:
        return 127, None, str(e)
    raw = _ANSI.sub("", p.stdout or "").strip()
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # some builds print a leading log line before the JSON body
            m = re.search(r"[\[{].*[\]}]", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
    return p.returncode, data, (p.stderr or "").strip()


def _looks_like_trust_problem(stderr):
    s = (stderr or "").lower()
    return any(k in s for k in ("pair", "trust", "password", "escrow",
                                "not paired", "please accept", "user denied"))


# ---- device discovery -------------------------------------------------------
def _dev_fields(d):
    props = d.get("Properties") or d.get("properties") or d
    conn = (props.get("ConnectionType") or d.get("ConnectionType") or "USB")
    serial = (props.get("SerialNumber") or props.get("UDID") or props.get("Serial")
              or d.get("SerialNumber") or d.get("UDID") or d.get("Serial")
              or d.get("Identifier"))
    return conn, serial


def pick_device():
    global UDID
    rc, data, err = _cli("usbmux", "list", timeout=30, use_udid=False)
    if rc != 0 and data is None:
        out("Could not talk to Apple USB support / pymobiledevice3.", "warn")
        if err:
            out("    " + err.splitlines()[0], "raw")
        out("Make sure the 'Apple Devices' app (or iTunes) is installed and its", "warn")
        out("Apple Mobile Device Service is running, then replug the phone.", "warn")
        return False
    devs = data or []
    usb = [d for d in devs if str(_dev_fields(d)[0]).upper().startswith("USB")] or devs
    if not usb:
        out("No iPhone/iPad detected over USB. Plug it in with a DATA cable", "warn")
        out("(not charge-only), UNLOCK it, and tap 'Trust This Computer'.", "warn")
        return False
    if len(usb) > 1:
        flag(f"{len(usb)} iOS devices attached; using the first. Unplug the "
             "others for a clean read.")
    UDID = _dev_fields(usb[0])[1]
    return True


# ---- collectors -------------------------------------------------------------
def section_identity():
    hdr("DEVICE UNDER TEST")
    rc, info, err = _cli("lockdown", "info", timeout=45)
    if not isinstance(info, dict):
        if _looks_like_trust_problem(err):
            out("The phone hasn't trusted this laptop yet. UNLOCK the iPhone,", "warn")
            out("tap 'Trust This Computer', enter the passcode, then re-run.", "warn")
        else:
            out("Could not read device info." + (f" ({err.splitlines()[0]})"
                if err else ""), "warn")
        return False
    for label, key in (("Name", "DeviceName"), ("Model", "ProductType"),
                       ("iOS", "ProductVersion"), ("Build", "BuildVersion"),
                       ("Serial", "SerialNumber")):
        out(f"  {label:14}: {info.get(key, '?')}", "info")
    if info.get("PasswordProtected") is False:
        flag("Device has NO passcode — anyone with 60 seconds of physical access "
             "can install a surveillance profile. Raise this with the client.")
    return True


def section_profiles():
    hdr("CONFIGURATION & MDM PROFILES  (the #1 iOS surveillance vector)")
    rc, data, err = _cli("profile", "list", timeout=45)
    if not isinstance(data, dict):
        out("  Could not read configuration profiles." + (f" ({err.splitlines()[0]})"
            if err else ""), "warn")
        return
    ids = data.get("OrderedIdentifiers") or list(
        (data.get("ProfileMetadata") or {}).keys())
    meta = data.get("ProfileMetadata") or {}
    if not ids:
        out("  No configuration profiles installed.", "ok")
        out("  (A clean personal iPhone usually has none — a good sign.)", "info")
        return
    flag(f"{len(ids)} configuration profile(s) installed. On a personal phone, "
         "EACH one must be something the client knowingly added:")
    for pid in ids:
        m = meta.get(pid, {}) if isinstance(meta, dict) else {}
        name = m.get("PayloadDisplayName", pid)
        org = m.get("PayloadOrganization", "")
        desc = m.get("PayloadDescription", "")
        out(f"      • {name}" + (f"   (org: {org})" if org else ""), "raw")
        blob = " ".join(str(x) for x in (name, org, desc, pid)).lower()
        for hint, label in _PROFILE_HINTS.items():
            if hint in blob:
                flag(f"    -> looks like: {label}")
                break
    note("MDM, a proxy, a VPN, or an installed root certificate on a PERSONAL "
         "phone are the strongest single leads. Open each on the phone (Settings "
         "> General > VPN & Device Management) and remove anything the client "
         "doesn't recognise. A profile can also be marked non-removable — that "
         "itself is a red flag on a personal device.")


def section_provisioning():
    hdr("PROVISIONING PROFILES  (enterprise / side-loaded apps)")
    rc, data, err = _cli("provision", "list", timeout=45)
    if data is None:
        out("  Could not read provisioning profiles." + (f" ({err.splitlines()[0]})"
            if err else ""), "warn")
        return
    items = data if isinstance(data, list) else (data.get("profiles") if isinstance(
        data, dict) else None) or []
    if not items:
        out("  No provisioning profiles — no enterprise/side-loaded apps.", "ok")
        return
    flag(f"{len(items)} provisioning profile(s) present — apps signed OUTSIDE the "
         "App Store (enterprise/developer), a known spyware delivery route:")
    for it in items:
        if isinstance(it, dict):
            name = it.get("Name") or it.get("name") or "?"
            team = it.get("TeamName") or it.get("team") or "?"
            exp = it.get("ExpirationDate") or it.get("expiration") or ""
            out(f"      • {name}   (team: {team})" + (f"  exp {exp}" if exp else ""),
                "raw")
        else:
            out(f"      • {it}", "raw")
    note("cross-check these against the installed apps below — an enterprise app "
         "the client didn't deliberately install is a strong lead.")


def section_apps():
    hdr("INSTALLED APPS  (user apps; App-Store signer expected)")
    rc, data, err = _cli("apps", "list", timeout=60)
    if not isinstance(data, (dict, list)):
        out("  Could not read installed apps." + (f" ({err.splitlines()[0]})"
            if err else ""), "warn")
        return
    apps = data.values() if isinstance(data, dict) else data
    apps = [a for a in apps if isinstance(a, dict)]
    user = [a for a in apps if str(a.get("ApplicationType", "")).lower() == "user"]
    out(f"  {len(user)} user-installed apps ({len(apps)} total incl. system).",
        "info")
    APPSTORE = "Apple iPhone OS Application Signing"
    odd = []
    for a in user:
        signer = a.get("SignerIdentity", "")
        if signer and APPSTORE not in signer:
            odd.append((a.get("CFBundleDisplayName")
                        or a.get("CFBundleName")
                        or a.get("CFBundleIdentifier", "?"),
                        a.get("CFBundleIdentifier", "?"), signer))
    if odd:
        flag(f"{len(odd)} app(s) NOT signed by the App Store (enterprise/dev/"
             "side-loaded):")
        for name, bid, signer in odd:
            out(f"      • {name}  [{bid}]", "raw")
            out(f"          signer: {signer}", "raw")
    elif user:
        out("  Every user app that reports a signer is App-Store-signed.", "ok")
    note("iOS is sandboxed, so this is a roster, not a behaviour scan. The lead "
         "is an app the client doesn't recognise — especially a non-App-Store one "
         "that lines up with a provisioning profile above.")


def summary():
    hdr("HOW TO READ THIS")
    out("  This is an INDICATOR sheet, not a verdict. On iOS the strongest leads", "info")
    out("  are: an MDM / proxy / VPN / root-cert profile, supervision, or an", "info")
    out("  enterprise app the client never knowingly installed. Walk each flagged", "info")
    out("  item with them; unknown profiles get removed on the phone (Settings >", "info")
    out("  General > VPN & Device Management).", "info")
    out("", "info")
    out("  30-SECOND HAND CHECK (no laptop): on the iPhone, Settings > General >", "info")
    out("  VPN & Device Management. If there is ANY profile or MDM the owner", "info")
    out("  didn't add, that's the finding.", "info")
    out("", "info")
    out("  DEEPER (known mercenary spyware — Pegasus/Predator/etc.): an encrypted", "info")
    out("  local backup analysed with Amnesty's MVT (mvt-ios) against IOC feeds.", "info")
    out("  Ask me to set that up — read-only too, just slower (backup takes time).", "info")
    out("", "info")
    out("  And on RF: a phone streaming audio/screen out transmits on the uplink", "info")
    out("  continuously — your Shield Sweep walk test / power trace sees that even", "info")
    out("  when the phone's config looks clean.", "info")


def write_html(path):
    css = ("body{background:#0b0f14;color:#cdd6e0;font:14px/1.5 Consolas,monospace;"
           "padding:24px}h1{color:#7fd1ff}.h{color:#7fd1ff;font-weight:700}"
           ".warn{color:#ff9b6b}.ok{color:#7bd88f}.raw{color:#8a97a6;white-space:pre}"
           ".info{color:#cdd6e0}")
    body = ["<h1>Shield Sweep — iPhone Integrity Indicators</h1>",
            f"<div class=raw>generated {time.strftime('%Y-%m-%d %H:%M:%S')} — READ-ONLY</div>"]
    for lvl, text in _LINES:
        cls = {"h": "h", "warn": "warn", "ok": "ok", "raw": "raw"}.get(lvl, "info")
        body.append(f"<div class='{cls}'>{html.escape(text) or '&nbsp;'}</div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset=utf-8><style>" + css + "</style>"
                + "".join(body))


def main():
    out("Shield Sweep — iPhone integrity check  (READ-ONLY; nothing is changed "
        "on the phone)", "h")
    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        out("pymobiledevice3 is not installed. Install it once:", "warn")
        out("    python -m pip install --upgrade pymobiledevice3", "raw")
        return 2
    if not pick_device():
        return 1

    def _safe(label, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - keep the report going in the field
            out(f"  [section '{label}' could not complete: {e}]", "warn")
            return None

    # identity doubles as the trust/pairing gate; if it can't read, stop clean
    if _safe("identity", section_identity) is False:
        return 1
    _safe("profiles", section_profiles)
    _safe("provisioning", section_provisioning)
    _safe("apps", section_apps)
    _safe("summary", summary)

    if "--html" in sys.argv:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "iphone_check_report.html")
        write_html(p)
        out(f"\nHTML report written: {p}", "raw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
