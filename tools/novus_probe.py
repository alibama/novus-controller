"""
novus_probe.py
--------------
Probe a Novus controller over BLE to confirm it speaks Modbus RTU on its
single write+notify characteristic.

Strategy:
  1. Connect, subscribe to notifications on the data characteristic
  2. Send Modbus "read holding register" queries (function 0x03) — read-only,
     safe to issue while a kiln is firing
  3. Buffer notification chunks and parse the response

Usage:
    python novus_probe.py <ADDRESS> [slave_id]
    python novus_probe.py 00:26:A4:XX:XX:XX
    python novus_probe.py 00:26:A4:XX:XX:XX 1

Phone Bluetooth must be OFF (only one BLE central at a time).
"""

import asyncio
import struct
import sys

from bleak import BleakClient

# The single write+notify characteristic discovered via gatt_enumerate.py
NOVUS_CHAR = "0783b03e-8535-b5a0-7140-a304d2495cba"

# Registers to probe. On most Novus controllers, register 0x00 is the
# Process Variable (current temperature) and 0x01 is the Setpoint, but
# this varies by model. We're just looking for *any* sensible response.
PROBE_REGISTERS = (0x0000, 0x0001, 0x0064)

# Slave IDs to try if the user doesn't specify one.
# 1 is the Novus default; 0 is broadcast; 247 is sometimes used.
DEFAULT_SLAVES = (1, 0, 247)


# ---------------------------------------------------------------------------
# Modbus RTU framing
# ---------------------------------------------------------------------------
def crc16_modbus(data: bytes) -> bytes:
    """Modbus RTU CRC-16, returned little-endian (the on-wire order)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc.to_bytes(2, "little")


def build_read_holding(slave_id: int, address: int, count: int = 1) -> bytes:
    """Function 0x03: Read N holding registers starting at `address`."""
    payload = struct.pack(">BBHH", slave_id, 0x03, address, count)
    return payload + crc16_modbus(payload)


def parse_modbus_response(data: bytes) -> str:
    """Best-effort interpretation of a Modbus RTU response."""
    if len(data) < 4:
        return f"too short to be Modbus ({len(data)} B): {data.hex(' ')}"

    slave, func = data[0], data[1]

    # Modbus exception responses set the high bit of the function code
    if func & 0x80:
        ec = data[2] if len(data) > 2 else 0
        names = {
            0x01: "ILLEGAL_FUNCTION",
            0x02: "ILLEGAL_DATA_ADDRESS",
            0x03: "ILLEGAL_DATA_VALUE",
            0x04: "SLAVE_DEVICE_FAILURE",
            0x06: "SLAVE_DEVICE_BUSY",
        }
        return (
            f"slave={slave} EXCEPTION (orig_func=0x{func & 0x7F:02x}) "
            f"code=0x{ec:02x} ({names.get(ec, '?')})"
        )

    if func == 0x03 and len(data) >= 5:
        byte_count = data[2]
        body = data[3:3 + byte_count]
        regs = [
            int.from_bytes(body[i:i + 2], "big")
            for i in range(0, len(body), 2)
        ]
        signed = [r if r < 32768 else r - 65536 for r in regs]
        return (
            f"slave={slave} func=0x03 bytes={byte_count} "
            f"regs(u16)={regs}  regs(s16)={signed}"
        )

    return f"slave={slave} func=0x{func:02x} payload={data[2:].hex(' ')}"


# ---------------------------------------------------------------------------
# Notification buffer
# ---------------------------------------------------------------------------
class ResponseBuffer:
    """Collect notification chunks until quiet for a moment, then return all."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self.last_chunk_at: float | None = None

    def on_notify(self, sender, data: bytearray) -> None:
        self.chunks.append(bytes(data))
        self.last_chunk_at = asyncio.get_event_loop().time()

    async def collect(self, settle_ms: int = 300, max_wait_s: float = 1.5) -> bytes:
        """Wait for notifications to stop arriving, then return all bytes."""
        deadline = asyncio.get_event_loop().time() + max_wait_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if self.last_chunk_at is None:
                continue
            quiet_for = (asyncio.get_event_loop().time() - self.last_chunk_at) * 1000
            if quiet_for >= settle_ms:
                break
        out = b"".join(self.chunks)
        self.chunks.clear()
        self.last_chunk_at = None
        return out


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------
async def probe(address: str, slave_ids) -> None:
    buf = ResponseBuffer()

    async with BleakClient(address, timeout=20.0) as client:
        try:
            mtu = await client._acquire_mtu()
        except Exception:
            mtu = client.mtu_size
        print(f"✓ connected to {address}  MTU={mtu}")

        await client.start_notify(NOVUS_CHAR, buf.on_notify)
        print(f"subscribed to notifications on {NOVUS_CHAR}\n")

        any_response = False
        for slave in slave_ids:
            for reg in PROBE_REGISTERS:
                frame = build_read_holding(slave, reg, count=1)
                print(f"→ slave={slave:>3} reg=0x{reg:04x}  tx: {frame.hex(' ')}")
                try:
                    await client.write_gatt_char(NOVUS_CHAR, frame, response=True)
                except Exception as e:
                    print(f"   write failed: {e}\n")
                    continue

                resp = await buf.collect()
                if resp:
                    any_response = True
                    print(f"   rx ({len(resp)}B): {resp.hex(' ')}")
                    print(f"   → {parse_modbus_response(resp)}")
                else:
                    print("   (no response)")
                print()

        await client.stop_notify(NOVUS_CHAR)

        if not any_response:
            print(
                "Nothing came back on any (slave, register) combination.\n"
                "Next steps to consider:\n"
                "  - Confirm the controller's Modbus address from its front-panel menu\n"
                "  - The bridge may strip slave/CRC and pass only function+data —\n"
                "    if so we'd need to rework the frame format\n"
                "  - Capture QuickTune Mobile's traffic via Android HCI snoop log\n"
                "    to see exactly what bytes it sends"
            )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python novus_probe.py <BT_ADDRESS> [slave_id]")
        sys.exit(1)

    address = sys.argv[1]
    slaves = [int(sys.argv[2])] if len(sys.argv) > 2 else list(DEFAULT_SLAVES)

    try:
        asyncio.run(probe(address, slaves))
    except Exception as e:
        print(f"\n✗ {type(e).__name__}: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
