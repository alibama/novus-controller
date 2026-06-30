"""
kiln_calibrate.py
=================
Characterize a kiln's *achievable* heating performance — the question
"how fast can this kiln actually climb at each temperature, given its
elements, insulation, and current load?"

The result is a performance envelope: max sustained ramp rate (°/min)
as a function of temperature. You run this once per kiln (and re-run
occasionally as elements age, or with a representative load in place),
and then use the envelope to design programs whose ramp segments are
actually achievable — instead of guessing and getting variable results.

HOW IT WORKS
------------
This does NOT run a Ramp & Soak program. It commands the controller into
manual output mode and applies a fixed high output power, then logs PV as
the kiln climbs. From the PV trace it computes ramp rate vs temperature.

Because it pins output power directly, it characterizes the kiln's raw
capability (not the PID's tracking of a moving setpoint). You choose the
target ceiling and the output level.

    python kiln_calibrate.py <BT_ADDRESS> --ceiling 1500 --power 100

SAFETY
------
- This drives the kiln HOT, on purpose. Stay with it. Do not leave it.
- Set --ceiling to a temperature appropriate for an EMPTY kiln test, or
  load it with a representative (sacrificial / safe) load.
- The script stops and reverts to manual 0% output when PV reaches the
  ceiling, on Ctrl-C, or on any error. It also enforces a hard wall-clock
  timeout.
- It does NOT modify any stored program. It only touches CTRL_AUTO (213),
  CTRL_RUN (214), CTRL_MV1 (202) and reads PV.

This is supervisory and assumes you are present. If the host dies mid-run,
the controller will hold the last commanded manual output — which is why
you supervise this and why --timeout exists as a backstop on the host.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from novus_client import NovusClient
from novus_protocol import (
    REG_CTRL_AUTO, REG_CTRL_RUN, REG_OUTPUT_PCT, REG_PV,
)

OUT_DIR = Path(__file__).parent / "calibration"


async def set_manual_output(c: NovusClient, power_pct: float) -> None:
    """Put the controller in manual mode and command a fixed output power."""
    # CTRL_AUTO = 0 (manual), then CTRL_MV1 = power (0..1000), then RUN = 1
    await c.write_register(REG_CTRL_AUTO, 0)
    await asyncio.sleep(0.1)
    await c.write_register(REG_OUTPUT_PCT, int(round(power_pct * 10)))  # 0..1000
    await asyncio.sleep(0.1)
    await c.write_register(REG_CTRL_RUN, 1)


async def all_stop(c: NovusClient) -> None:
    """Revert to a safe state: manual mode, 0% output, run off."""
    try:
        await c.write_register(REG_OUTPUT_PCT, 0)
        await c.write_register(REG_CTRL_RUN, 0)
    except Exception as e:
        print(f"  (warning: all_stop write failed: {e})")


async def calibrate(address: str, ceiling: float, power: float,
                    poll_s: float, timeout_min: float,
                    password: int | None) -> None:
    c = NovusClient("calib", address)
    await c.connect()
    if password is not None:
        await c.open_session(password)
    await c.detect_scaling()
    print(f"connected; decimal_places={c.decimal_places}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = OUT_DIR / f"calib-{stamp}.csv"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    t_start = time.time()
    hard_deadline = t_start + timeout_min * 60
    last_pv = None
    last_t = None

    print(f"Applying {power:.0f}% output, climbing to {ceiling:g}°. "
          f"Logging to {csv_path.name}")
    print(f"{'t(s)':>7} {'PV':>8} {'°/min':>8} {'out%':>6}")

    try:
        await set_manual_output(c, power)
        with csv_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "elapsed_s", "pv", "rate_per_min", "output_pct"])

            while not stop.is_set():
                now = time.time()
                if now > hard_deadline:
                    print("hard timeout reached — stopping")
                    break

                state = await c.read_state()
                pv = state.pv
                elapsed = now - t_start

                rate = ""
                if pv is not None and last_pv is not None and last_t is not None:
                    dt_min = (now - last_t) / 60.0
                    if dt_min > 0:
                        rate = (pv - last_pv) / dt_min

                w.writerow([
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    round(elapsed, 1), pv,
                    round(rate, 2) if rate != "" else "",
                    state.output_pct,
                ])
                f.flush()

                rate_str = f"{rate:8.1f}" if rate != "" else f"{'—':>8}"
                print(f"{elapsed:7.0f} {pv if pv is not None else '—':>8} "
                      f"{rate_str} {state.output_pct:>6}")

                if pv is not None:
                    last_pv, last_t = pv, now
                    if pv >= ceiling:
                        print(f"reached ceiling {ceiling:g}° — stopping")
                        break

                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_s)
                except asyncio.TimeoutError:
                    pass
    finally:
        print("reverting to manual 0% output…")
        await all_stop(c)
        await c.disconnect()

    print(f"\nDone. Raw data: {csv_path}")
    print("Run kiln_analysis.py and open the 'Ramp rate vs temperature' tab, "
          "or analyze the CSV directly: the rate_per_min vs pv columns are your "
          "performance envelope.")


def main():
    ap = argparse.ArgumentParser(description="Characterize kiln ramp-rate vs temperature")
    ap.add_argument("address", help="BLE address, e.g. 00:26:A4:XX:XX:XX")
    ap.add_argument("--ceiling", type=float, required=True,
                    help="stop when PV reaches this temperature")
    ap.add_argument("--power", type=float, default=100.0,
                    help="manual output power percent (default 100)")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="seconds between samples (default 5)")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="hard wall-clock timeout in minutes (default 240)")
    ap.add_argument("--password", type=int, default=None,
                    help="OPEN_SESSION password if the controller is protected")
    args = ap.parse_args()

    if not 0 < args.power <= 100:
        ap.error("--power must be in (0, 100]")

    asyncio.run(calibrate(args.address, args.ceiling, args.power,
                          args.poll, args.timeout, args.password))


if __name__ == "__main__":
    main()
