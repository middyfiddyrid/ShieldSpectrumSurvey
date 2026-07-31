"""Sentry RF Intelligence — live B220 field spectrum investigation.

Single command to launch:  python app.py     (opens the browser automatically)

Options:
  --simulate     run against the built-in simulated device (demo/dev only;
                 everything is marked SIMULATED in the UI, DB, and reports)
  --port N       override web UI port
  --no-browser   don't auto-open the browser

This is a RECEIVE-ONLY application. No transmit functionality of any kind.
"""

import argparse
import faulthandler
import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from survey.config import Config           # noqa: E402
from survey.db import Database             # noqa: E402
from survey.server import create_app       # noqa: E402

_CRASH_LOG = None   # keeps faulthandler's target file open for the process life


def main():
    parser = argparse.ArgumentParser(description="TSCM field spectrum survey")
    parser.add_argument("--simulate", action="store_true",
                        help="use the simulated device (demo only)")
    parser.add_argument("--demo", action="store_true",
                        help="PUBLIC DEMO: simulated device, separate empty database, "
                             "and all PII-bearing features (POI, internal memo, threat "
                             "analysis, case files) disabled. Safe to expose publicly.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # console + rotating file: the radio drivers (UHD/libiio) run in-process in
    # C++, so a USB fault can kill the whole app with nothing on screen — the
    # console closes and every diagnostic dies with it. logs/app.log survives a
    # crash; logs/crash.log gets a per-thread Python stack dump when a native
    # fault (access violation) takes the process down.
    log_dir = os.path.join(BASE_DIR, "logs")
    handlers = [logging.StreamHandler()]
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(RotatingFileHandler(
            os.path.join(log_dir, "app.log"), maxBytes=5_000_000,
            backupCount=5, encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a read-only disk must not block launch
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=handlers)
    try:
        # module-global keeps the file object alive for the process lifetime —
        # faulthandler holds only the fd, which closes if this is GC'd
        global _CRASH_LOG
        _CRASH_LOG = open(os.path.join(log_dir, "crash.log"), "a", encoding="utf-8")
        _CRASH_LOG.write(f"\n=== launch {datetime.now():%Y-%m-%d %H:%M:%S} "
                         f"pid={os.getpid()} ===\n")
        _CRASH_LOG.flush()
        faulthandler.enable(file=_CRASH_LOG, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    logging.getLogger("app").info("Sentry RF Intelligence starting (pid %d)", os.getpid())

    cfg = Config(os.path.join(BASE_DIR, "config.yaml"))
    demo = args.demo or os.environ.get("DCS_DEMO") == "1"
    if args.simulate or demo:
        # runtime-only: writing this into cfg.data would let any settings save
        # persist simulate:true, silently turning later real sweeps into sims
        cfg.set_runtime_override(("device", "simulate"), True)
        print("*** SIMULATED DEVICE MODE — demo/diagnostic only, not a real sweep ***")
    elif cfg.get("device", "simulate", default=False):
        print("!" * 78)
        print("!!! WARNING: config.yaml has device.simulate: true — ALL sweeps will run")
        print("!!! against the SIMULATED device, not real hardware. If this is not")
        print("!!! intentional, set device.simulate to false in config.yaml.")
        print("!" * 78)
    if demo:
        print("*** PUBLIC DEMO MODE — empty demo DB, PII features disabled ***")

    # the public demo runs on its own empty database — real client data
    # (data/sweeps.db) is never exposed
    db_name = "demo.db" if demo else "sweeps.db"
    db = Database(os.path.join(BASE_DIR, "data", db_name))
    if demo:
        # populate the empty demo DB with the committed PII-free signal library
        from survey.demo_seed import seed_demo
        seeded = seed_demo(db, cfg)
        if seeded:
            print(f"*** DEMO SEED — populated signal wiki with {seeded} "
                  f"de-identified reference captures ***")
    app = create_app(db, cfg, demo=demo)

    # Hosting: a platform (Render/Fly/etc.) sets $PORT and needs us bound to
    # 0.0.0.0. Locally we stay on 127.0.0.1 for safety.
    env_port = os.environ.get("PORT")
    hosted = bool(env_port)
    host = "0.0.0.0" if hosted else cfg.get("app", "host", default="127.0.0.1")
    port = args.port or (int(env_port) if env_port else 0) \
        or cfg.get("app", "port", default=8765)
    url = f"http://{host}:{port}"

    if not args.no_browser and not hosted and cfg.get("app", "open_browser", default=True):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    # hands-free: auto-connect the configured radio at launch so the app is ready
    # without a manual DEVICE-card connect (non-fatal if it's unplugged). Pairs
    # with the launcher's crash auto-restart: crash -> relaunch -> radio ready.
    # Skipped for the simulator/demo so a demo run never grabs hardware.
    if not (args.simulate or demo):
        ac = app.config.get("START_AUTOCONNECT")
        if ac:
            ac()

    print(f"Sentry RF Intelligence — {url}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
