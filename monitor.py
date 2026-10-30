"""
monitor.py
==========
Always-on background monitor for the kilns. Runs as a daemon task on the
shared asyncio loop inside the dashboard process, so it:

  - polls every controller on a fixed cadence REGARDLESS of whether a
    browser tab is open (the old dashboard only polled while someone was
    watching — no good for an overnight furnace watchdog),
  - appends every reading to the CSV logs,
  - runs the furnace watchdog and sends notifications,
  - publishes the latest state for the UI to render cheaply.

WHY IT LIVES IN THE DASHBOARD PROCESS
-------------------------------------
The Novus BLE bridge allows only ONE client per controller at a time.
A separate watchdog process couldn't connect while the dashboard holds
the link. So the monitor and the dashboard share one set of connections.

WATCHDOG SAFETY MODEL (read this before enabling auto_recover)
--------------------------------------------------------------
The watchdog classifies a furnace problem into three cases and treats
them very differently:

  A. DROPPED OUT   — run flag off / output dead while PV is falling below
                     the expected hold. RECOVERABLE: re-assert the program.
                     This is the overnight failure you observed.
  B. CANT_HOLD     — running, output already maxed, PV still falling.
                     Restarting won't help (element/SSR/load fault).
                     ALERT HARD, do NOT auto-restart.
  C. SENSOR_FAULT  — PV reads implausibly (huge jump vs. recent history).
                     A thermocouple reading LOW would otherwise trick us
                     into driving an already-hot furnace. ALERT HARD,
                     do NOT drive output.

Plus: debounce (N consecutive bad reads before acting), a recovery rate
limit (stop auto-recovering after too many in a window — a real fault),
and post-recovery confirmation that PV actually climbs.

auto_recover defaults to FALSE. In that mode the watchdog only alerts.
Turn it on only after you've watched it correctly flag a real event.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import commands as cmdq
import notify
import novus_models
import paths
from novus_client import NovusClient, ControllerState, ProgramSegment

LOG_DIR = paths.LOG_DIR
LIVE_PATH = paths.LIVE_PATH
COMMANDS_DIR = paths.COMMANDS_DIR
LOG_HEADER = ["timestamp_utc", "pv", "sp", "output_pct",
              "active_program", "active_segment", "connected"]


# ---------------------------------------------------------------------------
# Watchdog configuration (per controller, by name)
# ---------------------------------------------------------------------------
@dataclass
class WatchdogConfig:
    """Furnace hold watchdog. The core rule (matches the shop spec):
    if PV stays below (expected_setpoint - low_margin) — e.g. below 2000°F for
    a 2100°F hold — the furnace is restarted (stop then start) and recovery is
    confirmed by watching the temperature climb back. Layered with safety
    guards so it never drives output on a bad reading.
    """
    enabled: bool = False
    expected_setpoint: float = 2100.0     # the hold you expect (furnace: 2100)
    low_margin: float = 100.0             # low_line = expected_setpoint - this
                                          # (2100 - 100 = act below 2000)
    auto_recover: bool = False            # False = alert only; True = restart

    # "Set and forget": program 1 is the hold program on every controller.
    # Recovery re-runs this program. The watchdog only ENFORCES the hold when
    # the controller is running this program (or is stopped AND keep_hot is on)
    # — so it never fights a deliberate firing on some other program.
    hold_program: Optional[int] = 1
    keep_hot: bool = False                # True: also restart from a STOPPED
                                          # state (a furnace, or a kiln you're
                                          # parking warm). False: only rescue an
                                          # active hold that's failing; never
                                          # restart a kiln that finished/stopped.

    # --- timing ---
    # A *definitive* dropout (controller reports NOT running) is unambiguous, so
    # act after a short confirm rather than waiting the full sustain while the
    # furnace coasts. A low reading while still RUNNING is ambiguous, so it must
    # persist for the full sustain window before we touch anything.
    dropout_confirm_s: float = 90.0       # not-running + low: act after this
    sustain_s: float = 300.0              # low-while-running: act after this (5 min)
    cant_hold_confirm_s: float = 120.0    # running + output pinned + NOT recovering:
                                          # act after this (failing output stage)
    worry_poll_s: float = 30.0            # tight poll cadence once worried

    # --- recovery guards ---
    recover_on_cant_hold: bool = True     # ALSO restart when output is pinned high
                                          # but temperature is falling and not
                                          # recovering — i.e. the output stage
                                          # (SSR/contactor/connection) isn't
                                          # delivering power and a stop/start
                                          # re-latches it. The post-restart
                                          # probation stops fast if it doesn't help.
    recover_floor: float = 1000.0         # below this (when expected hot): do NOT
                                          # auto-drive — long outage / fault → human
    implausible_below: float = 200.0      # absurdly low → sensor fault, never drive
    fault_confirm_s: float = 60.0         # implausible must persist this long
    max_recoveries: int = 3               # within recovery_window_s, then give up
    recovery_window_s: float = 3600.0

    # --- post-recovery probation ---
    confirm_after_s: float = 150.0        # by now PV must have risen
    confirm_rise: float = 10.0            # ...by at least this much
    probation_abort_out: float = 99.0     # output pinned here + not rising → STOP fast
    settle_s: float = 600.0               # after a confirmed recovery, don't re-cycle
                                          # for this long (let it climb back)

    # --- trend detection ---
    # "Is it climbing on its own?" is judged over a WINDOW, not between two
    # consecutive 30-second polls: a slow heat-up gains ~1° per poll, which a
    # per-poll test reads as "not rising" every other time and then mislabels
    # a normal cold start as an output-stage fault.
    trend_window_s: float = 300.0         # look back this far
    trend_rise: float = 3.0               # ...and call it rising above this gain

    # --- OVER-temperature guard ---
    # PV well above the live setpoint is never normal. With output at ~0% the
    # controller is not asking for the heat → the output stage (SSR/contactor)
    # is stuck ON and only cutting power will help: alert hard, repeatedly.
    # With output still high it's the controller (manual mode / tuning / wrong
    # input type) → stop it and call a human.
    high_margin: float = 150.0            # PV > SP + this  → over-temp
    overtemp_confirm_s: float = 180.0     # ...sustained this long
    overtemp_alert_every_s: float = 900.0 # repeat the stuck-output alert this often


@dataclass
class _WatchState:
    low_since: Optional[float] = None
    fault_since: Optional[float] = None
    prev_pv: Optional[float] = None       # last poll's PV, for trend detection
    recoveries: deque = field(default_factory=lambda: deque())
    last_good_program: Optional[int] = None
    recovering: bool = False
    recover_started_at: float = 0.0
    pending_confirm_at: Optional[float] = None
    pending_confirm_pv: Optional[float] = None
    settle_until: float = 0.0
    last_alerted_case: Optional[str] = None
    needs_human: bool = False
    trend: deque = field(default_factory=lambda: deque(maxlen=64))  # (t, pv)
    high_since: Optional[float] = None
    last_overtemp_alert: float = 0.0


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, clients: dict[str, NovusClient],
                 watchdogs: dict[str, WatchdogConfig],
                 poll_interval_s: float = 300.0,
                 hold_connection: bool = False,
                 cooperative: bool = True):
        self.clients = clients
        self.watchdogs = watchdogs
        # Time between the START of one poll cycle and the next. Default 5 min.
        self.poll_interval_s = poll_interval_s
        # If False (default), the monitor connects, polls every controller,
        # then DISCONNECTS — leaving the radio free for QuickTune between
        # cycles. If True, it keeps connections open (lower latency, but it
        # holds the radio continuously).
        self.hold_connection = hold_connection
        # COOPERATIVE mode (default ON): the monitor is a good BLE citizen. It
        # ALWAYS disconnects after each brief touch — even when worried — so it
        # never blocks QuickTune, and if a controller can't be reached (someone
        # is on QuickTune, or it's powered off) it treats that as "unknown /
        # in use", logs it quietly, and keeps to the normal slow cadence instead
        # of escalating to fast polling or recovery. Analytics still fill in
        # whenever the controller is momentarily free.
        self.cooperative = cooperative
        self.latest: dict[str, ControllerState] = {}
        self.last_poll_at: Optional[float] = None
        # Cache of program segment tables, keyed by name then program number.
        # Populated on demand: set request_programs[name] = True and the next
        # poll cycle reads all 20 programs for that controller while connected.
        self.programs: dict[str, dict] = {n: {} for n in clients}
        self.request_programs: dict[str, bool] = {n: False for n in clients}
        self._watch: dict[str, _WatchState] = {n: _WatchState() for n in clients}
        self.paused = False           # True while BLE is released to QuickTune
        # "Worry mode": names of controllers currently in trouble (a real low
        # reading we could actually take). While non-empty the monitor polls
        # faster (worry_poll_s) — but in cooperative mode it STILL disconnects
        # between those faster touches. Unreachable controllers do NOT go here.
        self.worried: set[str] = set()
        self.worry_poll_s = 30.0
        # Consecutive failed-to-reach counts, and last time we logged about it,
        # so a QuickTune session (or a powered-off kiln) doesn't spam the log.
        self._unreachable: dict[str, int] = {n: 0 for n in clients}
        self._last_unreach_log: dict[str, float] = {}
        # Auto-recovery can be temporarily DISARMED per furnace — e.g. when an
        # operator manually stops it for maintenance — so the watchdog doesn't
        # immediately restart what a human just shut off. Re-armed on manual RUN
        # or from the UI toggle.
        self.recovery_armed: dict[str, bool] = {n: True for n in clients}
        # Per-furnace auto-restart trigger temperature (°F), set from the UI and
        # persisted. The watchdog restarts the furnace if PV stays below this.
        # If unset for a name, falls back to expected_setpoint - low_margin.
        self.recover_threshold: dict[str, float] = {}
        # Per-device write permission. A controller whose model this app has
        # not been verified on is READ-ONLY until the operator opts in; any
        # control()/write path refuses otherwise. Missing name => allowed.
        self.writable: dict[str, bool] = {n: True for n in clients}
        # Per-device controller password (register 53). When set, every write
        # path opens a session first — protected controllers reject writes
        # otherwise and the UI just sees "nothing happened".
        self.passwords: dict[str, Optional[int]] = {n: None for n in clients}
        # Per-device descriptive metadata (role, model) published in live.json
        # so API consumers can label readings. Set by the app from devices.json.
        self.meta: dict[str, dict] = {n: {} for n in clients}
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._poll_lock = asyncio.Lock()   # serialize background + manual polls
        self._last_cmd_check = 0.0

    # After this many consecutive unreachable cycles, emit ONE gentle alert
    # (so a genuinely dead furnace still notifies, without per-cycle spam).
    UNREACHABLE_ALERT_AFTER = 6
    UNREACH_LOG_EVERY_S = 600.0        # at most one "unreachable" log per 10 min

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._started:
            return
        self._started = True
        self._task = asyncio.run_coroutine_threadsafe(self._run(), loop)

    # ---- live device set (no restart needed) --------------------------------
    def add_device(self, client: NovusClient, watchdog: Optional[WatchdogConfig] = None,
                   writable: bool = True, meta: Optional[dict] = None,
                   password: Optional[int] = None) -> None:
        """Start monitoring another controller. Safe while running: the new
        entry is picked up on the next poll cycle."""
        name = client.name
        self.clients[name] = client
        if watchdog is not None:
            self.watchdogs[name] = watchdog
        self.programs.setdefault(name, {})
        self.request_programs.setdefault(name, False)
        self._watch.setdefault(name, _WatchState())
        self._unreachable.setdefault(name, 0)
        self.recovery_armed.setdefault(name, True)
        self.writable[name] = bool(writable)
        self.passwords[name] = password
        self.meta[name] = dict(meta or {})

    async def remove_device(self, name: str) -> None:
        """Stop monitoring a controller (disconnects it if connected). Does not
        touch the controller's own state — a running program keeps running."""
        async with self._poll_lock:
            c = self.clients.pop(name, None)
            if c is not None:
                try:
                    await c.disconnect()
                except Exception:
                    pass
            for d in (self.watchdogs, self.programs, self.request_programs,
                      self._watch, self._unreachable, self.recovery_armed,
                      self.recover_threshold, self.writable, self.meta,
                      self.passwords, self.latest, self._last_unreach_log):
                d.pop(name, None)
            self.worried.discard(name)

    def rename_device(self, old: str, new: str) -> None:
        """Carry per-device state across a rename (keyed by name everywhere)."""
        if old == new or old not in self.clients:
            return
        for d in (self.clients, self.watchdogs, self.programs, self.request_programs,
                  self._watch, self._unreachable, self.recovery_armed,
                  self.recover_threshold, self.writable, self.meta, self.passwords,
                  self.latest, self._last_unreach_log):
            if old in d:
                d[new] = d.pop(old)
        self.clients[new].name = new
        if old in self.worried:
            self.worried.discard(old); self.worried.add(new)

    async def scan_now(self, seconds: float = 8.0, novus_oui: str = "00:26:A4"):
        """Discover nearby BLE devices RIGHT NOW, safely.

        BlueZ allows only one discovery at a time per adapter. This serializes
        the scan against the poll loop (so a poll/connect can't overlap it and a
        second scan can't start mid-flight), frees the radio first, and ALWAYS
        stops discovery in a finally — even if the caller's timeout cancels us —
        so a scan can never leave the adapter stuck 'in progress' for the next
        one. If discovery is already running (a prior scan clearing up, or
        another app/tab), it backs off and retries instead of failing.
        Returns a list of {address, name_adv, rssi, novus}.
        """
        from bleak import BleakScanner
        try:
            from bleak.exc import BleakError
        except Exception:                       # pragma: no cover
            BleakError = Exception

        async with self._poll_lock:
            # free the radio so discovery isn't blocked by a held connection
            for c in list(self.clients.values()):
                try:
                    if getattr(c, "connected", False):
                        await c.disconnect()
                except Exception:
                    pass

            last_err = None
            for attempt in range(3):
                scanner = BleakScanner()
                try:
                    await scanner.start()
                    await asyncio.sleep(seconds)
                    try:
                        found = scanner.discovered_devices_and_advertisement_data
                        items = found.items()
                    except AttributeError:      # very old bleak
                        found = await BleakScanner.discover(timeout=0.1,
                                                            return_adv=True)
                        items = found.items()
                    return self._format_scan(items, novus_oui)
                except BleakError as e:
                    last_err = e
                    if "inprogress" in str(e).lower() or "in progress" in str(e).lower():
                        # another discovery is active — let it clear, then retry
                        await asyncio.sleep(2.0 + attempt)
                        continue
                    raise
                finally:
                    try:
                        await scanner.stop()
                    except Exception:
                        pass
            raise last_err or RuntimeError("scan failed")

    @staticmethod
    def _format_scan(items, novus_oui: str = novus_models.NOVUS_OUI):
        """Normalize scanner output and classify each advertisement. A device
        counts as Novus if ANY fingerprint matches: the 00:26:A4 MAC prefix,
        manufacturer id 511, the Novus BLE service UUID, or a model-like name
        (N20K48, N1200, …) — so sibling controllers show up too, not just the
        one model this app was built on. `reasons` says which matched."""
        out = []
        for addr, (dev, adv) in items:
            mfg = getattr(adv, "manufacturer_data", None) or {}
            name = (getattr(adv, "local_name", None)
                    or getattr(dev, "name", "") or "")
            uuids = list(getattr(adv, "service_uuids", None) or [])
            cls = novus_models.classify_advertisement(str(addr), name, mfg, uuids)
            if not cls["novus"] and novus_oui and \
                    str(addr).upper().startswith(novus_oui.upper()):
                cls = {"novus": True, "reasons": [f"MAC prefix {novus_oui}"],
                       "model_guess": ""}
            out.append({
                "address": str(addr).upper(),
                "name_adv": name,
                "rssi": getattr(adv, "rssi", None),
                "novus": cls["novus"],
                "reasons": cls["reasons"],
                "model_guess": cls["model_guess"],
                "mfg_ids": sorted(int(k) for k in mfg.keys()),
            })
        out.sort(key=lambda r: (not r["novus"], -(r["rssi"] or -999)))
        return out

    async def identify_now(self, address: str, name: str = "new") -> dict:
        """Connect briefly to ANY address (registered or not), read its device
        identification, unit/scaling and a first PV/SP reading, then disconnect.
        Read-only. Serialized against polling like every other radio use."""
        async with self._poll_lock:
            existing = next((c for c in self.clients.values()
                             if c.address.upper() == address.upper()), None)
            c = existing or NovusClient(name, address)
            try:
                return await c.identify()
            finally:
                if existing is None:
                    try:
                        await c.disconnect()
                    except Exception:
                        pass

    async def control(self, name: str, action, *args):
        """Run a control command on one controller RIGHT NOW, reliably.

        Intermittent polling means the radio is usually disconnected, so a
        STOP/RUN issued at a random moment often misses the connection window.
        This serializes against the poll loop, ensures a connection, runs the
        action (e.g. client.stop_program), then immediately re-reads state so
        the UI reflects the change. The connection is freed on the next poll.

        `action` is a bound coroutine method name on the client, e.g.
        await monitor.control("furnace", "stop_program")
        await monitor.control("bubba", "run_program", 5)
        """
        self._check_writable(name, action)
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            await self._open_session_if_needed(name, action)
            method = getattr(c, action)
            result = await method(*args)
            # Re-read so the dashboard shows the new state without waiting 5 min.
            try:
                self.latest[name] = await c.read_state()
                self._append_log(name, self.latest[name])
                self._write_live()
            except Exception as e:
                print(f"[monitor] {name} post-control read failed: {e}")
            return result

    # Client methods that change controller state. Everything else is a read.
    WRITE_ACTIONS = {"run_program", "stop_program", "set_manual_setpoint",
                     "write_program", "write_register", "write_registers",
                     "set_tolerance", "open_session"}

    def _check_writable(self, name: str, action: str) -> None:
        if name not in self.clients:
            raise KeyError(f"unknown device '{name}'")
        if action in self.WRITE_ACTIONS and not self.writable.get(name, True):
            raise PermissionError(
                f"{name} is read-only in this app (its controller model is not "
                f"verified). Enable writes for it on the Devices page first.")

    async def _open_session_if_needed(self, name: str, action: str,
                                      password: Optional[int] = None) -> None:
        """Protected controllers reject every write until the password is
        written to register 53. Do that before any write action when a
        password is known for the device (explicit one wins)."""
        pw = password if password is not None else self.passwords.get(name)
        if pw is None or action not in self.WRITE_ACTIONS or action == "open_session":
            return
        try:
            await self.clients[name].open_session(int(pw))
        except Exception as e:
            print(f"[monitor] {name} open_session failed: {e}")

    async def diagnose_now(self, name: str, wanted_sp: Optional[float] = None) -> dict:
        """Read the explain-everything registers (protection, unit, mode,
        limits, program, PID) and return plain-language findings. Read-only."""
        import diagnose as _dx
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            report = await _dx.diagnose(c, expected_unit="°F", wanted_sp=wanted_sp)
            report["device"] = name
            report["unit_seen"] = getattr(c, "unit", "°")
            return report

    async def write_program_now(self, name, num, segments, tolerance=0.0,
                                start_setpoint=0.0, link_to=0, password=None):
        """Write a full program into slot `num` RIGHT NOW, then read it back
        to verify. Same radio-grabbing discipline as control(): serialize
        against polling, ensure a connection, optionally open a write session
        on password-protected controllers, write, verify.

        `segments` is a list of ProgramSegment(setpoint, duration_minutes, event).
        Returns the read-back Program so the UI can confirm what landed.
        """
        self._check_writable(name, "write_program")
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            await self._open_session_if_needed(name, "write_program", password)
            await c.write_program(num, segments, tolerance=tolerance,
                                  start_setpoint=start_setpoint, link_to=link_to)
            prog = await c.read_program(num)
            self.programs.setdefault(name, {})[num] = prog
            return prog

    async def read_registers_now(self, name, addr, count=1):
        """Read `count` raw holding registers starting at `addr`, grabbing the
        radio if needed. Returns a list of ints."""
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            return await c.read_registers(int(addr), int(count))

    async def read_program_now(self, name, num):
        """Read one program slot (1..20) RIGHT NOW, grabbing the radio if
        needed. Returns the Program so the UI can load it into the editor."""
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            prog = await c.read_program(int(num))
            self.programs.setdefault(name, {})[int(num)] = prog
            return prog

    async def snapshot_config_now(self, name):
        """Capture a full, verified configuration snapshot of one controller,
        grabbing the radio like control() does. Returns the snapshot document
        (see config_snapshot.build_snapshot). Reading all 20 programs plus the
        config registers with multi-read verification takes a little while."""
        import config_snapshot
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            return await config_snapshot.read_snapshot(c)

    async def write_register_now(self, name, addr, value, password=None):
        """Write a single raw register value, optionally opening a write
        session first. Reads the value back and returns it for confirmation."""
        self._check_writable(name, "write_register")
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            await self._open_session_if_needed(name, "write_register", password)
            await c.write_register(int(addr), int(value))
            rb = await c.read_registers(int(addr), 1)
            return rb[0] if rb else None

    async def _poll_once(self):
        """Connect (if needed), read every controller, run the watchdog,
        then disconnect unless hold_connection is set. Serialized so a manual
        'Poll now' and the background cycle can't run at the same time."""
        async with self._poll_lock:
            await self._poll_body()

    async def _poll_body(self):
        for name, c in self.clients.items():
            reachable = True
            try:
                if not c.connected:
                    await c.connect()
                    await c.detect_scaling()
                state = await c.read_state()
                self._unreachable[name] = 0
            except Exception as e:
                reachable = False
                self._unreachable[name] = self._unreachable.get(name, 0) + 1
                state = ControllerState(name=name, address=c.address,
                                        connected=False, pv=None, sp=None)
                self._log_unreachable(name, e)
            self.latest[name] = state
            self._append_log(name, state)

            if name in self.watchdogs and self.watchdogs[name].enabled:
                if self.cooperative and not reachable:
                    # Couldn't take a reading. In cooperative mode we do NOT
                    # treat that as a fault — the controller is most likely in
                    # use on QuickTune, or powered off. Don't escalate to worry
                    # or recovery; just note a sustained absence once.
                    self.worried.discard(name)
                    n = self._unreachable[name]
                    if n == self.UNREACHABLE_ALERT_AFTER:
                        self._alert(
                            f"{name}: no reading for {n} cycles — it may be in "
                            f"use on QuickTune, out of range, or powered off. "
                            f"Not intervening.", name)
                else:
                    await self._watchdog(name, state)

            # Program-table reads (segments) while we have the connection.
            if state.connected:
                try:
                    if self.request_programs.get(name):
                        for pn in range(1, 21):
                            self.programs[name][pn] = await c.read_program(pn)
                        self.request_programs[name] = False
                    elif state.active_program:
                        self.programs[name][state.active_program] = \
                            await c.read_program(state.active_program)
                except Exception as e:
                    print(f"[monitor] {name} program read failed: {e}")

        self.last_poll_at = time.time()
        self._write_live()

        # Release the radio between cycles. In cooperative mode this is
        # UNCONDITIONAL — even while worried — so a brief fast-poll during real
        # trouble is still just a touch-and-go and never blocks QuickTune.
        if self.cooperative or (not self.hold_connection and not self.worried):
            for name, c in self.clients.items():
                if c.connected:
                    try:
                        await c.disconnect()
                    except Exception as e:
                        print(f"[monitor] {name} disconnect failed: {e}")

    def _log_unreachable(self, name, err):
        """Rate-limited log for a controller we couldn't reach, so a QuickTune
        session or a powered-off kiln doesn't flood the journal."""
        now = time.time()
        last = self._last_unreach_log.get(name, 0)
        if now - last >= self.UNREACH_LOG_EVERY_S:
            self._last_unreach_log[name] = now
            n = self._unreachable.get(name, 1)
            print(f"[monitor] {name} unreachable (in use on QuickTune, off, or "
                  f"out of range) — {n} cycle(s); leaving it alone. ({err})")

    def _alert(self, msg, name=None):
        """Best-effort notification + event log, safe to call from the loop."""
        try:
            if name:
                self._log_event(name, msg)
        except Exception:
            pass
        try:
            notify.send(msg, title="Kiln monitor", priority="low")
        except Exception:
            pass

    async def _run(self):
        # Announce we're alive (best-effort).
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: notify.send(
                    f"Kiln monitor started — polling every "
                    f"{self.poll_interval_s/60:g} min.",
                    title="Kiln monitor", priority="low"))
        except Exception:
            pass

        while True:
            if self.paused:
                # BLE released to QuickTune (or a long manual session). Make
                # sure we're not holding any links, then idle until resumed.
                for name, c in self.clients.items():
                    if c.connected:
                        try:
                            await c.disconnect()
                        except Exception:
                            pass
                await asyncio.sleep(2)
                continue

            cycle_start = time.time()
            await self._poll_once()

            # Poll fast while worried, lazily otherwise.
            interval = self.worry_poll_s if self.worried else self.poll_interval_s
            elapsed = time.time() - cycle_start
            remaining = max(1.0, interval - elapsed)
            slept = 0.0
            while slept < remaining and not self.paused:
                await asyncio.sleep(min(2.0, remaining - slept))
                slept += 2.0
                # Queued API commands are handled between polls so a remote
                # "run program 3" lands within a couple of seconds.
                try:
                    await self._process_commands()
                except Exception as e:
                    print(f"[monitor] command processing failed: {e}")

    # ---- live state + command queue (what the open-data API sees) ----------
    def live_snapshot(self) -> dict:
        """The latest reading per device as a plain, address-free dict."""
        devs = {}
        for name, c in self.clients.items():
            s = self.latest.get(name)
            m = self.meta.get(name, {})
            devs[name] = {
                "role": m.get("role", ""),
                "model": m.get("model", ""),
                "unit": getattr(c, "unit", "°"),
                "connected": bool(s and s.connected),
                "pv": s.pv if s else None,
                "sp": s.sp if s else None,
                "output_pct": s.output_pct if s else None,
                "running": bool(getattr(s, "running", False)) if s else False,
                "active_program": s.active_program if s else None,
                "active_segment": s.active_segment if s else None,
                "watchdog": {
                    "worried": name in self.worried,
                    "armed": self.recovery_armed.get(name, True),
                    "needs_human": bool(getattr(self._watch.get(name), "needs_human", False)),
                },
            }
        return {
            "schema": "novus-kiln-live/1",
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_poll_utc": (datetime.fromtimestamp(self.last_poll_at, timezone.utc)
                              .isoformat(timespec="seconds") if self.last_poll_at else None),
            "poll_interval_s": self.poll_interval_s,
            "paused": self.paused,
            "devices": devs,
        }

    def _write_live(self) -> None:
        """Publish live.json for the API (atomic replace; best-effort)."""
        try:
            LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = LIVE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.live_snapshot(), indent=2))
            os.replace(tmp, LIVE_PATH)
        except Exception as e:
            print(f"[monitor] live.json write failed: {e}")

    async def _process_commands(self) -> None:
        """Execute queued commands (see commands.py). Each runs through the
        same control() path as a UI button, including the read-only guard."""
        for cmd in cmdq.pending(COMMANDS_DIR):
            cid, name, action = cmd["id"], cmd.get("device"), cmd.get("action")
            args = cmd.get("args") or {}
            cmdq.update(COMMANDS_DIR, cid, status="running")
            try:
                if name not in self.clients:
                    raise KeyError(f"unknown device '{name}'")
                if action == "poll":
                    await self._poll_once()
                    result = self.live_snapshot()["devices"].get(name)
                elif action == "run_program":
                    n = int(args["program"])
                    await self.control(name, "run_program", n)
                    cfg = self.watchdogs.get(name)
                    self.recovery_armed[name] = (cfg is None or cfg.hold_program == n)
                    result = {"running_program": n}
                elif action == "stop_program":
                    await self.control(name, "stop_program")
                    self.recovery_armed[name] = False   # don't fight a remote stop
                    result = {"stopped": True}
                elif action == "set_manual_setpoint":
                    sp = float(args["setpoint"])
                    r = await self.control(name, "set_manual_setpoint", sp)
                    self.recovery_armed[name] = False
                    result = r if isinstance(r, dict) else {"setpoint": sp}
                elif action == "diagnose":
                    result = await self.diagnose_now(
                        name, wanted_sp=(float(args["setpoint"]) if args.get("setpoint")
                                         is not None else None))
                else:
                    raise ValueError(f"unknown action '{action}'")
                self._log_event(name, f"API command {action} {args} → ok ({cmd.get('source')})")
                cmdq.update(COMMANDS_DIR, cid, status="done", result=result)
            except PermissionError as e:
                cmdq.update(COMMANDS_DIR, cid, status="rejected", error=str(e))
            except Exception as e:
                if name:
                    self._log_event(name, f"API command {action} {args} FAILED: {e}")
                cmdq.update(COMMANDS_DIR, cid, status="failed", error=str(e))
        cmdq.prune(COMMANDS_DIR)

    # ---- logging -----------------------------------------------------------
    def _append_log(self, name: str, state: ControllerState):
        date = datetime.now().strftime("%Y-%m-%d")
        path = LOG_DIR / name / f"{date}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        try:
            with path.open("a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(LOG_HEADER)
                w.writerow([
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    state.pv if state.connected else "",
                    state.sp if state.connected else "",
                    state.output_pct if state.connected else "",
                    state.active_program or 0,
                    state.active_segment or 0,
                    int(state.connected),
                ])
        except Exception as e:
            print(f"[monitor] {name} log append failed: {e}")

    # ---- event log (audit trail, independent of notifications) -------------
    def _log_event(self, name: str, msg: str):
        path = LOG_DIR / name / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{msg}\n")
        except Exception as e:
            print(f"[watchdog:{name}] event-log failed: {e}")

    async def _safe_stop(self, name: str):
        """Stop output without trusting the control loop — used when we must
        NOT keep driving (sensor fault, give-up, failed recovery)."""
        try:
            await self.clients[name].stop_program()
        except Exception as e:
            print(f"[watchdog:{name}] safe-stop failed: {e}")

    async def _read_selected_program(self, name: str) -> Optional[int]:
        """Last resort: read register 247 (RS_PRN_EXEC) to learn which program
        was selected, so we can re-run it after a dropout."""
        try:
            from novus_protocol import REG_RS_PRN_EXEC
            vals = await self.clients[name].read_registers(REG_RS_PRN_EXEC, 1)
            n = vals[0] if vals else 0
            return n if 1 <= n <= 20 else None
        except Exception:
            return None

    # ---- watchdog ----------------------------------------------------------
    async def _watchdog(self, name: str, state: ControllerState):
        cfg = self.watchdogs[name]
        ws = self._watch[name]
        now = time.time()

        def alert(msg: str, priority: str = "high"):
            print(f"[watchdog:{name}] {msg}")
            self._log_event(name, msg)
            try:
                import asyncio as _a
                _a.get_event_loop().run_in_executor(
                    None, lambda: notify.send(msg, title=f"Furnace: {name}",
                                              priority=priority))
            except Exception as e:
                print(f"[watchdog:{name}] notify failed: {e}")

        # --- can't see it: worry and alert ---
        if not state.connected or state.pv is None:
            self.worried.add(name)
            if ws.last_alerted_case != "nocontact":
                ws.last_alerted_case = "nocontact"
                alert(f"Lost contact with {name} — cannot read temperature. "
                      f"Check the controller and the Bluetooth link.", "urgent")
            return

        pv = state.pv
        out = state.output_pct if state.output_pct is not None else 0.0
        running = bool(getattr(state, "running", False))
        sp = state.sp if state.sp is not None else cfg.expected_setpoint
        # Trend over a window: is the temperature climbing on its own? Compare
        # against the oldest reading inside trend_window_s (falling back to
        # the previous poll when that's all we have).
        prev = ws.prev_pv
        ws.prev_pv = pv
        while ws.trend and now - ws.trend[0][0] > cfg.trend_window_s:
            ws.trend.popleft()
        ws.trend.append((now, pv))
        window = [p for _, p in ws.trend]
        # rising: it has climbed trend_rise above the LOWEST point in the window
        # (so a dip followed by a climb-back counts as recovering); falling:
        # it has dropped trend_rise below the HIGHEST point in the window.
        oldest = window[0] if len(window) > 1 else prev
        rising = len(window) > 1 and (pv - min(window)) >= cfg.trend_rise
        falling = len(window) > 1 and (pv - max(window)) <= -cfg.trend_rise
        # Trigger temperature: the UI-set threshold if present, else the
        # config margin below the expected hold.
        low_line = self.recover_threshold.get(name)
        if low_line is None:
            low_line = cfg.expected_setpoint - cfg.low_margin

        # --- OVER-temperature: never normal, checked before anything else ---
        # A crash-cool (SP steps down, PV lags above it with output 0) is the
        # one legitimate "PV far above SP" state — and it is FALLING. A stuck
        # output stage holds or climbs. So the stuck alert needs "not falling".
        over = pv > sp + cfg.high_margin and sp > 0
        # With output off we also need a trend (≥2 samples) before judging.
        cooling_or_unknown = out <= 5.0 and (falling or len(window) < 2)
        if over and not cooling_or_unknown:
            ws.high_since = ws.high_since or now
            if (now - ws.high_since) >= cfg.overtemp_confirm_s:
                self.worried.add(name)
                if out <= 5.0:
                    # Controller isn't asking for heat → output stage stuck ON.
                    if now - ws.last_overtemp_alert >= cfg.overtemp_alert_every_s:
                        ws.last_overtemp_alert = now
                        ws.last_alerted_case = "overtemp_stuck"
                        alert(f"{name} is {pv - sp:g}° ABOVE setpoint (PV {pv:g}°, "
                              f"SP {sp:g}°) with output {out:g}%. The controller is "
                              f"NOT asking for this heat — suspect a shorted SSR / "
                              f"welded contactor. Software cannot stop it: CUT "
                              f"ELEMENT POWER at the breaker/contactor now.", "urgent")
                    return
                if ws.last_alerted_case != "overtemp_driving":
                    # Controller is still driving while far too hot → stop it.
                    ws.last_alerted_case = "overtemp_driving"
                    ws.needs_human = True
                    await self._safe_stop(name)
                    alert(f"{name} is {pv - sp:g}° ABOVE setpoint (PV {pv:g}°, SP "
                          f"{sp:g}°) and output was still {out:g}%. STOPPED it. "
                          f"Suspect manual mode, a wrong thermocouple type, or "
                          f"tuning. Needs a human.", "urgent")
                return
        elif over:
            ws.high_since = None          # cooling on its own: not a fault (yet)
        else:
            if ws.last_alerted_case in ("overtemp_stuck", "overtemp_driving"):
                alert(f"{name} back within {cfg.high_margin:g}° of setpoint "
                      f"(PV {pv:g}°, SP {sp:g}°).", "default")
                ws.last_alerted_case = None
            ws.high_since = None

        # Remember the program number whenever it's healthy AND running, so we
        # know what to re-assert if it later drops out.
        if running and state.active_program and pv >= low_line:
            ws.last_good_program = state.active_program

        # --- post-recovery probation -------------------------------------
        if ws.recovering:
            self.worried.add(name)
            rose = pv - (ws.pending_confirm_pv if ws.pending_confirm_pv is not None else pv)
            elapsed = now - ws.recover_started_at
            # Fast danger-abort: driving hard but temperature NOT moving means
            # the loop is chasing a bad (stuck-low) reading — STOP immediately
            # so we don't cook an already-hot furnace.
            if elapsed >= 60 and out >= cfg.probation_abort_out and rose < 3.0:
                await self._safe_stop(name)
                ws.recovering = False
                ws.needs_human = True
                ws.last_alerted_case = "abort"
                alert(f"{name}: after restart, output pinned at {out:g}% but temp "
                      f"is NOT rising (PV {pv:g}°). STOPPED to protect the furnace. "
                      f"The restart didn't help — suspect a real element/SSR/power "
                      f"failure (or a stuck-low sensor). Needs a human.", "urgent")
                return
            if ws.pending_confirm_at is not None and now >= ws.pending_confirm_at:
                if rose >= cfg.confirm_rise:
                    ws.recovering = False
                    ws.settle_until = now + cfg.settle_s
                    ws.last_alerted_case = "recovered_ok"
                    alert(f"{name}: recovery CONFIRMED — PV climbing ({pv:g}°, "
                          f"+{rose:g}° since restart, output {out:g}%).", "default")
                else:
                    await self._safe_stop(name)
                    ws.recovering = False
                    ws.needs_human = True
                    ws.last_alerted_case = "recover_failed"
                    alert(f"{name}: did NOT recover after restart (PV {pv:g}°, "
                          f"only +{rose:g}°). STOPPED. Needs a human — possible "
                          f"element/SSR/power/sensor fault.", "urgent")
            return

        # --- defer to deliberate firings & respect keep_hot --------------
        # If a NON-hold program is running, this is an intentional firing
        # (fuse, anneal, slump…) — leave it completely alone.
        if (running and state.active_program
                and cfg.hold_program is not None
                and state.active_program != cfg.hold_program):
            self.worried.discard(name)
            ws.low_since = None
            ws.last_alerted_case = None
            return
        # If it's stopped and this controller isn't meant to stay hot, don't
        # auto-restart — its firing may have legitimately finished.
        if (not running) and (not cfg.keep_hot):
            self.worried.discard(name)
            ws.low_since = None
            ws.last_alerted_case = None
            return

        # --- sensor fault: absurdly low reading --------------------------
        if pv < cfg.implausible_below and cfg.expected_setpoint > 1000:
            self.worried.add(name)
            ws.fault_since = ws.fault_since or now
            if (now - ws.fault_since) >= cfg.fault_confirm_s and ws.last_alerted_case != "sensor":
                ws.last_alerted_case = "sensor"
                ws.needs_human = True
                await self._safe_stop(name)
                alert(f"{name} reads {pv:g}° — implausibly low for a furnace held "
                      f"at {cfg.expected_setpoint:g}°. Suspected thermocouple/sensor "
                      f"fault. NOT driving output; ensured stopped. Check the TC.", "urgent")
            return
        ws.fault_since = None

        # --- healthy? ----------------------------------------------------
        if pv >= low_line:
            if name in self.worried:
                alert(f"{name} back to normal: PV {pv:g}° (≥ {low_line:g}°).", "default")
            self.worried.discard(name)
            ws.low_since = None
            ws.last_alerted_case = None
            ws.needs_human = False
            return

        # --- we are below the line ---------------------------------------
        self.worried.add(name)                     # tighten polling immediately
        if ws.low_since is None:
            ws.low_since = now
            alert(f"{name} dropped below {low_line:g}°: PV {pv:g}°, "
                  f"running={running}, output {out:g}%, program={state.active_program}. "
                  f"Watching closely.", "high")
        low_for = now - ws.low_since

        if ws.needs_human:
            return                                  # latched bad — no auto-action
        if now < ws.settle_until:
            return                                  # in post-recovery settle window

        # Classify and decide. Three below-line situations:
        #   * not running                         -> dropped out -> restart (fast)
        #   * running, output pinned, climbing    -> recovering on its own; leave it
        #   * running, output pinned, NOT climbing-> output stage isn't delivering
        #       power (failing SSR/contactor/connection). A stop/start re-latches
        #       it and the temperature climbs again -> restart.
        #   * running, output not pinned, low     -> PID still has headroom; wait
        if running and out >= 90.0 and rising:
            ws.last_alerted_case = "recovering_self"
            return
        if running and out >= 90.0 and len(window) < 2:
            return                                  # no trend yet — can't judge

        if not running:
            ready = low_for >= cfg.dropout_confirm_s
            reason = "dropped out (not running)"
        elif out >= 90.0:
            if not cfg.recover_on_cant_hold:
                if ws.last_alerted_case != "cant_hold":
                    ws.last_alerted_case = "cant_hold"
                    alert(f"{name} is at {pv:g}° with output pinned {out:g}% and not "
                          f"holding. Restart disabled for this case — check "
                          f"elements/SSR/load/power.", "urgent")
                return
            ready = low_for >= cfg.cant_hold_confirm_s
            reason = ("output pinned but not holding — output stage not delivering "
                      "power (suspect SSR/contactor/connection)")
        else:
            ready = low_for >= cfg.sustain_s
            reason = "low while running"
        if not ready:
            return

        if not cfg.auto_recover or not self.recovery_armed.get(name, True):
            if ws.last_alerted_case != "alertonly":
                ws.last_alerted_case = "alertonly"
                why = ("auto_recover is OFF" if not cfg.auto_recover
                       else "auto-recovery is DISARMED (manual override)")
                alert(f"{name} {reason}: PV {pv:g}° for {low_for/60:.1f} min. "
                      f"{why} — not acting.", "urgent")
            return

        # Floor guard: don't auto-drive from an implausibly low / uncertain value.
        if pv < cfg.recover_floor:
            if ws.last_alerted_case != "toolow":
                ws.last_alerted_case = "toolow"
                ws.needs_human = True
                await self._safe_stop(name)
                alert(f"{name} is at {pv:g}°, below the {cfg.recover_floor:g}° "
                      f"auto-recover floor. Not auto-driving from a reading this "
                      f"low/uncertain — needs a human. (Ensured stopped.)", "urgent")
            return

        # Rate limit: repeated dropouts = a real fault; stop trying.
        while ws.recoveries and now - ws.recoveries[0] > cfg.recovery_window_s:
            ws.recoveries.popleft()
        if len(ws.recoveries) >= cfg.max_recoveries:
            if ws.last_alerted_case != "gaveup":
                ws.last_alerted_case = "gaveup"
                ws.needs_human = True
                await self._safe_stop(name)
                alert(f"{name}: {len(ws.recoveries)} restarts within "
                      f"{cfg.recovery_window_s/3600:g}h. Stopping auto-recovery and "
                      f"STOPPING the furnace — a recurring fault needs a human "
                      f"(power/resume-mode/sensor).", "urgent")
            return

        # Resolve which program to re-run.
        prog = cfg.hold_program if cfg.hold_program is not None else ws.last_good_program
        if prog is None:
            prog = await self._read_selected_program(name)
        if not prog:
            if ws.last_alerted_case != "noprog":
                ws.last_alerted_case = "noprog"
                ws.needs_human = True
                alert(f"{name} dropped out but I can't tell which program to re-run "
                      f"(set hold_program in the watchdog config). NOT acting.", "urgent")
            return

        # DO IT: turn off, then on — exactly the manual fix, automated.
        try:
            c = self.clients[name]
            self._log_event(name, f"RECOVERY START: PV {pv:g}° ({reason}); "
                                  f"stop→start program {prog}.")
            await c.stop_program()
            await asyncio.sleep(2.0)
            await c.run_program(prog)
            ws.recoveries.append(now)
            ws.recovering = True
            ws.recover_started_at = now
            ws.pending_confirm_pv = pv
            ws.pending_confirm_at = now + cfg.confirm_after_s
            ws.low_since = None
            ws.last_alerted_case = "restarted"
            alert(f"{name}: RESTARTED (program {prog}) after {reason} at {pv:g}°. "
                  f"Confirming the temperature climbs within "
                  f"{cfg.confirm_after_s/60:g} min.", "high")
        except Exception as e:
            ws.needs_human = True
            alert(f"{name}: restart FAILED to write ({e}). Manual intervention "
                  f"needed now.", "urgent")
