"""
novus_protocol.py
=================
Wire-format protocol used by the Novus N20K48 BLE module (and presumably
other Bluetooth-capable Novus controllers). Reverse-engineered from
captured QuickTune Mobile traffic.

This module is pure protocol: frame building, parsing, and CRC. It does
no I/O. The BLE transport lives in `novus_client.py`.

────────────────────────────────────────────────────────────────────────
Frame format
────────────────────────────────────────────────────────────────────────

    ┌─────────────────┬──────┬──────────────┬──────────┐
    │ 5a 00 00 00     │ LEN  │ <payload>    │ CRC16-LE │
    │ (magic, 4 B)    │ (1B) │ (LEN bytes)  │ (2 B)    │
    └─────────────────┴──────┴──────────────┴──────────┘

  - LEN counts payload bytes only (excludes magic, LEN itself, and CRC).
  - CRC is Modbus CRC-16 (poly 0xA001, init 0xFFFF) over the magic +
    LEN + payload, transmitted little-endian.

Payload always begins with 0x01 (the bridge's internal slave address)
followed by a function code.

────────────────────────────────────────────────────────────────────────
Function codes (Modbus-like, but in the user-defined range 0x41–0x48)
────────────────────────────────────────────────────────────────────────

  0x46  READ_REGISTERS         (analog of Modbus FC 0x03)
        request:   01 46 <addr_be:2> <count_be:2>
        response:  01 46 <byte_count:1> <data:bc bytes>

  0x47  WRITE_SINGLE_REGISTER  (analog of Modbus FC 0x06)
        request:   01 47 <addr_be:2> <value_be:2>
        response:  01 47 <addr_be:2> <value_be:2>      (echoed)

  0x48  WRITE_MULTIPLE_REGS    (analog of Modbus FC 0x10)
        request:   01 48 <addr_be:2> <count_be:2> <bc:1> <data:bc>
        response:  01 48 <addr_be:2> <count_be:2>      (no data)

  0x2b 0x0e  READ_DEVICE_ID    (standard Modbus MEI Type 14)
        request:   01 2b 0e <read_id_code:1> <object_id:1>

────────────────────────────────────────────────────────────────────────
Verified registers (from trace + IoThrifty docs + Novus protocol PDF)
────────────────────────────────────────────────────────────────────────

  53   (0x0035)  COMMIT              — write 1 to commit pending config
  200  (0x00C8)  SETPOINT            (signed int16, scaled by decimal-point)
  201  (0x00C9)  PROCESS_VARIABLE    (signed int16, scaled by decimal-point)
  202  (0x00CA)  CONTROL_OUTPUT      (0..1000 = 0.0..100.0%)
  208  (0x00D0)  USER_MANUAL_SP      (preserved across mode changes)
  213  (0x00D5)  PROGRAM_TO_VIEW     (which program QuickTune is editing)
  214  (0x00D6)  PROGRAM_EXECUTING   (0 = stopped; 1..20 = running that program)
  215  (0x00D7)  CURRENT_SEGMENT     (segment index within executing program)

Verified by before/after probe of bubba: starting a program in QuickTune
flipped reg 214 from 0 → 1 and reg 202 from 0 → 1000 (heater on).

Program data lives in the 500–1100 range (programs 1..N, segments
of 3 registers each: SP, duration_minutes, event) and 2600+
(program metadata). Decimal-point setting is its own register in
the 0..50 config block; default to 0 unless your panel reads
fractional degrees.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC = b"\x5a\x00\x00\x00"
SLAVE = 0x01

FC_READ_REGISTERS       = 0x46
FC_WRITE_SINGLE         = 0x47
FC_WRITE_MULTIPLE       = 0x48
FC_READ_DEVICE_ID       = 0x2B    # followed by sub-FC 0x0E (MEI Type 14)
MEI_READ_DEVICE_ID      = 0x0E

# Verified register addresses
REG_COMMIT          = 53
REG_SETPOINT        = 200
REG_PV              = 201
REG_OUTPUT_PCT      = 202
REG_USER_MANUAL_SP  = 208
REG_PROGRAM_TO_VIEW = 213
REG_PROGRAM_EXEC    = 214   # 0 = stopped, 1..20 = running that program
REG_CURRENT_SEGMENT = 215


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def crc16_modbus(data: bytes) -> int:
    """Standard Modbus CRC-16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def _wrap(payload: bytes) -> bytes:
    if len(payload) > 255:
        raise ValueError(f"payload too long: {len(payload)} bytes (max 255)")
    body = MAGIC + bytes([len(payload)]) + payload
    return body + crc16_modbus(body).to_bytes(2, "little")


def build_read_registers(addr: int, count: int) -> bytes:
    """Build a FC 0x46 read request for `count` 16-bit registers at `addr`."""
    if not 0 <= addr <= 0xFFFF:
        raise ValueError(f"addr out of range: {addr}")
    if not 1 <= count <= 125:
        raise ValueError(f"count out of range: {count}")
    return _wrap(bytes([SLAVE, FC_READ_REGISTERS])
                 + addr.to_bytes(2, "big")
                 + count.to_bytes(2, "big"))


def build_write_single(addr: int, value: int) -> bytes:
    """Build a FC 0x47 write-single-register request."""
    if not 0 <= addr <= 0xFFFF:
        raise ValueError(f"addr out of range: {addr}")
    # value can be signed or unsigned int16
    if -32768 <= value < 0:
        value &= 0xFFFF
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"value out of range: {value}")
    return _wrap(bytes([SLAVE, FC_WRITE_SINGLE])
                 + addr.to_bytes(2, "big")
                 + value.to_bytes(2, "big"))


def build_write_multiple(addr: int, values: list[int]) -> bytes:
    """Build a FC 0x48 write-multiple-registers request."""
    if not values or len(values) > 123:
        raise ValueError(f"values length out of range: {len(values)}")
    data = b"".join(
        ((v & 0xFFFF) if v < 0 else v).to_bytes(2, "big") for v in values
    )
    return _wrap(bytes([SLAVE, FC_WRITE_MULTIPLE])
                 + addr.to_bytes(2, "big")
                 + len(values).to_bytes(2, "big")
                 + bytes([len(data)])
                 + data)


def build_read_device_id(read_id_code: int = 0x01, object_id: int = 0x00) -> bytes:
    """Build a Modbus 'Read Device Identification' (MEI Type 14) request."""
    return _wrap(bytes([SLAVE, FC_READ_DEVICE_ID, MEI_READ_DEVICE_ID,
                        read_id_code, object_id]))


# ---------------------------------------------------------------------------
# Frame parser
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    payload: bytes
    crc_ok: bool

    @property
    def slave(self) -> int:
        return self.payload[0] if self.payload else 0

    @property
    def function_code(self) -> int:
        return self.payload[1] if len(self.payload) >= 2 else 0


def parse_frame(raw: bytes) -> Optional[Frame]:
    """Parse an incoming frame. Returns None if obviously malformed."""
    if len(raw) < 9 or raw[:4] != MAGIC:
        return None
    length = raw[4]
    if len(raw) != 4 + 1 + length + 2:
        return None
    payload = raw[5:5 + length]
    crc_rx = struct.unpack("<H", raw[5 + length:])[0]
    crc_ok = crc_rx == crc16_modbus(raw[:5 + length])
    return Frame(payload=payload, crc_ok=crc_ok)


def parse_read_response(payload: bytes) -> list[int]:
    """Parse the payload of a FC 0x46 response into a list of u16 register values."""
    if len(payload) < 3 or payload[1] != FC_READ_REGISTERS:
        raise ValueError(f"not a read response: {payload.hex(' ')}")
    bc = payload[2]
    data = payload[3:3 + bc]
    if len(data) != bc:
        raise ValueError(f"truncated read response: bc={bc} got {len(data)}")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, bc, 2)]


def to_signed(u16: int) -> int:
    return u16 if u16 < 0x8000 else u16 - 0x10000


# ---------------------------------------------------------------------------
# Streaming reader (for BLE notifications that may arrive in fragments)
# ---------------------------------------------------------------------------

class FrameAssembler:
    """
    Accumulates notification fragments and yields complete frames as they
    arrive. BLE notifications are MTU-bounded (typically 20 B with default
    MTU); large responses span multiple notifications.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf.extend(chunk)
        out = []
        while True:
            if len(self._buf) < 5:
                break  # not even the magic + length yet
            if bytes(self._buf[:4]) != MAGIC:
                # resync: drop one byte at a time until we find magic or run out
                idx = self._buf.find(MAGIC)
                if idx < 0:
                    self._buf.clear()
                    break
                del self._buf[:idx]
                continue
            length = self._buf[4]
            total = 4 + 1 + length + 2
            if len(self._buf) < total:
                break
            raw = bytes(self._buf[:total])
            del self._buf[:total]
            f = parse_frame(raw)
            if f is not None:
                out.append(f)
        return out
