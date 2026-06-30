"""
Furnace-watchdog behavior tests. BLE and notifications are stubbed, so these
run anywhere with no hardware. They lock in the safety-critical decisions:
recover real dropouts and output-stage faults, but never drive on a bad
reading, and stop fast when a restart doesn't help.

Run with:  pytest tests/test_watchdog.py
"""
import sys, pathlib, types, asyncio, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Stub bleak + notify BEFORE importing the project modules that pull them in.
_bleak = types.ModuleType("bleak"); _bleak.BleakClient = type("B", (), {})
_bleak.BleakScanner = type("S", (), {}); _bleak.BleakError = Exception
sys.modules["bleak"] = _bleak
_notify = types.ModuleType("notify"); _notify.send = lambda *a, **k: None
sys.modules["notify"] = _notify

import pytest
from monitor import Monitor, WatchdogConfig
from novus_client import ControllerState


class FakeClient:
    def __init__(self):
        self.name = "furnace"; self.address = "x"; self.connected = True
        self.decimal_places = 0; self.log = []
    async def connect(self): self.connected = True
    async def detect_scaling(self): return 0
    async def stop_program(self): self.log.append("stop"); return True
    async def run_program(self, n): self.log.append(("run", n)); return True
    async def read_registers(self, a, n): return [1]


def state(pv, running, out, prog=1):
    return ControllerState(name="furnace", address="x", connected=True, pv=pv,
                           sp=2100, output_pct=out, running=running,
                           active_program=(prog if running else None))


def make(**cfg):
    c = FakeClient()
    cfg.setdefault("dropout_confirm_s", 90)
    cfg.setdefault("cant_hold_confirm_s", 120)
    wd = WatchdogConfig(enabled=True, expected_setpoint=2100, low_margin=100,
                        auto_recover=True, **cfg)
    m = Monitor({"furnace": c}, {"furnace": wd}); m.worry_poll_s = 30
    return m, c


def watch(m, st):
    asyncio.new_event_loop().run_until_complete(m._watchdog("furnace", st))


def test_brownout_dropout_recovers():
    m, c = make(); ws = m._watch["furnace"]
    watch(m, state(2100, True, 0))                 # healthy -> captures program 1
    assert ws.last_good_program == 1
    watch(m, state(1800, False, 0))                # dropped out, coasting
    assert "furnace" in m.worried and c.log == []  # worried, not yet acted
    ws.low_since = time.time() - 95                # past dropout_confirm
    watch(m, state(1790, False, 0))
    assert "stop" in c.log and ("run", 1) in c.log
    assert c.log.index("stop") < c.log.index(("run", 1))   # off THEN on


def test_output_stage_fault_recovers():
    # Running, output pinned 100%, temperature falling -> the real-world fault.
    m, c = make(); ws = m._watch["furnace"]
    watch(m, state(2100, True, 0))
    for pv in (1980, 1953, 1951, 1949):            # falling
        watch(m, state(pv, True, 100))
    assert c.log == []                             # not long enough yet
    ws.low_since = time.time() - 130               # past cant_hold_confirm
    watch(m, state(1945, True, 100))
    assert ("run", 1) in c.log                     # restarted


def test_self_recovering_is_left_alone():
    m, c = make(); ws = m._watch["furnace"]
    watch(m, state(2100, True, 0))
    watch(m, state(1950, True, 100))               # first low poll (prev=1950)
    ws.low_since = time.time() - 200               # would be "ready"
    watch(m, state(1965, True, 100))               # +15 -> rising
    assert c.log == []                             # must not interfere


def test_implausible_low_never_drives():
    m, c = make(fault_confirm_s=60); ws = m._watch["furnace"]
    watch(m, state(150, False, 0))                 # sensor reads 150 -> fault
    assert c.log == []
    ws.fault_since = time.time() - 65
    watch(m, state(150, False, 0))
    assert "stop" in c.log and ws.needs_human and ("run", 1) not in c.log


def test_below_floor_never_drives():
    m, c = make(recover_floor=1000, dropout_confirm_s=0)
    ws = m._watch["furnace"]; ws.last_good_program = 1
    ws.low_since = time.time() - 5
    watch(m, state(800, False, 0))                 # below the 1000 floor
    assert ("run", 1) not in c.log and ws.needs_human


def test_failed_recovery_stops_fast():
    # Restart, but temperature does not rise -> stop within ~60s, flag a human.
    m, c = make(); ws = m._watch["furnace"]
    watch(m, state(2100, True, 0))
    ws.low_since = time.time() - 130
    watch(m, state(1900, True, 100))               # triggers restart
    assert ("run", 1) in c.log
    c.log.clear()
    ws.recover_started_at = time.time() - 65       # probation, 65s in
    watch(m, state(1895, True, 100))               # still falling, pinned
    assert "stop" in c.log and ws.needs_human


def test_rate_limit_gives_up():
    m, c = make(dropout_confirm_s=0, max_recoveries=3, settle_s=0)
    ws = m._watch["furnace"]; ws.last_good_program = 1
    now = time.time(); ws.recoveries.extend([now - 9, now - 6, now - 3])
    ws.low_since = now - 5
    watch(m, state(1800, False, 0))
    assert ("run", 1) not in c.log and "stop" in c.log and ws.needs_human


def test_disarmed_does_not_restart():
    m, c = make(); ws = m._watch["furnace"]
    watch(m, state(2100, True, 0))
    m.recovery_armed["furnace"] = False            # operator override
    ws.low_since = time.time() - 200
    watch(m, state(1800, False, 0))
    assert ("run", 1) not in c.log


def test_ui_threshold_governs_trigger():
    m, c = make(); ws = m._watch["furnace"]
    m.recover_threshold["furnace"] = 1900.0        # set from the UI
    watch(m, state(2100, True, 0))
    watch(m, state(1950, True, 40))                # above 1900 -> healthy
    assert "furnace" not in m.worried and c.log == []
