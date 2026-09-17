"""
Cooperative-mode tests: the monitor must never block QuickTune. When a
controller can't be reached it is treated as "in use / off" (no worry, no
recovery, slow cadence); a real low reading still triggers the watchdog; and
the connection is always released after a poll.

Run with:  pytest tests/test_cooperative.py
"""
import sys, pathlib, types, asyncio, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_b = types.ModuleType("bleak"); _b.BleakClient = type("B", (), {})
_b.BleakScanner = type("S", (), {}); _b.BleakError = Exception
sys.modules.setdefault("bleak", _b)
sys.modules.setdefault("notify", types.ModuleType("notify")).send = lambda *a, **k: None

from monitor import Monitor, WatchdogConfig
from novus_client import ControllerState


class Unreachable:
    def __init__(self, name="furnace"):
        self.name = name; self.address = "x"; self.connected = False
        self.decimal_places = 0
    async def connect(self): raise Exception("Device with address x was not found")
    async def detect_scaling(self): return 0
    async def read_state(self): raise Exception("not connected")
    async def disconnect(self): self.connected = False
    async def stop_program(self): self.acted = "stop"; return True
    async def run_program(self, n): self.acted = ("run", n); return True
    async def read_program(self, n): return None


class LowButReachable(Unreachable):
    async def connect(self): self.connected = True
    async def read_state(self):
        return ControllerState(name=self.name, address="x", connected=True,
                               pv=1500, sp=2100, output_pct=100, running=True,
                               active_program=1)


def _fwd(**kw):
    return WatchdogConfig(enabled=True, expected_setpoint=2100, low_margin=100,
                          auto_recover=True, hold_program=1, keep_hot=True, **kw)


def _run(coro):
    asyncio.new_event_loop().run_until_complete(coro)


def test_unreachable_is_not_a_fault():
    c = Unreachable()
    m = Monitor({"furnace": c}, {"furnace": _fwd(dropout_confirm_s=0)},
                cooperative=True)
    _run(_poll_n(m, 8))
    assert "furnace" not in m.worried          # not escalated
    assert not hasattr(c, "acted")             # no stop/run attempted
    # interval stays slow (worried empty) so we don't hammer QuickTune every 30s
    interval = m.worry_poll_s if m.worried else m.poll_interval_s
    assert interval == m.poll_interval_s


def test_real_low_reading_still_protected():
    c = LowButReachable("furnace2")
    m = Monitor({"furnace2": c},
                {"furnace2": _fwd(cant_hold_confirm_s=0, recover_floor=400)},
                cooperative=True)
    ws = m._watch["furnace2"]
    async def go():
        await m._poll_body()
        ws.low_since = time.time() - 999
        await m._poll_body()
    _run(go())
    assert hasattr(c, "acted")                 # watchdog still acts on real data


def test_cooperative_always_releases_connection():
    c = LowButReachable("k")
    m = Monitor({"k": c}, {"k": _fwd()}, cooperative=True)
    _run(m._poll_body())
    assert c.connected is False                 # released even though reachable


async def _poll_n(m, n):
    for _ in range(n):
        await m._poll_body()
