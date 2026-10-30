"""
novus_client.py
===============
BLE client for Novus N20K48 controllers via the QuickTune Mobile-compatible
Bluetooth bridge.

This replaces the earlier MOCK client with real I/O. The public API
(`NovusClient.read_state`, `run_program`, `stop_program`, etc.) and
the dataclasses (`ControllerState`, `Program`, `ProgramSegment`) are
preserved so that `kiln_dashboard.py` does not need to change.

Drop this file into your kiln-bt-scanner directory next to
`novus_protocol.py` and the dashboard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient

from novus_protocol import (
    FrameAssembler, build_read_registers, build_write_single,
    build_write_multiple, build_read_device_id, parse_read_response,
    parse_device_id_response, to_signed,
    REG_SETPOINT, REG_PV, REG_OUTPUT_PCT,
    REG_CTRL_AUTO, REG_CTRL_RUN,
    REG_RS_PRN_EXEC, REG_RS_PRN_EDIT, REG_RS_SEG, REG_RS_SEG_TIME,
    REG_RS_TBASE, REG_OPEN_SESSION, REG_DPPO, REG_UNIT,
    REG_SPLL, REG_SPHL,
    REG_TUNE_AUTO, REG_TUNE_PB, REG_TUNE_IR, REG_TUNE_DT, REG_TUNE_CT,
    program_base, RS_SEGMENTS,
    FC_READ_REGISTERS, FC_WRITE_SINGLE, FC_WRITE_MULTIPLE, FC_READ_DEVICE_ID,
)
import novus_models


# ---------------------------------------------------------------------------
# Public dataclasses (unchanged shape from the mock client)
# ---------------------------------------------------------------------------

@dataclass
class ProgramSegment:
    setpoint: float
    duration_minutes: int
    event: int = 0


@dataclass
class Program:
    number: int
    name: str = ""
    tolerance: float = 0.0
    segments: list[ProgramSegment] = field(default_factory=list)
    link_to: int = 0


@dataclass
class ControllerState:
    name: str
    address: str
    connected: bool
    pv: Optional[float]
    sp: Optional[float]
    output_pct: Optional[float] = None
    running: bool = False           # CTRL_RUN (214): is the controller actually driving?
    active_program: Optional[int] = None
    active_segment: Optional[int] = None
    segment_time_left_s: Optional[float] = None
    alarms: list[bool] = field(default_factory=lambda: [False, False, False, False])


# ---------------------------------------------------------------------------
# Real BLE client
# ---------------------------------------------------------------------------

class NovusClient:
    """One client per controller; one BLE connection held open for the lifetime."""

    SERVICE_UUID = novus_models.NOVUS_SERVICE_UUID
    DATA_CHAR    = novus_models.NOVUS_DATA_CHAR

    REQUEST_TIMEOUT_S = 3.0

    def __init__(self, name: str, address: str):
        self.name = name
        self.address = address
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._assembler = FrameAssembler()
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._programs: dict[int, Program] = {}
        # Decimal-point setting: divide raw int16 by 10^DP for display.
        # The N20K48 stores this in a config register; until we read it
        # at connect time, default to 0. Override via
        # `client.decimal_places = 1` if your controller uses 1 DP.
        self.decimal_places = 0
        # Temperature unit as configured on the controller (register 77):
        # "°F", "°C", or "°" until read.
        self.unit = "°"
        # Device identification (vendor / product code / firmware), filled by
        # identify() or read_device_id(). model is the short key, e.g. N20K48.
        self.device_id: dict = {}
        self.model: str = ""

    # ---- BLE plumbing ------------------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        for frame in self._assembler.feed(bytes(data)):
            self._inbox.put_nowait(frame)

    async def connect(self) -> bool:
        if self._client and self._client.is_connected:
            return True
        self._client = BleakClient(self.address, timeout=15.0)
        await self._client.connect()
        try:   # older bleak (<1.0) needed an explicit MTU negotiation on BlueZ
            await self._client._acquire_mtu()          # type: ignore[attr-defined]
        except Exception:
            pass
        await self._client.start_notify(self.DATA_CHAR, self._on_notify)
        return True

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(self.DATA_CHAR)
            except Exception:
                pass
            await self._client.disconnect()
        self._client = None

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    # ---- Low-level request/response ----------------------------------------

    async def _request(self, frame_bytes: bytes, expect_fc: int,
                       expect_count: Optional[int] = None, retries: int = 2):
        """Send a built frame; await the RIGHT response frame.

        BLE notifications are async and a slow/late response from a previous
        request can arrive while we're waiting for this one. Rather than
        hard-fail on the first frame we see, we discard frames that don't match
        (bad CRC, wrong function code, or — for reads — the wrong register
        count) and keep waiting until the matching response arrives or the
        deadline passes, then retry the send. This is what prevents the
        "expected FC 0x47, got 0x46" failures and the misaligned reads that
        crossed a write with a stale read response.
        """
        if not self.connected:
            raise RuntimeError(f"{self.name}: not connected")

        loop = asyncio.get_event_loop()
        async with self._lock:
            last_err: Optional[Exception] = None
            for _attempt in range(retries + 1):
                # Flush anything stale before sending.
                while not self._inbox.empty():
                    self._inbox.get_nowait()

                await self._client.write_gatt_char(self.DATA_CHAR, frame_bytes,
                                                   response=True)

                deadline = loop.time() + self.REQUEST_TIMEOUT_S
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        last_err = TimeoutError(
                            f"{self.name}: no matching response within "
                            f"{self.REQUEST_TIMEOUT_S}s")
                        break
                    try:
                        frame = await asyncio.wait_for(self._inbox.get(),
                                                       timeout=remaining)
                    except asyncio.TimeoutError:
                        last_err = TimeoutError(
                            f"{self.name}: no response within {self.REQUEST_TIMEOUT_S}s")
                        break
                    # Discard stale / mismatched frames and keep waiting.
                    if not frame.crc_ok:
                        last_err = IOError(f"{self.name}: CRC error in response")
                        continue
                    if frame.function_code != expect_fc:
                        last_err = IOError(
                            f"{self.name}: discarded stale FC "
                            f"0x{frame.function_code:02x} (wanted 0x{expect_fc:02x})")
                        continue
                    if expect_count is not None:
                        try:
                            vals = parse_read_response(frame.payload)
                        except Exception as e:
                            last_err = IOError(f"{self.name}: parse error: {e}")
                            continue
                        if len(vals) != expect_count:
                            last_err = IOError(
                                f"{self.name}: discarded misaligned read "
                                f"({len(vals)} regs, wanted {expect_count})")
                            continue
                    return frame
                # deadline hit for this attempt — loop retries the send
            raise last_err or IOError(f"{self.name}: request failed")

    # ---- Modbus-shaped helpers --------------------------------------------

    async def read_registers(self, addr: int, count: int) -> list[int]:
        """Read `count` 16-bit registers starting at `addr`. Returns u16 list."""
        frame = await self._request(build_read_registers(addr, count),
                                    expect_fc=FC_READ_REGISTERS,
                                    expect_count=count)
        return parse_read_response(frame.payload)

    async def write_register(self, addr: int, value: int) -> None:
        """Write a single 16-bit register."""
        await self._request(build_write_single(addr, value),
                            expect_fc=FC_WRITE_SINGLE)

    async def write_registers(self, addr: int, values: list[int]) -> None:
        """Write multiple consecutive 16-bit registers."""
        await self._request(build_write_multiple(addr, values),
                            expect_fc=FC_WRITE_MULTIPLE)

    # ---- identification ----------------------------------------------------

    async def read_device_id(self) -> dict:
        """Ask the controller who it is (Modbus MEI type 14 'Read Device
        Identification'). Returns {vendor, product, revision, model, objects}
        and caches it on self.device_id / self.model. Read-only and safe."""
        frame = await self._request(build_read_device_id(),
                                    expect_fc=FC_READ_DEVICE_ID)
        info = parse_device_id_response(frame.payload)
        info["model"] = novus_models.model_from_text(info.get("product"))
        self.device_id = info
        self.model = info["model"]
        return info

    async def identify(self) -> dict:
        """Connect (if needed), read the device ID, scaling/unit and a first
        state reading, and return a plain dict the UI can show for a device
        that is not in the registry yet. Never writes."""
        await self.connect()
        out = {"address": self.address, "vendor": "", "product": "",
               "revision": "", "model": "", "unit": "", "pv": None, "sp": None,
               "output_pct": None, "running": None, "error": None}
        try:
            out.update({k: v for k, v in (await self.read_device_id()).items()
                        if k in out})
        except Exception as e:
            out["error"] = f"device-id read failed: {e}"
        try:
            await self.detect_scaling()
            out["unit"] = self.unit
            s = await self.read_state()
            out.update(pv=s.pv, sp=s.sp, output_pct=s.output_pct,
                       running=s.running)
        except Exception as e:
            out["error"] = (out["error"] + "; " if out["error"] else "") + \
                           f"state read failed: {e}"
        return out

    async def read_pid(self) -> dict:
        """Read the PID tuning block (257..261): autotune, proportional band,
        integral rate, derivative time, PWM cycle. Read-only."""
        vals = await self.read_registers(REG_TUNE_AUTO, 5)
        return {"autotune": vals[0], "proportional_band": vals[1],
                "integral_rate": vals[2], "derivative_time": vals[3],
                "cycle_time": vals[4]}

    # ---- High-level API used by the dashboard -----------------------------

    def _scale(self, raw: int) -> float:
        """Convert a raw int16 register to a displayed value per decimal_places."""
        return to_signed(raw) / (10 ** self.decimal_places)

    async def read_state(self) -> ControllerState:
        """One round-trip: read PV, SP, output, and program execution state.

        Reads the 16-register block at addr 200 which covers everything
        we need: SP (200), PV (201), output (202), user manual SP (208),
        program-being-executed (214), current segment (215).
        """
        if not self.connected:
            return ControllerState(name=self.name, address=self.address,
                                   connected=False, pv=None, sp=None)
        try:
            # Read 200..214 covers SP/PV/output (200-202) and the run flag (214)
            op = await self.read_registers(REG_SETPOINT, 15)       # 200..214
            # Read 247..250 for program / segment / segment-time
            rs = await self.read_registers(REG_RS_PRN_EXEC, 4)     # 247,248,249,250
        except Exception as e:
            print(f"[{self.name}] read_state failed: {e}")
            return ControllerState(name=self.name, address=self.address,
                                   connected=False, pv=None, sp=None)

        sp_raw, pv_raw, out_raw = op[0], op[1], op[2]
        run_flag  = op[REG_CTRL_RUN - REG_SETPOINT]   # reg 214: 1 = running
        prog_exec = rs[0]          # 247: program selected (NOT cleared on stop)
        cur_seg   = rs[2]          # 249: current segment
        seg_time  = rs[3]          # 250: elapsed time in current segment (s)

        is_running = bool(run_flag)
        # A program is only "active" if the controller is actually running it.
        # Register 247 just holds the last-selected program even when stopped,
        # so trusting it alone falsely shows idle kilns as "running".
        active_prog = prog_exec if (is_running and prog_exec > 0) else None

        return ControllerState(
            name=self.name,
            address=self.address,
            connected=True,
            pv=round(self._scale(pv_raw), 1),
            sp=round(self._scale(sp_raw), 1),
            output_pct=round(out_raw / 10.0, 1),
            running=is_running,
            active_program=active_prog,
            active_segment=cur_seg if active_prog else None,
            segment_time_left_s=None,   # see read_program() to compute remaining
        )

    # ---- decimal point auto-detect ----------------------------------------

    async def detect_scaling(self) -> int:
        """Read INPUT_DPPO (279) and set self.decimal_places accordingly, and
        read the unit register (77) to learn °C/°F. Returns the detected
        decimal-place count. Verify against the panel — the protocol doc notes
        temperature PV may be ×10 regardless of DPPO."""
        try:
            dppo = (await self.read_registers(REG_DPPO, 1))[0]
            if 0 <= dppo <= 3:
                self.decimal_places = dppo
        except Exception as e:
            print(f"[{self.name}] detect_scaling failed: {e}")
        try:
            unit = (await self.read_registers(REG_UNIT, 1))[0]
            self.unit = "°F" if unit == 1 else "°C" if unit == 0 else "°"
        except Exception as e:
            print(f"[{self.name}] unit read failed: {e}")
        return self.decimal_places

    # ---- session / password ------------------------------------------------

    async def open_session(self, password: int) -> None:
        """Unlock config writes on a password-protected controller by writing
        the password to OPEN_SESSION (reg 53). Call right after connect()."""
        await self.write_register(REG_OPEN_SESSION, password)

    # ---- program table read / write ---------------------------------------

    async def read_program(self, num: int) -> Program:
        """Read program `num` (1..20) from the controller's register table."""
        base = program_base(num)
        regs = await self.read_registers(base, 2 + 1 + RS_SEGMENTS * 3)  # tol,link,sp0 + 9*(t,e,sp)
        tol_raw = regs[0]
        link    = regs[1]
        sp0_raw = regs[2]
        segments: list[ProgramSegment] = []
        for k in range(RS_SEGMENTS):
            t = regs[3 + k * 3]          # time
            e = regs[3 + k * 3 + 1]      # event
            sp = regs[3 + k * 3 + 2]     # setpoint
            # Trailing empty segments (time 0 and sp 0) are omitted
            if t == 0 and sp == 0:
                continue
            segments.append(ProgramSegment(
                setpoint=round(self._scale(sp), 1),
                duration_minutes=t,
                event=e,
            ))
        p = Program(
            number=num,
            name=f"Program {num:02d}",
            tolerance=round(self._scale(tol_raw), 1),
            segments=segments,
            link_to=link,
        )
        # also remember the starting setpoint on the dataclass via attribute
        p.start_setpoint = round(self._scale(sp0_raw), 1)  # type: ignore[attr-defined]
        self._programs[num] = p
        return p

    async def write_program(self, num: int, segments: list[ProgramSegment],
                            tolerance: float = 0.0, start_setpoint: float = 0.0,
                            link_to: int = 0) -> None:
        """
        Write a full program (1..20). Up to 9 segments. Values are scaled by
        decimal_places on the way out. Unused trailing segments are zeroed.

        NOTE: on password-protected controllers, call open_session() first.
        Writing the program table is a config change; the controller persists
        R&S program registers as they are written (they are RW holding regs).
        """
        if len(segments) > RS_SEGMENTS:
            raise ValueError(f"max {RS_SEGMENTS} segments, got {len(segments)}")

        def unscale(v: float) -> int:
            return int(round(v * (10 ** self.decimal_places)))

        values: list[int] = [
            unscale(tolerance),       # base+0 tolerance
            int(link_to),             # base+1 link
            unscale(start_setpoint),  # base+2 SP0
        ]
        for k in range(RS_SEGMENTS):
            if k < len(segments):
                seg = segments[k]
                values += [int(seg.duration_minutes), int(seg.event),
                           unscale(seg.setpoint)]
            else:
                values += [0, 0, 0]   # zero out unused segments

        base = program_base(num)
        # 30 registers; write in one multi-register write (well under the 123 limit)
        await self.write_registers(base, values)

    async def set_tolerance(self, num: int, tolerance: float) -> None:
        """Set just the guaranteed-soak tolerance band for program `num`.
        This is the single highest-leverage knob for firing repeatability:
        the segment timer only advances while |PV - SP| <= tolerance."""
        base = program_base(num)
        await self.write_register(base, int(round(tolerance * (10 ** self.decimal_places))))

    async def get_tolerance(self, num: int) -> float:
        base = program_base(num)
        raw = (await self.read_registers(base, 1))[0]
        return round(self._scale(raw), 1)

    def list_programs(self) -> list[Program]:
        """Return cached programs. Call read_program(n) to populate from device.
        Returns placeholders for any not yet read."""
        out = []
        for i in range(1, 21):
            out.append(self._programs.get(i, Program(number=i, name=f"Program {i:02d}")))
        return out

    def get_program(self, num: int) -> Optional[Program]:
        return self._programs.get(num)

    async def run_program(self, num: int) -> bool:
        """
        Start Ramp & Soak program `num` (1..20).

        Correct sequence (per official register map):
          1. reg 247 (RS_PRN_EXEC) := num   → select WHICH program executes
          2. reg 213 (CTRL_AUTO)   := 1     → automatic control mode
          3. reg 214 (CTRL_RUN)    := 1     → start

        The earlier bug ("runs whatever was last selected") came from writing
        213/214 only — 213 is the auto/manual flag and 214 is just run/stop.
        Neither selects the program; register 247 does.
        """
        if not 1 <= num <= 20:
            raise ValueError(f"program number must be 1..20, got {num}")
        await self.write_register(REG_RS_PRN_EXEC, num)
        await asyncio.sleep(0.1)
        await self.write_register(REG_CTRL_AUTO, 1)
        await asyncio.sleep(0.1)
        await self.write_register(REG_CTRL_RUN, 1)
        return True

    async def stop_program(self) -> bool:
        """Stop execution by clearing the run flag (reg 214 := 0)."""
        await self.write_register(REG_CTRL_RUN, 0)
        return True

    async def set_manual_setpoint(self, temp: float) -> dict:
        """Manual override: hold a fixed setpoint in automatic mode with NO
        Ramp & Soak program. Sequence: stop (214:=0), deselect any program
        (247:=0 — otherwise run would restart it and its setpoint would
        overwrite ours within a second), write SP (200), automatic mode (213),
        run (214). Then READ THE SETPOINT BACK: a protected controller or one
        with setpoint limits silently ignores or clamps the write, and the
        caller must be told. Returns {requested, actual, clamped}."""
        raw = int(round(temp * (10 ** self.decimal_places)))
        await self.write_register(REG_CTRL_RUN, 0)
        await asyncio.sleep(0.1)
        await self.write_register(REG_RS_PRN_EXEC, 0)
        await asyncio.sleep(0.1)
        await self.write_register(REG_SETPOINT, raw)
        await asyncio.sleep(0.1)
        await self.write_register(REG_CTRL_AUTO, 1)
        await asyncio.sleep(0.1)
        await self.write_register(REG_CTRL_RUN, 1)
        await asyncio.sleep(0.2)
        rb = await self.read_registers(REG_SETPOINT, 1)
        actual = self._scale(rb[0]) if rb else None
        return {"requested": float(temp), "actual": actual,
                "clamped": (actual is None or abs(actual - temp) > 0.5)}

    async def is_running(self) -> int:
        """Return 0 if stopped, else the program number (1..20) being executed."""
        run = (await self.read_registers(REG_CTRL_RUN, 1))[0]
        if not run:
            return 0
        return (await self.read_registers(REG_RS_PRN_EXEC, 1))[0]


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

async def _selftest(address: str):
    print(f"Connecting to {address}...")
    c = NovusClient("test", address)
    await c.connect()
    print(f"  connected={c.connected}")
    try:
        ident = await c.read_device_id()
        print(f"  {ident['vendor']} · {ident['product']} · fw {ident['revision']}"
              f" → model {ident['model'] or '?'}")
    except Exception as e:
        print(f"  (device-id read failed: {e})")
    await c.detect_scaling()
    state = await c.read_state()
    print(f"  PV = {state.pv}    SP = {state.sp}    Output = {state.output_pct}%")
    if state.active_program:
        print(f"  *** Running program {state.active_program}, segment {state.active_segment} ***")
    else:
        print(f"  (idle — no program running)")
    await c.disconnect()
    print("done")


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python novus_client.py <BT_ADDRESS>")
        sys.exit(1)
    asyncio.run(_selftest(sys.argv[1]))
