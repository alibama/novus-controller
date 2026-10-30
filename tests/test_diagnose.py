"""
Diagnosis tests: register reads → plain-language findings. No hardware.

Run with:  pytest tests/test_diagnose.py
"""
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import diagnose as dx
import novus_protocol as P


class FakeClient:
    def __init__(self, regs):
        self.regs = regs; self.decimal_places = 0; self.name = "k"
    async def read_registers(self, addr, count=1):
        return [self.regs.get(addr + i, 0) for i in range(count)]


HEALTHY = {51: 1, 77: 1, 200: 896, 201: 894, 202: 300, 213: 1, 214: 1, 219: 2,
           220: 0, 221: 2400, 224: 0, 225: 1000, 226: 0, 247: 1, 248: 1, 249: 1,
           250: 30, 252: 1, 254: 2, 257: 0, 258: 70, 259: 20, 260: 5, 261: 10, 279: 0}


def _run(regs, **kw):
    r = asyncio.new_event_loop().run_until_complete(dx.read_registers(FakeClient(regs)))
    return dx.interpret(r, **kw)


def _levels(rep):
    return [f["level"] for f in rep["findings"]]


def test_healthy_controller_has_no_warnings():
    rep = _run(HEALTHY)
    assert "danger" not in _levels(rep) and "warn" not in _levels(rep)
    assert not rep["writes_blocked"] and rep["unit"] == "°F"
    assert "running program 1" in rep["summary"]


def test_protection_blocks_writes():
    rep = _run({**HEALTHY, 51: 3})
    assert rep["writes_blocked"]
    assert any("password" in f["text"] for f in rep["findings"])


def test_celsius_is_flagged_as_danger():
    rep = _run({**HEALTHY, 77: 0})
    assert any(f["level"] == "danger" and "°C" in f["text"] for f in rep["findings"])
    assert rep["unit"] == "°C"


def test_manual_mode_flagged():
    rep = _run({**HEALTHY, 213: 0})
    assert any("MANUAL" in f["text"] for f in rep["findings"])


def test_setpoint_outside_limits():
    rep = _run({**HEALTHY, 221: 1200}, wanted_sp=1465)
    assert any("outside the setpoint limits" in f["text"] for f in rep["findings"])


def test_stuck_output_signature():
    # PV 1271, SP 895, output 0 → stuck SSR
    rep = _run({**HEALTHY, 201: 1271, 200: 895, 202: 0})
    assert any("shorted SSR" in f["text"] for f in rep["findings"])


def test_scaling_with_one_decimal():
    rep = _run({**HEALTHY, 279: 1, 200: 8960, 201: 8940, 221: 24000})
    assert "SP 896" in rep["summary"]


def test_text_render_lists_registers():
    t = dx.to_text(_run(HEALTHY))
    assert "Protection level (51)" in t and "= 1" in t
