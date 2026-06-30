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
    build_write_multiple, parse_read_response, to_signed,
    REG_SETPOINT, REG_PV, REG_OUTPUT_PCT,
    REG_CTRL_AUTO, REG_CTRL_RUN,
    REG_RS_PRN_EXEC, REG_RS_PRN_EDIT, REG_RS_SEG, REG_RS_SEG_TIME,
    REG_RS_TBASE, REG_OPEN_SESSION, REG_DPPO, REG_UNIT,
    REG_SPLL, REG_SPHL,
    program_base, RS_SEGMENTS,
    FC_READ_REGISTERS, FC_WRITE_SINGLE, FC_WRITE_MULTIPLE,
)


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

    SERVICE_UUID = "0783b03e-8535-b5a0-7140-a304d2495cb7"
    DATA_CHAR    = "0783b03e-8535-b5a0-7140-a304d2495cba"

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

    # ---- BLE plumbing ------------------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        for frame in self._assembler.feed(bytes(data)):
            self._inbox.put_nowait(frame)

    async def connect(self) -> bool:
        if self._client and self._client.is_connected:
            return True
        self._client = BleakClient(self.address, timeout=15.0)
        await self._client.connect()
        try:
            await self._client._acquire_mtu()
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

    async def _request(self, frame_bytes: bytes, expect_fc: int):
        """Send a built frame; await the matching response frame."""
        if not self.connected:
            raise RuntimeError(f"{self.name}: not connected")

        async with self._lock:
            # Drain any stale notifications before sending
            while not self._inbox.empty():
                self._inbox.get_nowait()

            await self._client.write_gatt_char(self.DATA_CHAR, frame_bytes, response=True)

            try:
                frame = await asyncio.wait_for(self._inbox.get(),
                                               timeout=self.REQUEST_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise TimeoutError(f"{self.name}: no response within {self.REQUEST_TIMEOUT_S}s")

            if not frame.crc_ok:
                raise IOError(f"{self.name}: CRC error in response")
            if frame.function_code != expect_fc:
                raise IOError(
                    f"{self.name}: expected FC 0x{expect_fc:02x}, "
                    f"got 0x{frame.function_code:02x}"
                )
            return frame

    # ---- Modbus-shaped helpers --------------------------------------------

    async def read_registers(self, addr: int, count: int) -> list[int]:
        """Read `count` 16-bit registers starting at `addr`. Returns u16 list."""
        frame = await self._request(build_read_registers(addr, count),
                                    expect_fc=FC_READ_REGISTERS)
        return parse_read_response(frame.payload)

    async def write_register(self, addr: int, value: int) -> None:
        """Write a single 16-bit register."""
        await self._request(build_write_single(addr, value),
                            expect_fc=FC_WRITE_SINGLE)

    async def write_registers(self, addr: int, values: list[int]) -> None:
        """Write multiple consecutive 16-bit registers."""
        await self._request(build_write_multiple(addr, values),
                            expect_fc=FC_WRITE_MULTIPLE)

    async def commit(self) -> None:
        """Tell the controller to persist any pending config writes."""
        await self.write_register(REG_COMMIT, 1)

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
        """Read INPUT_DPPO (279) and set self.decimal_places accordingly.
        Returns the detected decimal-place count. Verify against the panel —
        the protocol doc notes temperature PV may be ×10 regardless of DPPO."""
        try:
            dppo = (await self.read_registers(REG_DPPO, 1))[0]
            if 0 <= dppo <= 3:
                self.decimal_places = dppo
        except Exception as e:
            print(f"[{self.name}] detect_scaling failed: {e}")
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
