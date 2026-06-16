"""Shield TSCM — Field Spectrum Survey.

Single command to launch:  python app.py     (opens the browser automatically)

Options:
  --simulate     run against the built-in simulated device (demo/dev only;
                 everything is marked SIMULATED in the UI, DB, and reports)
  --port N       override web UI port
  --no-browser   don't auto-open the browser

This is a RECEIVE-ONLY application. No transmit functionality of any kind.
"""

import argparse
import logging
import os
import sys
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from survey.config import Config           # noqa: E402
from survey.db import Database             # noqa: E402
from survey.server import create_app       # noqa: E402


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

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = Config(os.path.join(BASE_DIR, "config.yaml"))
    demo = args.demo or os.environ.get("DCS_DEMO") == "1"
    if args.simulate or demo:
        cfg.data["device"]["simulate"] = True
        print("*** SIMULATED DEVICE MODE — demo/diagnostic only, not a real sweep ***")
    if demo:
        print("*** PUBLIC DEMO MODE — empty demo DB, PII features disabled ***")

    # the public demo runs on its own empty database — real client data
    # (data/sweeps.db) is never exposed
    db_name = "demo.db" if demo else "sweeps.db"
    db = Database(os.path.join(BASE_DIR, "data", db_name))
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

    print(f"Shield TSCM Spectrum Survey — {url}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
