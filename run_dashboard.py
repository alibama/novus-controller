"""
run_dashboard.py — start the always-on monitor, THEN serve the dashboard.

    .venv/bin/python run_dashboard.py [--server.address 0.0.0.0] [--server.port 8501]

Why not plain `streamlit run kiln_dashboard.py`? Streamlit only executes the
app script when a browser session connects, and the monitor (CSV logging,
furnace watchdog, API command queue, live.json) used to be created inside
that script run — after the password gate. So after every reboot or update
nothing was monitored until a human opened the site and logged in. This
launcher calls app_core.boot() first, in the same process, so the monitor is
running from the first second; the Streamlit pages then pick up the same
singletons.

Any other `--key value` pairs are passed to Streamlit as config options.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _flag_options(argv: list[str]) -> dict:
    """Defaults < DASHBOARD_ADDRESS / DASHBOARD_PORT env (api.env, loaded by
    the service) < command-line flags. Set DASHBOARD_ADDRESS=127.0.0.1 when
    `tailscale serve` (or a reverse proxy) fronts the dashboard, so plain
    http://host:8501 stops answering and only the HTTPS name works."""
    opts = {
        "server.headless": True,
        "browser.gatherUsageStats": False,
        "server.address": os.environ.get("DASHBOARD_ADDRESS") or "0.0.0.0",
        "server.port": os.environ.get("DASHBOARD_PORT") or 8501,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
            i += 1
        elif a.startswith("--") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    # type coercion for the common ones
    try:
        opts["server.port"] = int(opts["server.port"])
    except (TypeError, ValueError):
        opts["server.port"] = 8501
    for k in ("server.headless", "browser.gatherUsageStats"):
        if isinstance(opts.get(k), str):
            opts[k] = opts[k].strip().lower() in ("1", "true", "yes", "on")
    return opts


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(HERE)                       # .streamlit/secrets.toml, data dirs
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    # 1. the monitor — before any browser exists
    import app_core
    bridge, clients, monitor, devices, _ = app_core.boot()
    print(f"[run_dashboard] monitor started: {len(devices)} device(s), "
          f"reads every {monitor.poll_interval_s/60:g} min, "
          f"cooperative={monitor.cooperative}", flush=True)

    # 2. Streamlit, in this process (mirrors `streamlit run`)
    from streamlit import config as _config
    from streamlit.web import bootstrap

    script = str(HERE / "kiln_dashboard.py")
    _config._main_script_path = script
    opts = _flag_options(argv)
    bootstrap.load_config_options(flag_options=opts)
    bootstrap.run(script, False, [], opts)


if __name__ == "__main__":
    main()
