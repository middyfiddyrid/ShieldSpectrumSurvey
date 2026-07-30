"""Android device checker — plug the client's phone into the laptop by USB and
read the phone's OWN configuration to surface the mechanisms that spyware,
stalkerware and screen-mirror apps actually depend on.

THIS DOES NOT PRODUCE A "CLEAN / INFECTED" VERDICT, AND MUST NOT.
================================================================
No tool can honestly declare a phone clean, and a false clean on a client's
handset is worse than no check at all. This module reports INDICATORS with the
evidence attached, exactly like netscan reports unowned LAN hosts: the operator
walks each one with the client and decides. `severity` ranks what to look at
first; it is never a diagnosis.

READ-ONLY BY DESIGN
===================
Every adb invocation goes through AdbRunner.run(), which refuses any verb not on
the allowlist below. There is no uninstall, no `settings put`, no `am`, no
force-stop, no file push. The phone is handed back byte-identical to how it
arrived. The one thing the operator changes is turning USB debugging on (and we
remind them to turn it back off).

WHAT IT READS (and why each matters)
  * MediaProjection session ....... the API screen-mirroring/recording spyware
                                    uses. Active = something is capturing the
                                    screen right now.
  * Accessibility services ........ the single most abused stalkerware hook: it
                                    can read every word on screen and act as the
                                    user. TalkBack/password managers are legit.
  * Notification listeners ........ reads every notification — message previews,
                                    2FA codes.
  * Device admins ................. elevated control + resists uninstall.
  * Overlay (SYSTEM_ALERT_WINDOW) . draw-over-other-apps; hides UI, fakes screens.
  * Installer source .............. anything not from Play/Galaxy is sideloaded.
  * Runtime permissions ........... mic / camera / location / SMS / call log.
  * Live TCP endpoints ............ best-effort; Android restricts /proc/net to
                                    root on modern versions, so an empty result
                                    is a LIMIT, not a finding.

The real traffic question is answered on RF, not here: a handset exfiltrating
audio or screen transmits on the uplink continuously, which the sweep and the
Device Lab walk test can see even when the app list looks ordinary.

Pure/impure split matches netscan.py: every parse_* / score_* helper is pure and
unit-tested; all subprocess contact is isolated in AdbRunner so tests inject
canned output.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

# ---------------------------------------------------------------- read-only --
# adb verbs we permit at all.
_ADB_OK = {"devices", "get-state", "shell"}
# first token of an `adb shell` command.
_SHELL_OK = {"getprop", "dumpsys", "settings", "pm", "cmd", "appops", "ss", "cat"}
# verbs that can mutate unless pinned to their read form.
_SHELL_PINNED = {
    "settings": {"get"},
    "pm": {"list"},
    "cmd": {"appops"},          # only ever `cmd appops query-op ... `
    "appops": {"query-op", "get"},
}


class ReadOnlyViolation(RuntimeError):
    """Raised if anything ever tries to route a state-changing adb call
    through this module. Tests assert on it."""


def assert_read_only(args) -> None:
    if not args or args[0] not in _ADB_OK:
        raise ReadOnlyViolation(f"adb verb not permitted: {args!r}")
    if args[0] != "shell" or len(args) < 2:
        return
    head = args[1]
    if head not in _SHELL_OK:
        raise ReadOnlyViolation(f"shell command not permitted: {args[1:]!r}")
    pinned = _SHELL_PINNED.get(head)
    if pinned is not None:
        if len(args) < 3 or args[2] not in pinned:
            raise ReadOnlyViolation(f"shell subcommand not permitted: {args[1:]!r}")
    # `cmd appops` pins only the subsystem above; pin the VERB too, or a future
    # `cmd appops set|reset|clear-history` would slip a mutation through. (Bare
    # `appops set` is already blocked by the {query-op,get} pin above.)
    if head == "cmd" and len(args) >= 3 and args[2] == "appops":
        if len(args) < 4 or args[3] not in {"query-op", "get"}:
            raise ReadOnlyViolation(f"cmd appops verb not permitted: {args[1:]!r}")


ADB_CANDIDATES = (
    os.path.join("native", "platform-tools", "adb.exe"),
    os.path.join("native", "adb.exe"),
    r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
    r"C:\platform-tools\adb.exe",
    r"%USERPROFILE%\platform-tools\adb.exe",
    # winget installs Google.PlatformTools here but does NOT put it on PATH, so
    # `where adb` finds nothing on a machine that genuinely has adb installed.
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\adb.exe",
)

# same reason — the winget package dir carries a source hash in its name, so it
# has to be globbed rather than hard-coded.
ADB_GLOBS = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools*\platform-tools\adb.exe",
    r"%LOCALAPPDATA%\Android\Sdk\platform-tools*\adb.exe",
)

INSTALL_HINT = (
    "Android platform-tools (adb) is not installed. Fastest: run "
    "`winget install Google.PlatformTools` in a terminal, or unzip "
    "developer.android.com/tools/releases/platform-tools to C:\\platform-tools\\."
)


def find_adb(base_dir: str = "") -> str | None:
    """adb on PATH, else a bundled/standard/winget location. None if absent."""
    p = shutil.which("adb")
    if p:
        return p
    for cand in ADB_CANDIDATES:
        c = os.path.expandvars(cand)
        if not os.path.isabs(c):
            c = os.path.join(base_dir or os.getcwd(), c)
        if os.path.isfile(c):
            return c
    for pattern in ADB_GLOBS:
        hits = sorted(glob.glob(os.path.expandvars(pattern)))
        if hits:
            return hits[-1]          # newest-sorting install wins
    return None


class AdbRunner:
    """All subprocess contact lives here so tests can inject canned output."""

    def __init__(self, adb_path: str, serial: str = "", timeout: int = 25):
        self.adb = adb_path
        self.serial = serial
        self.timeout = timeout

    def run(self, *args, timeout: int | None = None):
        assert_read_only(args)
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout or self.timeout, errors="replace")
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except OSError as e:
            return 127, "", str(e)

    def sh(self, *args, timeout: int | None = None):
        return self.run("shell", *args, timeout=timeout)


# -------------------------------------------------------------- pure parsers --
def parse_devices(raw: str):
    """`adb devices` -> [{'serial':..,'state':..}] (header line dropped)."""
    outs = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith("list of devices"):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            outs.append({"serial": parts[0], "state": parts[1]})
    return outs


def parse_component_list(raw: str):
    """`pkg/.Service:pkg2/.Other` (or 'null') -> sorted unique packages."""
    if not raw or raw.strip().lower() in {"null", "none", ""}:
        return []
    return sorted({c.split("/")[0].strip()
                   for c in raw.replace(",", ":").split(":")
                   if "/" in c and c.split("/")[0].strip()})


PLAY_INSTALLERS = {"com.android.vending", "com.sec.android.app.samsungapps",
                   "com.amazon.venezia", "com.huawei.appmarket"}
_UNKNOWN_INSTALLERS = {"", "null", "none"}


def parse_packages(raw: str):
    """`pm list packages -3 -i` -> [{'pkg':..,'installer':..,'sideloaded':bool}]."""
    outs = []
    for ln in (raw or "").splitlines():
        m = re.match(r"package:(\S+)", ln.strip())
        if not m:
            continue
        pkg = m.group(1)
        mi = re.search(r"installer=(\S+)", ln)
        installer = (mi.group(1) if mi else "").strip()
        norm = installer.lower()
        outs.append({
            "pkg": pkg,
            "installer": installer or "unknown",
            "sideloaded": norm in _UNKNOWN_INSTALLERS or norm not in PLAY_INSTALLERS,
        })
    return outs


def parse_device_admins(raw: str):
    """`dumpsys device_policy` -> packages holding device-admin."""
    return sorted(set(re.findall(r"ComponentInfo\{([^/}\s]+)/", raw or "")))


def parse_media_projection(raw: str):
    """True if a screen-capture session looks ACTIVE. Output format varies a lot
    across OS versions, so we look for any positive session marker."""
    if not raw:
        return False, []
    active = bool(re.search(r"valid=true|mActive\s*[=:]\s*true|"
                            r"Active\s+projection|session=(?!null)\S", raw, re.I))
    ev = [ln.strip() for ln in raw.splitlines()
          if re.search(r"package|uid=|session|valid=", ln, re.I)][:8]
    return active, ev


_RISK_PERMS = {
    "android.permission.RECORD_AUDIO": "mic",
    "android.permission.CAMERA": "camera",
    "android.permission.ACCESS_FINE_LOCATION": "precise-location",
    "android.permission.ACCESS_BACKGROUND_LOCATION": "background-location",
    "android.permission.READ_SMS": "read-SMS",
    "android.permission.RECEIVE_SMS": "receive-SMS",
    "android.permission.READ_CALL_LOG": "call-log",
    "android.permission.READ_CONTACTS": "contacts",
    "android.permission.READ_PHONE_STATE": "phone-state",
}


def parse_granted_perms(raw: str):
    """`dumpsys package <pkg>` -> short names of granted high-risk perms."""
    got = set()
    for perm, short in _RISK_PERMS.items():
        if re.search(re.escape(perm) + r"\s*:\s*granted=true", raw or ""):
            got.add(short)
    return sorted(got)


def parse_connections(raw: str):
    """`ss -tunH` -> sorted unique remote endpoints (loopback dropped)."""
    outs = set()
    for ln in (raw or "").splitlines():
        parts = ln.split()
        if len(parts) >= 6 and parts[1].upper() in {"ESTAB", "SYN-SENT"}:
            peer = parts[5]
            if not peer.startswith(("127.", "[::1]", "0.0.0.0", "*", "[::ffff:127.")):
                outs.add(peer)
    return sorted(outs)


# --------------------------------------------------------------- scoring -----
def score_app(app):
    """Rank how much an app deserves the operator's attention. This is a
    TRIAGE ORDER, not an accusation — a legit password manager scores high on
    purpose, which is why the reasons are always shown next to the score."""
    stack, score = [], 0
    if app.get("accessibility"):
        stack.append("accessibility service (can read the whole screen)")
        score += 4
    if app.get("admin"):
        stack.append("device admin (elevated, resists uninstall)")
        score += 3
    if app.get("notif_listener"):
        stack.append("reads all notifications")
        score += 2
    if app.get("sideloaded"):
        stack.append(f"sideloaded (installer: {app.get('installer', 'unknown')})")
        score += 2
    perms = set(app.get("perms") or [])
    if {"mic", "camera"} & perms:
        stack.append("holds " + "+".join(sorted({"mic", "camera"} & perms)))
        score += 2
    if {"read-SMS", "receive-SMS", "call-log"} & perms:
        stack.append("reads SMS / call log")
        score += 1
    if app.get("overlay"):
        stack.append("draws over other apps")
        score += 1
    return score, stack


def summarize(apps):
    """The lead is an app that STACKS mechanisms, not any single flag."""
    ranked = sorted(apps, key=lambda a: -a.get("score", 0))
    leads = [a for a in ranked if a.get("score", 0) >= 6]
    watch = [a for a in ranked if 3 <= a.get("score", 0) < 6]
    return ranked, leads, watch


# ------------------------------------------------------------------ scan -----
# deep permission dumps cost ~0.2 s each; check every interesting app plus a
# bounded sample of the rest so a 200-app phone still returns promptly.
DEEP_CHECK_CAP = 45


def scan(runner: AdbRunner):
    """Full read-only pass. Returns a dict the UI renders verbatim."""
    res = {"phone": {}, "findings": [], "apps": [], "connections": [],
           "limits": [], "counts": {}}

    for label, prop in (("model", "ro.product.model"), ("brand", "ro.product.brand"),
                        ("android", "ro.build.version.release"),
                        ("patch", "ro.build.version.security_patch"),
                        ("build", "ro.build.display.id")):
        _, v, _ = runner.sh("getprop", prop, timeout=10)
        res["phone"][label] = v

    # ---- screen mirroring / capture
    _, mp, _ = runner.sh("dumpsys", "media_projection", timeout=15)
    mp_active, mp_ev = parse_media_projection(mp)
    if mp_active:
        res["findings"].append({
            "id": "media_projection", "severity": "high",
            "title": "Screen capture session appears ACTIVE",
            "detail": "Something is using MediaProjection — the screen-recording / "
                      "mirroring API — right now.",
            "items": mp_ev,
            "why": "If the client is not deliberately casting or recording, "
                   "identify the app holding this before anything else."})
    else:
        res["findings"].append({
            "id": "media_projection", "severity": "clear",
            "title": "No active screen-capture session",
            "detail": "Nothing is holding MediaProjection at this moment.",
            "items": [],
            "why": "Note it only covers this instant — a mirror app that starts "
                   "on a trigger will not show here. The RF sweep covers that."})

    _, acc_raw, _ = runner.sh("settings", "get", "secure",
                              "enabled_accessibility_services", timeout=10)
    acc = parse_component_list(acc_raw)

    _, nl_raw, _ = runner.sh("settings", "get", "secure",
                             "enabled_notification_listeners", timeout=10)
    notif = parse_component_list(nl_raw)

    _, dp, _ = runner.sh("dumpsys", "device_policy", timeout=15)
    admins = parse_device_admins(dp)

    _, ov, _ = runner.sh("cmd", "appops", "query-op", "SYSTEM_ALERT_WINDOW",
                         "allow", timeout=15)
    overlay = sorted(set(re.findall(r"\b([a-z][\w]*(?:\.[\w]+){2,})\b", ov or "")))

    for key, pkgs, sev, title, why in (
        ("accessibility", acc, "high" if acc else "clear",
         "Accessibility services enabled",
         "This is the mechanism stalkerware most often uses: it can read every "
         "word on screen and act as the user. TalkBack, password managers and "
         "some launchers are legitimate — the client should recognise each one."),
        ("notif_listeners", notif, "watch" if notif else "clear",
         "Notification-listener apps",
         "These read every notification, including message previews and 2FA codes."),
        ("device_admins", admins, "watch" if admins else "clear",
         "Device-admin apps",
         "Elevated control and hard to uninstall. MDM, work profiles and "
         "Find-My-Device are legitimate; an unrecognised admin is a classic tell."),
    ):
        res["findings"].append({
            "id": key, "severity": sev,
            "title": f"{title} — {len(pkgs)}" if pkgs else f"{title}: none",
            "detail": ", ".join(pkgs) if pkgs else "none enabled.",
            "items": pkgs, "why": why})

    # ---- installed apps
    _, pkg_raw, _ = runner.sh("pm", "list", "packages", "-3", "-i", timeout=45)
    pkgs = parse_packages(pkg_raw)
    sideloaded = [p for p in pkgs if p["sideloaded"]]
    res["counts"] = {"third_party": len(pkgs), "sideloaded": len(sideloaded)}
    res["findings"].append({
        "id": "sideloaded",
        "severity": "watch" if sideloaded else "clear",
        "title": (f"Sideloaded apps — {len(sideloaded)} of {len(pkgs)} third-party"
                  if sideloaded else
                  f"All {len(pkgs)} third-party apps came from an app store"),
        "detail": ", ".join(p["pkg"] for p in sideloaded) if sideloaded else
                  "No app bypassed the store.",
        "items": [f"{p['pkg']}  (installer: {p['installer']})" for p in sideloaded],
        "why": "Commercial stalkerware is almost always sideloaded, because the "
               "stores reject it. Sideloaded alone is not proof — developers and "
               "some vendors sideload legitimately."})

    # ---- per-app detail: everything interesting, plus a bounded sample
    acc_s, adm_s, nl_s, ov_s = set(acc), set(admins), set(notif), set(overlay)
    interesting = [p for p in pkgs
                   if p["pkg"] in acc_s | adm_s | nl_s | ov_s or p["sideloaded"]]
    rest = [p for p in pkgs if p not in interesting]
    deep = interesting + rest[:max(0, DEEP_CHECK_CAP - len(interesting))]
    if len(deep) < len(pkgs):
        res["limits"].append(
            f"Permission detail read for {len(deep)} of {len(pkgs)} third-party "
            "apps (every flagged app plus a sample) to keep the scan quick.")

    for p in deep:
        _, praw, _ = runner.sh("dumpsys", "package", p["pkg"], timeout=20)
        app = dict(p)
        app["perms"] = parse_granted_perms(praw)
        app["accessibility"] = p["pkg"] in acc_s
        app["admin"] = p["pkg"] in adm_s
        app["notif_listener"] = p["pkg"] in nl_s
        app["overlay"] = p["pkg"] in ov_s
        app["score"], app["stack"] = score_app(app)
        res["apps"].append(app)

    ranked, leads, watch = summarize(res["apps"])
    res["apps"] = ranked
    res["leads"] = [a["pkg"] for a in leads]
    res["watch"] = [a["pkg"] for a in watch]

    # ---- live connections (best-effort; root-restricted on modern Android)
    _, ss, _ = runner.sh("ss", "-tunH", timeout=15)
    res["connections"] = parse_connections(ss)
    if not res["connections"]:
        res["limits"].append(
            "Live TCP endpoints could not be read — Android restricts /proc/net "
            "to root on recent versions. This is expected and is NOT a finding. "
            "Answer the traffic question on RF instead: continuous uplink while "
            "the phone is idle is the signature worth chasing.")
    return res


def status(base_dir: str = ""):
    """Cheap pre-flight for the UI: is adb here, is a phone attached/authorized."""
    adb = find_adb(base_dir)
    if not adb:
        return {"adb": None, "ready": False, "state": "no_adb", "hint": INSTALL_HINT}
    rc, o, e = AdbRunner(adb).run("devices", timeout=15)
    devs = parse_devices(o)
    if not devs:
        return {"adb": adb, "ready": False, "state": "no_device",
                "hint": "No phone detected. Plug the Android in by USB, then "
                        "enable Developer options > USB debugging."}
    d = devs[0]
    if d["state"] == "unauthorized":
        return {"adb": adb, "ready": False, "state": "unauthorized",
                "serial": d["serial"],
                "hint": "Phone is connected but not authorized. Look at the phone "
                        "and tap ALLOW on the 'Allow USB debugging?' prompt."}
    if d["state"] != "device":
        return {"adb": adb, "ready": False, "state": d["state"],
                "serial": d["serial"],
                "hint": f"Phone state is '{d['state']}' — unlock it and replug."}
    return {"adb": adb, "ready": True, "state": "device", "serial": d["serial"],
            "extra_devices": len(devs) - 1,
            "hint": "Ready. Nothing on the phone is modified by the scan."}
