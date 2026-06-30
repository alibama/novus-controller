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
Register map — from the OFFICIAL Novus protocol document
("N20K48 MODULAR CONTROLLER COMMUNICATION PROTOCOL V1.0x A")
────────────────────────────────────────────────────────────────────────

The BLE register addresses are identical to the RS-485 Modbus map; we
confirmed this against 200/201/202, which match the doc exactly. Earlier
reverse-engineered guesses for 213/214/247 were WRONG and are corrected
here.

  51   PROTECTION    Password protection level (1..4)
  53   OPEN_SESSION  Write password here to unlock config writes (0..65535)
  77   AI_UNIT       Temperature unit: 0 = °C, 1 = °F
  200  CTRL_SP       Main setpoint
  201  CTRL_PV1      Process variable (for temperature, value is ×10 — see note)
  202  CTRL_MV1      Output power, 0..1000 = 0.0..100.0 %
  213  CTRL_AUTO     Control mode: 0 = manual, 1 = automatic
  214  CTRL_RUN      Run: 0 = stopped, 1 = running
  219  CTRL_HYST     ON/OFF control hysteresis
  220  CTRL_SPLL     Setpoint lower limit
  221  CTRL_SPHL     Setpoint upper limit
  224  CTRL_OULL     Output lower limit
  225  CTRL_OUHL     Output upper limit
  226  CTRL_SFST     Soft-start time (s)
  247  RS_PRN_EXEC   Ramp&Soak program being executed (0..20) ← SELECT here
  248  RS_PRN_EDIT   Ramp&Soak program to view/edit (0..20)
  249  RS_SEG        Current program segment (0..20)
  250  RS_SEG_TIME   Current segment elapsed time (s)
  252  RS_TBAS       R&S time base: 0 = seconds, 1 = minutes
  254  RS_PROG_TYPE  0 = none, 1 = ramp-to-soak, 2 = R&S program
  257  TUNE_AUTO     Autotune mode (0..5)
  258  TUNE_PB       Proportional band
  259  TUNE_IR       Integral rate (repetitions/min)
  260  TUNE_DT       Derivative time (s)
  261  TUNE_CT       PWM cycle period (s)
  279  INPUT_DPPO    Decimal point: 0=XXXX, 1=XXX.X, 2=XX.XX, 3=X.XXX

NOTE on scaling: the protocol doc states that for temperature inputs the
PV register is always ×10 regardless of INPUT_DPPO. In practice, read
INPUT_DPPO (279) and AI_UNIT (77) and reconcile against the front panel
to be certain. The client exposes `decimal_places` as a manual override.

────────────────────────────────────────────────────────────────────────
Program table layout (each program is a 40-register block)
────────────────────────────────────────────────────────────────────────

  base(N) = 400 + (N-1) * 40       for program N in 1..20

  base + 0   PTOL    Program tolerance (guaranteed-soak band). The segment
                     timer only advances while |PV - SP| <= PTOL. 0 often
                     means "no hold-back" (timer runs on the clock).
  base + 1   LP      Link to another program (0 = none, 1..20)
  base + 2   PSP0    Setpoint 0 — the program's starting setpoint
  then 9 segments, 3 registers each, for k = 1..9:
    base + 3*k       PT(k)   Time of segment k    (minutes if RS_TBAS=1)
    base + 3*k + 1   PE(k)   Event of segment k   (0..15, digital outputs)
    base + 3*k + 2   PSP(k)  Setpoint of segment k

  Program N therefore occupies base..base+29; base+30..+39 are reserved.
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

# ---- Register addresses (official N20K48 protocol V1.0x) ----
# Session / protection
REG_PROTECTION    = 51     # password protection level (1..4)
REG_OPEN_SESSION  = 53     # write password to unlock config writes
REG_UNIT          = 77     # 0 = °C, 1 = °F
# Operation cycle
REG_SETPOINT      = 200    # CTRL_SP
REG_PV            = 201    # CTRL_PV1  (temperature: value is ×10 — verify)
REG_OUTPUT_PCT    = 202    # CTRL_MV1  (0..1000 = 0..100.0%)
REG_CTRL_AUTO     = 213    # 0 = manual, 1 = automatic
REG_CTRL_RUN      = 214    # 0 = stopped, 1 = running
REG_HYST          = 219
REG_SPLL          = 220    # setpoint lower limit
REG_SPHL          = 221    # setpoint upper limit
REG_OULL          = 224    # output lower limit
REG_OUHL          = 225    # output upper limit
REG_SOFT_START    = 226    # soft-start time (s)
# Ramp & Soak engine
REG_RS_PRN_EXEC   = 247    # program being executed (0..20)  <-- program SELECT
REG_RS_PRN_EDIT   = 248    # program to view/edit (0..20)
REG_RS_SEG        = 249    # current segment (0..20)
REG_RS_SEG_TIME   = 250    # elapsed time in current segment (s)
REG_RS_TBASE      = 252    # 0 = seconds, 1 = minutes
REG_RS_PROG_TYPE  = 254    # 0 = none, 1 = ramp-to-soak, 2 = R&S program
# Tuning
REG_TUNE_AUTO     = 257
REG_TUNE_PB       = 258    # proportional band
REG_TUNE_IR       = 259    # integral rate (rep/min)
REG_TUNE_DT       = 260    # derivative time (s)
REG_TUNE_CT       = 261    # PWM cycle (s)
# Input
REG_DPPO          = 279    # decimal point: 0=XXXX,1=XXX.X,2=XX.XX,3=X.XXX

# Program table
RS_PROG_BASE      = 400    # program 1 tolerance lives here
RS_PROG_STRIDE    = 40     # each program block is 40 registers wide
RS_SEGMENTS       = 9      # segments per program


def program_base(n: int) -> int:
    """First register (tolerance) of program n (1..20)."""
    if not 1 <= n <= 20:
        raise ValueError(f"program must be 1..20, got {n}")
    return RS_PROG_BASE + (n - 1) * RS_PROG_STRIDE


# Backwards-compat aliases (older code referenced these names)
REG_COMMIT          = REG_OPEN_SESSION   # 53; note: this is OPEN_SESSION, not a commit
REG_PROGRAM_EXEC    = REG_RS_PRN_EXEC    # 247 (was wrongly 214)
REG_PROGRAM_TO_VIEW = REG_RS_PRN_EDIT    # 248 (was wrongly 213)
REG_CURRENT_SEGMENT = REG_RS_SEG         # 249 (was wrongly 215)


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
