"""
Config-snapshot tests: verified reads, canonical hashing, diff, tamper check,
and human-readable rendering. No hardware — a fake client supplies register and
program reads.

Run with:  pytest tests/test_snapshot.py
"""
import sys, pathlib, types, asyncio, copy
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_bleak = types.ModuleType("bleak"); _bleak.BleakClient = type("B", (), {})
sys.modules.setdefault("bleak", _bleak)

import config_snapshot as cs
import novus_protocol as P
from novus_client import Program, ProgramSegment


class FakeClient:
    def __init__(self):
        self.name = "furnace"; self.address = "00:26:A4:XX:XX:XX"
        self.decimal_places = 0; self.connected = True
        self.regs = {P.REG_UNIT: 1, P.REG_DPPO: 0, P.REG_SETPOINT: 2100,
                     P.REG_RS_PRN_EXEC: 1, 253: 0}
        self.flaky = {}; self._i = {}

    async def read_registers(self, addr, count=1):
        if addr in self.flaky:
            seq = self.flaky[addr]; i = self._i.get(addr, 0); self._i[addr] = i + 1
            return [seq[i % len(seq)]]
        return [self.regs.get(addr, 0)]

    async def read_program(self, num):
        if num == 1:
            p = Program(number=1, tolerance=2.0, link_to=0,
                        segments=[ProgramSegment(2100.0, 60, 0)])
            p.start_setpoint = 70.0
        else:
            p = Program(number=num, tolerance=0.0, link_to=0, segments=[])
            p.start_setpoint = 0.0
        return p


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_clean_snapshot_verifies():
    snap = _run(cs.read_snapshot(FakeClient(), program_range=range(1, 4)))
    assert cs.verify_snapshot(snap)
    assert snap["meta"]["unstable_reads"] == []


def test_identical_config_hashes_identically():
    c = FakeClient()
    a = _run(cs.read_snapshot(c, program_range=range(1, 4)))
    b = _run(cs.read_snapshot(c, program_range=range(1, 4)))
    assert a["meta"]["sha256"] == b["meta"]["sha256"]   # timestamp excluded


def test_diff_catches_a_real_change():
    c = FakeClient()
    a = _run(cs.read_snapshot(c, program_range=range(1, 4)))
    c.regs[P.REG_SETPOINT] = 2150
    b = _run(cs.read_snapshot(c, program_range=range(1, 4)))
    assert a["meta"]["sha256"] != b["meta"]["sha256"]
    changes = cs.diff_snapshots(a, b)
    assert any("2100 → 2150" in ch for ch in changes)


def test_unstable_read_is_flagged_not_trusted():
    c = FakeClient(); c.flaky[253] = [0, 1, 2]     # never agrees
    snap = _run(cs.read_snapshot(c, program_range=range(1, 4)))
    assert "reg 253" in snap["meta"]["unstable_reads"]


def test_tampered_snapshot_fails_verification():
    snap = _run(cs.read_snapshot(FakeClient(), program_range=range(1, 4)))
    bad = copy.deepcopy(snap)
    bad["data"]["config"][str(P.REG_SETPOINT)] = 9999
    assert not cs.verify_snapshot(bad)


def test_human_readable_renders_key_facts():
    snap = _run(cs.read_snapshot(FakeClient(), program_range=range(1, 4)))
    h = cs.to_human(snap)
    assert "SHA-256" in h and "Program  1" in h and "°F" in h
