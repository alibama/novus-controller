"""
gatt_enumerate.py
-----------------
Connect to a BLE device and dump its GATT tree (services, characteristics,
descriptors, and the values of anything readable).

Usage:
    python gatt_enumerate.py 00:26:A4:00:5F:4E

Run from inside your venv:
    ~/kiln-bt-scanner/.venv/bin/python gatt_enumerate.py <ADDRESS>

IMPORTANT: Disconnect QuickTune Mobile on your phone (or just turn the phone's
Bluetooth off) before running this. BLE peripherals only allow one active
central connection at a time.
"""

import asyncio
import sys

from bleak import BleakClient, BleakError

# ---------------------------------------------------------------------------
# Annotations for well-known UUIDs — helps spot familiar services at a glance
# ---------------------------------------------------------------------------
KNOWN_UUIDS = {
    # Standard GATT
    "00001800-0000-1000-8000-00805f9b34fb": "Generic Access",
    "00001801-0000-1000-8000-00805f9b34fb": "Generic Attribute",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery Service",
    "00002a00-0000-1000-8000-00805f9b34fb": "Device Name",
    "00002a01-0000-1000-8000-00805f9b34fb": "Appearance",
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
    # Nordic UART Service — the canonical BLE serial-bridge
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e": "*** Nordic UART Service (NUS) ***",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e": "    NUS RX  (you write here)",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e": "    NUS TX  (subscribe for data from device)",
}


def annotate(uuid: str) -> str:
    return KNOWN_UUIDS.get(uuid.lower(), "")


def prop_flags(char) -> str:
    """Compact property string: R=read W=write w=write-no-resp N=notify I=indicate"""
    p = char.properties
    out = ""
    out += "R" if "read" in p else "-"
    out += "W" if "write" in p else "-"
    out += "w" if "write-without-response" in p else "-"
    out += "N" if "notify" in p else "-"
    out += "I" if "indicate" in p else "-"
    return out


def pretty_value(raw: bytes) -> str:
    """Show hex always; show ASCII too if it's printable."""
    hex_str = raw.hex(" ")
    text = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    if text and all(32 <= ord(c) < 127 or c in "\r\n\t" for c in text):
        return f"hex={hex_str!s}  ascii={text!r}"
    return f"hex={hex_str!s}"


async def enumerate_device(address: str) -> None:
    print(f"→ Connecting to {address} (timeout 20s)…")
    async with BleakClient(address, timeout=20.0) as client:
        print(f"✓ Connected.  MTU={client.mtu_size}\n")

        for service in client.services:
            label = annotate(service.uuid)
            print(f"┌── Service  {service.uuid}  {label}")

            for char in service.characteristics:
                clabel = annotate(char.uuid)
                print(f"│   ├── Char  {char.uuid}  [{prop_flags(char)}]  {clabel}")

                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        print(f"│   │     value: {pretty_value(val)}")
                    except Exception as e:
                        print(f"│   │     (read failed: {e})")

                for desc in char.descriptors:
                    print(f"│   │     descriptor {desc.uuid}")

            print("│")
        print("└── done")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python gatt_enumerate.py <BT_ADDRESS>")
        print("Example: python gatt_enumerate.py 00:26:A4:00:5F:4E")
        sys.exit(1)

    address = sys.argv[1]
    try:
        asyncio.run(enumerate_device(address))
    except BleakError as e:
        print(f"\n✗ BLE error: {e}")
        print("  Common causes:")
        print("  - Phone is still connected to this device (turn off phone BT)")
        print("  - Address typo")
        print("  - Device went out of range or powered off")
        sys.exit(2)
    except asyncio.TimeoutError:
        print(f"\n✗ Connection timed out — device not reachable.")
        sys.exit(2)


if __name__ == "__main__":
    main()
