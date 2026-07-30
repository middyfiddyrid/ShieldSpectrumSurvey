#!/usr/bin/env python3
"""Shield Sweep — Android (droid) integrity check.  STANDALONE, READ-ONLY.

Field tool for a TSCM sweep: plug the client's Android phone into this laptop by
USB (with USB debugging turned on) and this reads the phone's own configuration
to surface the mechanisms real spyware / stalkerware / screen-mirror apps rely
on.  It does NOT give a "clean/infected" verdict — no tool honestly can.  It
lists INDICATORS for the operator to walk through with the client, cheapest and
strongest first, exactly like NETWORK CHECK lists unowned hosts.

READ-ONLY BY DESIGN
===================
Every adb call goes through _adb() / _shell(), which refuse any verb that is not
on the read-only allowlist below.  There is NO uninstall, NO `settings put`, NO
`am`/`pm` state change, NO force-stop.  This tool cannot alter the phone; it
reads dumpsys / settings / getprop / pm-list / /proc only.  That is the whole
point: you can hand the phone back exactly as you got it.

WHAT IT LOOKS AT (and why)
  * Screen mirroring / capture ...... MediaProjection sessions, enabled
                                      accessibility services, notification
                                      listeners, cast routes.  These are the
                                      APIs screen-mirror spyware actually uses.
  * Remote control / persistence .... device-admin apps, overlay (draw-over)
                                      grants.  Stalkerware hides here.
  * Installed apps .................. third-party packages + their installer;
                                      anything NOT from the Play Store is
                                      sideloaded and worth a question.
  * High-risk permissions ........... which sideloaded/third-party apps hold
                                      mic / camera / location / SMS / call-log.
  * Traffic / connections ........... live TCP endpoints the phone is talking
                                      to (best-effort; some needs root).

Requires: Android platform-tools (`adb`).  If it is not installed this script
tells you how to get it and stops.  Nothing here needs the Shield Sweep app to
be running or restarted.

Usage:   python droid_check.py            (console report)
         python droid_check.py --html     (also write an HTML report next to it)
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import time

# ---- read-only guard -------------------------------------------------------
# adb subcommands we permit. Anything else raises. `shell` is permitted but the
# shell command's first token is separately checked against _SHELL_OK.
_ADB_OK = {"devices", "shell", "get-state"}
_SHELL_OK = {
    "getprop", "dumpsys", "settings", "pm", "cmd", "cat", "ss", "appops", "svc?",
}
# `settings` / `pm` / `cmd` / `svc` can mutate — pin the exact read-only forms.
_SHELL_ARG_OK = {
    ("settings", "get"),
    ("pm", "list"),
    ("cmd", "appops"),          # only ever called as: cmd appops query-op ...
    ("appops", "query-op"),
    ("appops", "get"),
}


class ReadOnlyViolation(RuntimeError):
    pass


ADB = None     # resolved in main()
DEVICE = None  # selected serial, set in main() so multi-device runs target it


def _adb(*args, timeout=25):
    if not args or args[0] not in _ADB_OK:
        raise ReadOnlyViolation(f"blocked non-read adb call: {args!r}")
    cmd = [ADB]
    # target the chosen serial so 2+ attached devices don't return an empty
    # (silently "clean") result from `adb: more than one device`.
    if DEVICE and args[0] != "devices":
        cmd += ["-s", DEVICE]
    cmd += list(args)
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)


def _shell(cmd, timeout=25):
    """Run `adb shell <cmd>` after checking cmd is read-only."""
    toks = cmd.split()
    head = toks[0].rstrip("?")
    if head not in _SHELL_OK:
        raise ReadOnlyViolation(f"blocked non-read shell command: {cmd!r}")
    if len(toks) >= 2 and head in {"settings", "pm", "cmd", "appops"}:
        pair = (head, toks[1])
        if pair not in _SHELL_ARG_OK:
            raise ReadOnlyViolation(f"blocked shell subcommand: {cmd!r}")
    # pin the appops VERB read-only for both `appops <verb>` and `cmd appops
    # <verb>` — the pair check above only validates the subsystem, so a future
    # edit to `cmd appops set ...` would otherwise slip a mutation through.
    if head == "appops":
        if len(toks) < 2 or toks[1] not in {"query-op", "get"}:
            raise ReadOnlyViolation(f"blocked appops verb: {cmd!r}")
    elif head == "cmd" and len(toks) >= 2 and toks[1] == "appops":
        if len(toks) < 3 or toks[2] not in {"query-op", "get"}:
            raise ReadOnlyViolation(f"blocked cmd appops verb: {cmd!r}")
    return _adb("shell", *toks, timeout=timeout)


# ---- pretty print ----------------------------------------------------------
_LINES = []  # (level, text) for optional HTML export; level: h/ok/warn/info/raw


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


# ---- adb discovery ---------------------------------------------------------
def find_adb():
    p = shutil.which("adb")
    if p:
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    spots = [
        os.path.join(here, "native", "platform-tools", "adb.exe"),
        os.path.join(here, "native", "adb.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        os.path.expandvars(r"%USERPROFILE%\platform-tools\adb.exe"),
        # winget installs Google.PlatformTools but leaves it off PATH
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\adb.exe"),
    ]
    for s in spots:
        if os.path.isfile(s):
            return s
    import glob as _glob
    hits = sorted(_glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools*"
        r"\platform-tools\adb.exe")))
    return hits[-1] if hits else None


def no_adb_help():
    out("Android platform-tools (adb) is NOT installed on this laptop.", "warn")
    out("")
    out("adb is how any tool reads an Android phone. Install it once:", "info")
    out("")
    out("  Fastest (Windows):   winget install Google.PlatformTools", "raw")
    out("  Or download the zip: https://developer.android.com/tools/releases/platform-tools", "raw")
    out("     unzip to  C:\\platform-tools\\  (this script will find it there),", "raw")
    out("     or drop the unzipped folder in  spectrum_survey\\native\\platform-tools\\.", "raw")
    out("")
    out("Then on the phone:  Settings > About > tap 'Build number' 7x to unlock", "info")
    out("Developer options, enable 'USB debugging', plug in USB, tap ALLOW on the", "info")
    out("RSA prompt. Re-run this script.", "info")


# ---- device selection -------------------------------------------------------
def pick_device():
    rc, o, e = _adb("devices")
    if rc != 0:
        out(f"adb devices failed: {e or o}", "warn")
        return None
    rows = [ln.split("\t") for ln in o.splitlines()[1:] if "\t" in ln]
    if not rows:
        out("No phone detected. Plug the Android in by USB and enable USB", "warn")
        out("debugging (Developer options). Then re-run.", "warn")
        return None
    dev, state = rows[0][0], rows[0][1].strip()
    if len(rows) > 1:
        flag(f"{len(rows)} devices attached; using the first ({dev}). "
             "Unplug the others for a clean read.")
    if state == "unauthorized":
        out("Phone is connected but UNAUTHORIZED. Look at the phone screen and", "warn")
        out("tap ALLOW on the 'Allow USB debugging?' prompt, then re-run.", "warn")
        return None
    if state != "device":
        out(f"Phone state is '{state}' (need 'device'). Replug / unlock it.", "warn")
        return None
    return dev


# ---- collectors ------------------------------------------------------------
def section_identity():
    hdr("PHONE UNDER TEST")
    props = {
        "Model": "ro.product.model",
        "Brand": "ro.product.brand",
        "Android": "ro.build.version.release",
        "Security patch": "ro.build.version.security_patch",
        "Build": "ro.build.display.id",
    }
    for label, key in props.items():
        _, v, _ = _shell(f"getprop {key}")
        out(f"  {label:16}: {v or '?'}", "info")
    # stale patch level is itself an exposure
    _, patch, _ = _shell("getprop ro.build.version.security_patch")
    m = re.match(r"(\d{4})-(\d{2})", patch or "")
    if m:
        now = time.gmtime()
        behind = (now.tm_year - int(m.group(1))) * 12 + (now.tm_mon - int(m.group(2)))
        if behind >= 4:
            note(f"security patch is ~{behind} months behind — unpatched phones "
                 "are easier to compromise. Worth flagging to the client.")


def _pkgs_from_component_list(raw):
    """settings values look like  pkg/.Svc:pkg2/.Svc  -> {pkg, pkg2}."""
    if not raw or raw.lower() == "null":
        return []
    return sorted({c.split("/")[0] for c in raw.split(":") if "/" in c})


def section_mirroring():
    hdr("SCREEN MIRRORING / CAPTURE  (what streams your screen out)")

    # 1. Active MediaProjection = something is capturing the screen RIGHT NOW.
    #    Per AOSP, an active grant prints "(<pkg>, uid=<n>): TYPE_SCREEN_CAPTURE"
    #    (or TYPE_MIRRORING / TYPE_PRESENTATION); idle prints "null". The old
    #    tokens (valid=true / mActive) never appear in this dump — keying on them
    #    silently missed live capture, so match the real grant markers instead.
    _, mp, _ = _shell("dumpsys media_projection")
    active = [ln.strip() for ln in mp.splitlines()
              if re.search(r"package|uid=\d+|Session|TYPE_|valid=true", ln, re.I)]
    active_now = bool(
        re.search(r"uid=\d+", mp)
        or re.search(r"\bTYPE_(SCREEN_CAPTURE|MIRRORING|PRESENTATION)\b", mp)
        or re.search(r"valid=true|mActive=true|state=STATE_(STARTED|RECORDING|CAPTURING)",
                     mp, re.I))
    if active_now:
        flag("A screen-capture (MediaProjection) session appears ACTIVE. If the "
             "client isn't casting/recording on purpose, identify the app now.")
        for ln in active[:8]:
            out("        " + ln, "raw")
    else:
        out("  MediaProjection: no active screen-capture session seen.", "ok")

    # 2. Accessibility services = the #1 stalkerware mechanism (reads the screen,
    #    can act as the user). Legit for TalkBack/password managers — verify each.
    _, acc, _ = _shell("settings get secure enabled_accessibility_services")
    acc_pkgs = _pkgs_from_component_list(acc)
    if acc_pkgs:
        flag(f"{len(acc_pkgs)} accessibility service(s) ENABLED — these can read "
             "everything on screen and act for the user:")
        for p in acc_pkgs:
            out("        " + p, "raw")
        note("legit ones: TalkBack, password managers, launchers. Anything the "
             "client doesn't recognise is a strong lead.")
    else:
        out("  Accessibility services: none enabled.", "ok")

    # 3. Notification listeners = can read every notification (2FA codes, msgs).
    _, nl, _ = _shell("settings get secure enabled_notification_listeners")
    nl_pkgs = _pkgs_from_component_list(nl)
    if nl_pkgs:
        flag(f"{len(nl_pkgs)} notification-listener app(s) — can read all "
             "notifications (message previews, 2FA codes):")
        for p in nl_pkgs:
            out("        " + p, "raw")
    else:
        out("  Notification listeners: none enabled.", "ok")

    # 4. Active cast route (best-effort). Only note a genuinely CONNECTED route:
    #    every Android 11+ phone always dumps a default system route, so the old
    #    bare "selectedRoute" match fired on every clean phone.
    _, mr, _ = _shell("dumpsys media_router")
    if re.search(r"status=.*connected.*route", mr, re.I):
        note("a cast/media route is present — screen may be mirroring to a "
             "receiver. Confirm it's the client's own TV/Chromecast.")


def section_persistence():
    hdr("REMOTE CONTROL / PERSISTENCE  (how it stays and takes over)")

    # Device-admin apps: can lock/wipe/enforce policy; stalkerware registers here
    # to be hard to remove.
    _, dp, _ = _shell("dumpsys device_policy")
    admins = sorted(set(re.findall(r"ComponentInfo\{([^/}]+)/", dp)))
    if admins:
        flag(f"{len(admins)} device-admin app(s) — elevated control, resist "
             "uninstall:")
        for p in admins:
            out("        " + p, "raw")
        note("MDM/Find-My-Device/work-profile are legit. An unknown admin is a "
             "classic stalkerware tell.")
    else:
        out("  Device admins: none.", "ok")

    # Draw-over-other-apps (overlay) — used to fake screens / hide, phishing.
    _, ov, _ = _shell("cmd appops query-op SYSTEM_ALERT_WINDOW allow")
    ov_pkgs = sorted(set(re.findall(r"([a-zA-Z][\w.]+\.[\w.]+)", ov)))
    if ov_pkgs:
        note(f"{len(ov_pkgs)} app(s) can draw over other apps (overlay). Common "
             "for chat heads/password managers; also used to hide spyware UI.")
        for p in ov_pkgs[:12]:
            out("        " + p, "raw")

    # Always-on VPN — pins 100% of traffic through one app. An unrecognized one
    # is a covert-exfil channel the (root-limited) traffic section can't see.
    _, vpn, _ = _shell("settings get secure always_on_vpn_app")
    vpn = (vpn or "").strip()
    if vpn and vpn.lower() != "null":
        _, lock, _ = _shell("settings get secure always_on_vpn_lockdown")
        hard = (lock or "").strip() == "1"
        flag(f"ALWAYS-ON VPN is set to '{vpn}'"
             + (" with LOCKDOWN (blocks all non-VPN traffic)" if hard else "")
             + " — all traffic routes through it. Confirm the client set this.")

    # Default SMS app — a switched default handler can read/suppress texts + 2FA.
    _, sms, _ = _shell("settings get secure sms_default_application")
    sms = (sms or "").strip()
    if sms and sms.lower() != "null":
        note(f"default SMS app is '{sms}'. Ask the client: is that their texting "
             "app? A handler they don't recognise can intercept messages/2FA.")


_PLAY = {"com.android.vending"}
_INSTALLERS_SIDELOAD = {"", "null", "com.android.packageinstaller",
                        "com.google.android.packageinstaller",
                        "com.android.shell"}


def section_installed():
    hdr("INSTALLED APPS  (third-party; anything not from Play = sideloaded)")
    _, raw, _ = _shell("pm list packages -3 -i", timeout=40)
    entries = []
    for ln in raw.splitlines():
        m = re.match(r"package:(\S+)(?:\s+installer=(\S+))?", ln.strip())
        if m:
            entries.append((m.group(1), (m.group(2) or "").strip()))
    if not entries:
        out("  No third-party apps enumerated (or none installed).", "info")
        return []
    sideloaded = [(p, i) for p, i in entries
                  if i not in _PLAY and i not in {"com.sec.android.app.samsungapps"}]
    out(f"  {len(entries)} third-party apps total; "
        f"{len(sideloaded)} NOT from a normal app store.", "info")
    for p, i in sideloaded:
        src = i if i and i.lower() != "null" else "unknown/adb-sideloaded"
        flag(f"{p}   (installer: {src})")
    if not sideloaded:
        out("  Every third-party app came from Play/Galaxy Store.", "ok")
    return [p for p, _ in entries]


_RISK_PERMS = {
    "android.permission.RECORD_AUDIO": "mic",
    "android.permission.CAMERA": "camera",
    "android.permission.ACCESS_FINE_LOCATION": "precise-location",
    "android.permission.READ_SMS": "read-SMS",
    "android.permission.RECEIVE_SMS": "receive-SMS",
    "android.permission.READ_CALL_LOG": "call-log",
    "android.permission.READ_CONTACTS": "contacts",
    "android.permission.READ_PHONE_STATE": "phone-state",
    # NOTE: PACKAGE_USAGE_STATS is deliberately NOT here — it's an appop-gated
    # permission that never shows granted=true in `dumpsys package` for the
    # third-party apps we inspect, so a granted=true check for it can't fire.
}


def section_permissions(pkgs, cap=80):
    hdr("HIGH-RISK PERMISSIONS  (mic / camera / location / SMS / calls)")
    if not pkgs:
        out("  (no third-party apps to check)", "info")
        return
    capped = pkgs[:cap]
    if len(pkgs) > cap:
        note(f"checking first {cap} of {len(pkgs)} apps (bounded for speed).")
    hits = 0
    for p in capped:
        _, dp, _ = _shell(f"dumpsys package {p}", timeout=20)
        granted = set()
        for perm, short in _RISK_PERMS.items():
            if re.search(re.escape(perm) + r": granted=true", dp):
                granted.add(short)
        # the combo that matters most: can hear + can phone-home
        if granted:
            hits += 1
            danger = {"mic", "camera"} & granted
            marker = "[!] " if danger else "    "
            lvl = "warn" if danger else "info"
            out(f"  {marker}{p}", lvl)
            out(f"        holds: {', '.join(sorted(granted))}", "raw")
    if not hits:
        out("  No third-party app holds any of the high-risk permissions.", "ok")
    else:
        note("holding a permission is normal (a camera app needs the camera). "
             "The lead is an app that has mic/camera AND you don't recognise it "
             "AND it was sideloaded AND it's an accessibility service.")


def section_traffic():
    hdr("TRAFFIC / LIVE CONNECTIONS  (where the phone is talking to)")
    _, ss, _ = _shell("ss -tunH")
    conns = []
    for ln in (ss or "").splitlines():
        # ss columns: Netid State Recv-Q Send-Q Local Peer
        parts = ln.split()
        if len(parts) >= 6 and parts[1] in {"ESTAB", "SYN-SENT"}:
            conns.append(parts[5])
    if conns:
        remotes = sorted(set(c for c in conns
                             if not c.startswith(("127.", "[::1]", "0.0.0.0"))))
        out(f"  {len(remotes)} distinct remote endpoint(s) with a live "
            "connection:", "info")
        for r in remotes[:40]:
            out("        " + r, "raw")
        note("nothing here is proof by itself — phones talk to many servers. "
             "Cross it with the Shield Sweep RF sweep: a phone that is exfil'ing "
             "audio/screen transmits on the uplink CONTINUOUSLY, which the walk "
             "test / power trace sees even when the app list looks clean.")
    else:
        out("  Could not read live connections without root (Android restricts", "info")
        out("  /proc/net for the shell user on recent versions). This is expected", "info")
        out("  and NOT a finding. Use the RF sweep for the traffic question.", "info")


def summary():
    hdr("HOW TO READ THIS")
    out("  This is an INDICATOR sheet, not a verdict. No line here means 'clean'", "info")
    out("  or 'infected'. The strongest single lead is one app that stacks up:", "info")
    out("    sideloaded  +  accessibility service  +  mic/camera  +  device admin", "raw")
    out("  Walk each flagged item with the client — do they recognise the app?", "info")
    out("  Then confirm on RF: real screen/audio exfiltration shows as a steady", "info")
    out("  uplink the sweep can watch. Reminder: turn USB debugging back OFF on", "info")
    out("  the phone when you're done — leaving it on is its own small exposure.", "info")


def write_html(path):
    css = ("body{background:#0b0f14;color:#cdd6e0;font:14px/1.5 Consolas,monospace;"
           "padding:24px}h1{color:#7fd1ff}.h{color:#7fd1ff;font-weight:700}"
           ".warn{color:#ff9b6b}.ok{color:#7bd88f}.raw{color:#8a97a6;white-space:pre}"
           ".info{color:#cdd6e0}")
    body = [f"<h1>Shield Sweep — Droid Integrity Indicators</h1>",
            f"<div class=raw>generated {time.strftime('%Y-%m-%d %H:%M:%S')} — READ-ONLY</div>"]
    for lvl, text in _LINES:
        cls = {"h": "h", "warn": "warn", "ok": "ok", "raw": "raw"}.get(lvl, "info")
        body.append(f"<div class='{cls}'>{html.escape(text) or '&nbsp;'}</div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset=utf-8><style>{css}</style>"
                + "".join(body))


def main():
    global ADB, DEVICE
    out("Shield Sweep — Android integrity check  (READ-ONLY; nothing is changed "
        "on the phone)", "h")
    ADB = find_adb()
    if not ADB:
        no_adb_help()
        return 2
    out(f"using adb: {ADB}", "raw")
    dev = pick_device()
    if not dev:
        return 1
    DEVICE = dev  # target this serial so 2+ attached devices don't blank the read
    out(f"device: {dev}", "raw")

    # Field safety net: never let one section's parse error abort the whole
    # report in front of a client — each runs isolated and fails soft.
    def _safe(label, fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - deliberate: keep the report going
            out(f"  [section '{label}' could not complete: {e}]", "warn")
            return None

    _safe("identity", section_identity)
    _safe("mirroring", section_mirroring)
    _safe("persistence", section_persistence)
    pkgs = _safe("installed", section_installed) or []
    _safe("permissions", section_permissions, pkgs)
    _safe("traffic", section_traffic)
    _safe("summary", summary)

    if "--html" in sys.argv:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "droid_check_report.html")
        try:
            write_html(p)
            out(f"\nHTML report written: {p}", "raw")
        except OSError as e:
            out(f"\n[could not write HTML report: {e}] — the console output "
                "above is complete", "warn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
