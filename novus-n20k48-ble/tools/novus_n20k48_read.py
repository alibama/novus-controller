"""
novus_n20k48_read.py
--------------------
Read setpoint (SP) and process variable (PV) from a Novus N20K48 controller
over the BLE bridge, using the documented Modbus register map:

  Slave ID:        255 (0xFF)
  Register 200:    SP (Setpoint)
  Register 201:    PV (Process Variable)

Tries two variants on the BLE write:
  (a) Full Modbus RTU frame (slave + FC + addr + count + CRC)
  (b) Same frame without the trailing CRC, in case the bridge handles framing

Usage:
    python novus_n20k48_read.py 00:26:A4:XX:XX:XX
"""

import asyncio
import struct
import sys

from bleak import BleakClient

NOVUS_CHAR = "0783b03e-8535-b5a0-7140-a304d2495cba"

SLAVE_ID = 0xFF       # documented N20K48 Modbus address
REG_SP = 200          # 0x00C8
REG_PV = 201          # 0x00C9


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc.to_bytes(2, "little")


def build_read(slave: int, addr: int, count: int) -> bytes:
    payload = struct.pack(">BBHH", slave, 0x03, addr, count)
    return payload + crc16_modbus(payload)


def parse_modbus_03(data: bytes):
    if len(data) < 5:
        return None
    if data[1] == 0x03 and len(data) >= 3 + data[2] + 2:
        bc = data[2]
        body = data[3:3 + bc]
        regs = [int.from_bytes(body[i:i + 2], "big") for i in range(0, bc, 2)]
        return regs
    return None


class Buf:
    def __init__(self):
        self.chunks = []
        self.last_at = None

    def on_notify(self, _sender, data):
        self.chunks.append(bytes(data))
        self.last_at = asyncio.get_event_loop().time()

    async def collect(self, settle_ms=300, max_wait_s=1.5):
        deadline = asyncio.get_event_loop().time() + max_wait_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if self.last_at is None:
                continue
            quiet = (asyncio.get_event_loop().time() - self.last_at) * 1000
            if quiet >= settle_ms:
                break
        out = b"".join(self.chunks)
        self.chunks.clear()
        self.last_at = None
        return out


async def go(address: str):
    buf = Buf()

    # Three queries to send, in order:
    queries = [
        ("SP+PV (regs 200-201) with CRC",
         build_read(SLAVE_ID, REG_SP, 2)),
        ("SP+PV (regs 200-201) WITHOUT CRC",
         build_read(SLAVE_ID, REG_SP, 2)[:-2]),
        # Sanity check: re-run the original reg=0 query but with slave 0xFF
        # to see how the bridge responds with the documented slave ID.
        ("reg 0 with slave 255 (sanity)",
         build_read(SLAVE_ID, 0x0000, 1)),
    ]

    async with BleakClient(address, timeout=20.0) as client:
        try:
            await client._acquire_mtu()
        except Exception:
            pass
        print(f"✓ connected, MTU={client.mtu_size}\n")

        await client.start_notify(NOVUS_CHAR, buf.on_notify)

        for label, frame in queries:
            print(f"── {label}")
            print(f"   tx ({len(frame)}B): {frame.hex(' ')}")
            try:
                await client.write_gatt_char(NOVUS_CHAR, frame, response=True)
            except Exception as e:
                print(f"   write failed: {e}\n")
                continue
            resp = await buf.collect()
            if not resp:
                print("   (no response)\n")
                continue
            print(f"   rx ({len(resp)}B): {resp.hex(' ')}")

            # Try parsing as standard Modbus RTU response
            parsed = parse_modbus_03(resp)
            if parsed is not None:
                print(f"   ★ Modbus parse: registers = {parsed}")
                if "SP+PV" in label and len(parsed) >= 2:
                    sp, pv = parsed[0], parsed[1]
                    sp_signed = sp if sp < 32768 else sp - 65536
                    pv_signed = pv if pv < 32768 else pv - 65536
                    print(f"     interpretation:")
                    print(f"       SP raw=0x{sp:04x} ({sp_signed}) "
                          f"→ try ÷10 = {sp_signed/10:.1f}, ÷1 = {sp_signed}")
                    print(f"       PV raw=0x{pv:04x} ({pv_signed}) "
                          f"→ try ÷10 = {pv_signed/10:.1f}, ÷1 = {pv_signed}")
            else:
                print("   (not a standard Modbus response — bridge wraps it)")
            print()

        await client.stop_notify(NOVUS_CHAR)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python novus_n20k48_read.py <BT_ADDRESS>")
        sys.exit(1)
    asyncio.run(go(sys.argv[1]))
