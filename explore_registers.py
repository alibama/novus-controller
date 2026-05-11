"""
explore_registers.py
====================
Read-only diagnostic. Connects to bubba, reads the suspected
program-state register region and the "high" config block at 2400+,
and prints everything in human-friendly form. No writes — safe to run
while the kiln is firing.

Usage:
    python explore_registers.py 00:26:A4:00:5F:4E
"""
import asyncio
import sys
from novus_client import NovusClient


async def main(address: str):
    c = NovusClient("bubba", address)
    await c.connect()
    print(f"connected={c.connected}\n")

    # 1. Operation cycle (200..204): SP, PV, output, ?, ?
    print("--- Operation cycle (200..209) ---")
    regs = await c.read_registers(200, 10)
    labels = {
        200: "SETPOINT",
        201: "PROCESS_VARIABLE",
        202: "OUTPUT (0..1000=0..100%)",
        203: "?",
        204: "?",
    }
    for i, v in enumerate(regs):
        addr = 200 + i
        s = v if v < 32768 else v - 65536
        lbl = labels.get(addr, "")
        print(f"  reg {addr:4d}: {v:6d} (signed: {s:6d})  {lbl}")

    # 2. The block where R&S execution registers should live (210..229)
    print("\n--- Suspected R&S program-state region (210..229) ---")
    regs = await c.read_registers(210, 20)
    for i, v in enumerate(regs):
        addr = 210 + i
        s = v if v < 32768 else v - 65536
        marker = ""
        if addr == 213: marker = "  <-- HYPOTHESIS: program being executed (0=none, 1..20=running)"
        if addr == 214: marker = "  <-- HYPOTHESIS: program selected for viewing/edit"
        if addr == 215: marker = "  <-- HYPOTHESIS: current program segment"
        print(f"  reg {addr:4d}: {v:6d} (signed: {s:6d}){marker}")

    # 3. The 2400 block (we saw [1, 1025, 256, 0, ...])
    print("\n--- High config block (2400..2419) ---")
    regs = await c.read_registers(2400, 20)
    for i, v in enumerate(regs):
        addr = 2400 + i
        s = v if v < 32768 else v - 65536
        print(f"  reg {addr:4d}: {v:6d} (signed: {s:6d})")

    await c.disconnect()
    print("\nDone. Compare values against what the kiln front panel shows.")
    print("If reg 213 shows the program number you have running, we found it.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python explore_registers.py <BT_ADDRESS>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
