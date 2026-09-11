"""
Scan robustness tests: recover from BlueZ "operation already in progress" and
ALWAYS stop discovery (even on cancellation) so the adapter is never left stuck
for the next scan. Fake bleak — no hardware.

Run with:  pytest tests/test_scan.py
"""
import sys, pathlib, types, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


class _Adv:
    def __init__(self, name, rssi, mfg=None):
        self.local_name = name; self.rssi = rssi; self.manufacturer_data = mfg or {}


class _Dev:
    def __init__(self, name):
        self.name = name


class _InProgress(Exception):
    pass


def _install_fake_bleak(state, in_progress_first=False):
    class FakeScanner:
        def __init__(self, *a, **k):
            pass
        async def start(self):
            state["starts"] += 1
            if in_progress_first and state["discovering"]:
                state["discovering"] = False
                raise _InProgress("[org.bluez.Error.InProgress] "
                                  "Operation already in progress")
        async def stop(self):
            state["stops"] += 1
        @property
        def discovered_devices_and_advertisement_data(self):
            return {"00:26:A4:11:22:33": (_Dev("n"), _Adv("N20K48", -55, {511: b"x"})),
                    "AA:BB:CC:DD:EE:FF": (_Dev("p"), _Adv("Phone", -80))}
    bleak = types.ModuleType("bleak")
    bleak.BleakScanner = FakeScanner
    bleak.BleakClient = type("B", (), {})
    bleak.BleakError = Exception
    sys.modules["bleak"] = bleak
    exc = types.ModuleType("bleak.exc"); exc.BleakError = _InProgress
    sys.modules["bleak.exc"] = exc
    sys.modules.setdefault("notify", types.ModuleType("notify")).send = lambda *a, **k: None
    return FakeScanner


def _monitor():
    from monitor import Monitor
    return Monitor({}, {})


def test_scan_recovers_from_in_progress_and_sorts_novus_first():
    state = {"discovering": True, "starts": 0, "stops": 0}
    _install_fake_bleak(state, in_progress_first=True)
    import importlib
    import monitor as M; importlib.reload(M)
    res = asyncio.new_event_loop().run_until_complete(
        M.Monitor({}, {}).scan_now(seconds=0.001))
    assert state["starts"] == 2          # retried past InProgress
    assert state["stops"] >= 2           # stopped each attempt
    assert res[0]["novus"] and res[0]["address"].startswith("00:26:A4")
    assert res[1]["novus"] is False


def test_scan_stops_discovery_on_cancellation():
    state = {"discovering": False, "starts": 0, "stops": 0}
    _install_fake_bleak(state, in_progress_first=False)
    import importlib
    import monitor as M; importlib.reload(M)
    mon = M.Monitor({}, {})

    async def go():
        task = asyncio.ensure_future(mon.scan_now(seconds=5))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.new_event_loop().run_until_complete(go())
    assert state["stops"] >= 1           # discovery stopped despite cancel
