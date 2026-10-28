"""
paths.py — where this install keeps its data.

Everything the app writes (device list, logs, settings, snapshots, notebooks,
the live-state file the API serves, the command queue the API writes) lives
under ONE data directory so it is easy to back up and easy to point the API
at. Default: the repository directory itself (so a plain checkout "just
works"). Override with the KILN_DATA_DIR environment variable.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("KILN_DATA_DIR") or REPO_DIR)

DEVICES_PATH = DATA_DIR / "devices.json"
LOG_DIR = DATA_DIR / "logs"
SNAPSHOT_DIR = DATA_DIR / "config_snapshots"
NOTEBOOK_DIR = DATA_DIR / "notebooks"
NOTES_DIR = DATA_DIR / "notes"
STATE_DIR = DATA_DIR / "state"            # live.json — latest reading per device
COMMANDS_DIR = DATA_DIR / "commands"      # API → monitor command queue
WATCHDOG_PATH = DATA_DIR / "watchdog_settings.json"
STUDIO_PATH = DATA_DIR / "studio_settings.json"
MONITOR_PATH = DATA_DIR / "monitor_settings.json"
API_ENV_PATH = DATA_DIR / "api.env"
LIVE_PATH = STATE_DIR / "live.json"
