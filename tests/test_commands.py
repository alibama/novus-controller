"""
Command-queue tests: the file queue itself, the monitor executing queued
commands through its control() path, the read-only guard, live.json, and
live add/remove of devices. BLE + notify stubbed; no hardware.

Run with:  pytest tests/test_commands.py
"""
import sys, pathlib, types, asyncio, json, os, tempfile, importlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_b = types.ModuleType("bleak"); _b.BleakClient = type("B", (), {})
_b.BleakScanner = type("S", (), {}); _b.BleakError = Exception
sys.modules.setdefault("bleak", _b)
sys.modules.setdefault("notify", types.ModuleType("notify")).send = lambda *a, **k: None

import commands as cmdq
from novus_client import ControllerState


class FakeClient:
    def __init__(self, name="kiln1"):
        self.name = name; self.address = "00:26:A4:00:00:01"; self.connected = False
        self.decimal_places = 0; self.unit = "°F"; self.log = []
    async def connect(self): self.connected = True
    async def disconnect(self): self.connected = False
    async def detect_scaling(self): return 0
    async def read_state(self):
        return ControllerState(name=self.name, address=self.address, connected=True,
                               pv=1200, sp=1465, output_pct=55, running=True,
                               active_program=3)
    async def run_program(self, n): self.log.append(("run", n)); return True
    async def stop_program(self): self.log.append("stop"); return True
    async def set_manual_setpoint(self, t): self.log.append(("sp", t)); return True
    async def read_program(self, n): return None


def _env():
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["KILN_DATA_DIR"] = str(tmp)
    import paths; importlib.reload(paths)
    import monitor as M; importlib.reload(M)
    return tmp, M


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_queue_validate_and_roundtrip():
    tmp = pathlib.Path(tempfile.mkdtemp())
    ok, _ = cmdq.validate("run_program", {"program": 3}); assert ok
    ok, msg = cmdq.validate("run_program", {"program": 42}); assert not ok and "1..20" in msg
    ok, _ = cmdq.validate("explode", {}); assert not ok
    c = cmdq.enqueue(tmp, "kiln1", "run_program", {"program": 3}, source="t")
    assert cmdq.load(tmp, c["id"])["status"] == "pending"
    assert [x["id"] for x in cmdq.pending(tmp)] == [c["id"]]
    cmdq.update(tmp, c["id"], status="done", result={"x": 1})
    got = cmdq.load(tmp, c["id"])
    assert got["status"] == "done" and got["finished_utc"] and not cmdq.pending(tmp)
    assert cmdq.load(tmp, "../etc/passwd") is None


def test_monitor_executes_queued_commands_and_writes_live():
    tmp, M = _env()
    c = FakeClient()
    m = M.Monitor({"kiln1": c}, {"kiln1": M.WatchdogConfig(enabled=False)})
    m.meta["kiln1"] = {"role": "kiln", "model": "N20K48"}
    cmdq.enqueue(M.COMMANDS_DIR, "kiln1", "run_program", {"program": 3})
    cmdq.enqueue(M.COMMANDS_DIR, "kiln1", "set_manual_setpoint", {"setpoint": 900})
    cmdq.enqueue(M.COMMANDS_DIR, "ghost", "stop_program", {})
    _run(m._process_commands())
    assert ("run", 3) in c.log and ("sp", 900.0) in c.log
    statuses = sorted(x["status"] for x in cmdq.recent(M.COMMANDS_DIR))
    assert statuses == ["done", "done", "failed"]
    live = json.loads(M.LIVE_PATH.read_text())
    d = live["devices"]["kiln1"]
    assert d["pv"] == 1200 and d["active_program"] == 3 and d["role"] == "kiln"
    assert "address" not in json.dumps(live).lower().replace("addresses", "")
    assert "00:26:A4" not in M.LIVE_PATH.read_text()


def test_readonly_device_rejects_writes_but_reads():
    tmp, M = _env()
    c = FakeClient("n1200")
    m = M.Monitor({"n1200": c}, {})
    m.writable["n1200"] = False
    cmdq.enqueue(M.COMMANDS_DIR, "n1200", "run_program", {"program": 1})
    cmdq.enqueue(M.COMMANDS_DIR, "n1200", "poll", {})
    _run(m._process_commands())
    st = {x["action"]: x["status"] for x in cmdq.recent(M.COMMANDS_DIR)}
    assert st["run_program"] == "rejected" and st["poll"] == "done"
    assert c.log == []
    try:
        _run(m.control("n1200", "stop_program")); assert False
    except PermissionError:
        pass


def test_live_add_rename_remove_device():
    tmp, M = _env()
    m = M.Monitor({}, {})
    c = FakeClient("new")
    m.add_device(c, M.WatchdogConfig(enabled=False), writable=True,
                 meta={"role": "kiln", "model": "N20K48"})
    assert "new" in m.clients and "new" in m._watch and m.writable["new"]
    m.rename_device("new", "annealer")
    assert "annealer" in m.clients and "new" not in m.clients
    assert m.clients["annealer"].name == "annealer"
    _run(m._poll_body())
    assert m.latest["annealer"].pv == 1200
    _run(m.remove_device("annealer"))
    assert not m.clients and "annealer" not in m.latest
