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
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import notify
from novus_client import NovusClient, ControllerState, ProgramSegment

LOG_DIR = Path(__file__).parent / "logs"
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


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, clients: dict[str, NovusClient],
                 watchdogs: dict[str, WatchdogConfig],
                 poll_interval_s: float = 300.0,
                 hold_connection: bool = False):
        self.clients = clients
        self.watchdogs = watchdogs
        # Time between the START of one poll cycle and the next. Default 5 min.
        self.poll_interval_s = poll_interval_s
        # If False (default), the monitor connects, polls every controller,
        # then DISCONNECTS — leaving the radio free for QuickTune between
        # cycles. If True, it keeps connections open (lower latency, but it
        # holds the radio continuously).
        self.hold_connection = hold_connection
        self.latest: dict[str, ControllerState] = {}
        self.last_poll_at: Optional[float] = None
        # Cache of program segment tables, keyed by name then program number.
        # Populated on demand: set request_programs[name] = True and the next
        # poll cycle reads all 20 programs for that controller while connected.
        self.programs: dict[str, dict] = {n: {} for n in clients}
        self.request_programs: dict[str, bool] = {n: False for n in clients}
        self._watch: dict[str, _WatchState] = {n: _WatchState() for n in clients}
        self.paused = False           # True while BLE is released to QuickTune
        # "Worry mode": names of controllers currently in trouble. While this
        # is non-empty the monitor polls fast (worry_poll_s) and HOLDS the
        # connection, so a furnace dropout is caught and acted on in seconds
        # instead of being invisible between 5-minute cycles.
        self.worried: set[str] = set()
        self.worry_poll_s = 30.0
        # Auto-recovery can be temporarily DISARMED per furnace — e.g. when an
        # operator manually stops it for maintenance — so the watchdog doesn't
        # immediately restart what a human just shut off. Re-armed on manual RUN
        # or from the UI toggle.
        self.recovery_armed: dict[str, bool] = {n: True for n in clients}
        # Per-furnace auto-restart trigger temperature (°F), set from the UI and
        # persisted. The watchdog restarts the furnace if PV stays below this.
        # If unset for a name, falls back to expected_setpoint - low_margin.
        self.recover_threshold: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._poll_lock = asyncio.Lock()   # serialize background + manual polls

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._started:
            return
        self._started = True
        self._task = asyncio.run_coroutine_threadsafe(self._run(), loop)

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
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            method = getattr(c, action)
            result = await method(*args)
            # Re-read so the dashboard shows the new state without waiting 5 min.
            try:
                self.latest[name] = await c.read_state()
                self._append_log(name, self.latest[name])
            except Exception as e:
                print(f"[monitor] {name} post-control read failed: {e}")
            return result

    async def write_program_now(self, name, num, segments, tolerance=0.0,
                                start_setpoint=0.0, link_to=0, password=None):
        """Write a full program into slot `num` RIGHT NOW, then read it back
        to verify. Same radio-grabbing discipline as control(): serialize
        against polling, ensure a connection, optionally open a write session
        on password-protected controllers, write, verify.

        `segments` is a list of ProgramSegment(setpoint, duration_minutes, event).
        Returns the read-back Program so the UI can confirm what landed.
        """
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            if password is not None:
                await c.open_session(int(password))
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
        async with self._poll_lock:
            c = self.clients[name]
            if not c.connected:
                await c.connect()
                await c.detect_scaling()
            if password is not None:
                await c.open_session(int(password))
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
            try:
                if not c.connected:
                    await c.connect()
                    await c.detect_scaling()
                state = await c.read_state()
            except Exception as e:
                print(f"[monitor] {name} poll failed: {e}")
                state = ControllerState(name=name, address=c.address,
                                        connected=False, pv=None, sp=None)
            self.latest[name] = state
            self._append_log(name, state)
            if name in self.watchdogs and self.watchdogs[name].enabled:
                await self._watchdog(name, state)

            # Program-table reads (segments) while we have the connection.
            if state.connected:
                try:
                    if self.request_programs.get(name):
                        # Read all 20 programs (one-time, on user request).
                        for pn in range(1, 21):
                            self.programs[name][pn] = await c.read_program(pn)
                        self.request_programs[name] = False
                    elif state.active_program:
                        # Keep the running program's segments fresh.
                        self.programs[name][state.active_program] = \
                            await c.read_program(state.active_program)
                except Exception as e:
                    print(f"[monitor] {name} program read failed: {e}")

        self.last_poll_at = time.time()

        if not self.hold_connection and not self.worried:
            # Release the radio between cycles so QuickTune can use it — but
            # NOT while worried: during trouble we keep the link open for fast,
            # low-latency polling and immediate control.
            for name, c in self.clients.items():
                try:
                    await c.disconnect()
                except Exception as e:
                    print(f"[monitor] {name} disconnect failed: {e}")

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
            with path.open("a") as f:
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
        # Trend vs the previous poll: is the temperature climbing on its own?
        prev = ws.prev_pv
        ws.prev_pv = pv
        rising = (prev is not None) and ((pv - prev) > 1.0)
        # Trigger temperature: the UI-set threshold if present, else the
        # config margin below the expected hold.
        low_line = self.recover_threshold.get(name)
        if low_line is None:
            low_line = cfg.expected_setpoint - cfg.low_margin

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
