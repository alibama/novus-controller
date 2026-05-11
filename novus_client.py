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
    REG_SETPOINT, REG_PV, REG_OUTPUT_PCT, REG_COMMIT,
    REG_PROGRAM_EXEC, REG_CURRENT_SEGMENT, REG_USER_MANUAL_SP,
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
            regs = await self.read_registers(REG_SETPOINT, 16)   # 200..215
        except Exception as e:
            print(f"[{self.name}] read_state failed: {e}")
            return ControllerState(name=self.name, address=self.address,
                                   connected=False, pv=None, sp=None)

        sp_raw = regs[0]                  # 200
        pv_raw = regs[1]                  # 201
        out_raw = regs[2]                 # 202
        active_program = regs[14]         # 214 (0 = stopped, 1..20 = running)
        active_segment = regs[15]         # 215

        return ControllerState(
            name=self.name,
            address=self.address,
            connected=True,
            pv=round(self._scale(pv_raw), 1),
            sp=round(self._scale(sp_raw), 1),
            output_pct=round(out_raw / 10.0, 1),
            active_program=active_program if active_program > 0 else None,
            active_segment=active_segment if active_program > 0 else None,
            segment_time_left_s=None,    # would need extra reads to compute
        )

    def list_programs(self) -> list[Program]:
        """Return cached program metadata. Currently placeholder until we map
        the program-table register layout from the trace; see protocol notes."""
        if not self._programs:
            for i in range(1, 21):
                self._programs[i] = Program(number=i, name=f"Program {i:02d}")
        return [self._programs[i] for i in range(1, 21)]

    def get_program(self, num: int) -> Optional[Program]:
        if not self._programs:
            self.list_programs()
        return self._programs.get(num)

    async def run_program(self, num: int) -> bool:
        """
        Start Ramp & Soak program `num` (1..20).

        Verified: writing the program number to register 214 starts execution.
        Writing 0 stops it.
        """
        if not 1 <= num <= 20:
            raise ValueError(f"program number must be 1..20, got {num}")
        await self.write_register(REG_PROGRAM_EXEC, num)
        return True

    async def stop_program(self) -> bool:
        """Stop any running program by writing 0 to reg 214."""
        await self.write_register(REG_PROGRAM_EXEC, 0)
        return True

    async def is_running(self) -> int:
        """Returns 0 if stopped, or the program number (1..20) currently executing."""
        regs = await self.read_registers(REG_PROGRAM_EXEC, 1)
        return regs[0]


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
