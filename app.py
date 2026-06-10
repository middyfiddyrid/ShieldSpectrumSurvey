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
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = Config(os.path.join(BASE_DIR, "config.yaml"))
    if args.simulate:
        cfg.data["device"]["simulate"] = True
        print("*** SIMULATED DEVICE MODE — demo/diagnostic only, not a real sweep ***")

    db = Database(os.path.join(BASE_DIR, "data", "sweeps.db"))
    app = create_app(db, cfg)

    host = cfg.get("app", "host", default="127.0.0.1")
    port = args.port or cfg.get("app", "port", default=8765)
    url = f"http://{host}:{port}"

    if not args.no_browser and cfg.get("app", "open_browser", default=True):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"Shield TSCM Spectrum Survey — {url}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
