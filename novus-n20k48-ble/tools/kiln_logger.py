"""
kiln_logger.py
==============
Continuous state logger for the Novus kilns. Writes one CSV per day per
controller into logs/<name>/<YYYY-MM-DD>.csv.

Run standalone:
    python kiln_logger.py

Or via systemd (see kiln-logger.service in this repo).

Cannot run at the same time as the dashboard — both would try to hold
BLE connections to the same controllers, and the Novus bridge only
accepts one client at a time. Pick one.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from __future__ import annotations

import asyncio
import csv
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

from novus_client import NovusClient
from kiln_config import KILNS

# ---------------------------------------------------------------------------
# Configuration  (KILNS now lives in kiln_config.py)
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
POLL_INTERVAL_S = 5.0
RECONNECT_DELAY_S = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("kiln-logger")


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------
CSV_HEADER = [
    "timestamp_utc",
    "pv", "sp", "output_pct",
    "active_program", "active_segment",
    "connected",
]


def daily_csv(name: str) -> Path:
    date = datetime.now().strftime("%Y-%m-%d")
    p = LOG_DIR / name / f"{date}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def append_state(name: str, state) -> None:
    path = daily_csv(name)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(CSV_HEADER)
        w.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            state.pv if state.connected else "",
            state.sp if state.connected else "",
            state.output_pct if state.connected else "",
            state.active_program or 0,
            state.active_segment or 0,
            int(state.connected),
        ])


# ---------------------------------------------------------------------------
# Per-kiln logging task
# ---------------------------------------------------------------------------
async def logger_for(client: NovusClient, stop: asyncio.Event) -> None:
    while not stop.is_set():
        if not client.connected:
            try:
                log.info(f"{client.name}: connecting…")
                await client.connect()
                log.info(f"{client.name}: connected")
            except Exception as e:
                log.warning(f"{client.name}: connect failed: {e}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_DELAY_S)
                    return
                except asyncio.TimeoutError:
                    continue

        try:
            state = await client.read_state()
            append_state(client.name, state)
        except Exception as e:
            log.warning(f"{client.name}: read failed: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    stop = asyncio.Event()

    def request_stop():
        log.info("shutdown requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    clients = [NovusClient(name, addr) for name, addr in KILNS]
    log.info(f"logging {len(clients)} kilns to {LOG_DIR}")

    await asyncio.gather(*(logger_for(c, stop) for c in clients),
                         return_exceptions=True)

    for c in clients:
        try:
            await c.disconnect()
        except Exception:
            pass
    log.info("done")


if __name__ == "__main__":
    asyncio.run(main())
