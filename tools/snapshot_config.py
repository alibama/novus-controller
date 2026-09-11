#!/usr/bin/env python3
"""
snapshot_config.py — capture a controller's full config from the command line.

    python tools/snapshot_config.py <name> <BLE_ADDRESS> [--out DIR]

Reads all 20 program tables and the key config registers with multi-read
verification, then writes two files into DIR/<name>/ (default ./config_snapshots):
a canonical JSON with a SHA-256 fingerprint and a human-readable .txt.

Good for a nightly cron backup or a before/after check around a config change.
Diff two JSON files later with the app's Settings page, or eyeball the .txt.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import json

import config_snapshot
from novus_client import NovusClient


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="short name, e.g. furnace")
    ap.add_argument("address", help="BLE address, e.g. 00:26:A4:XX:XX:XX")
    ap.add_argument("--out", default="config_snapshots", help="output directory")
    args = ap.parse_args()

    c = NovusClient(args.name, args.address)
    await c.connect()
    try:
        await c.detect_scaling()
        print(f"[{args.name}] reading config + 20 programs (verified)…")
        snap = await config_snapshot.read_snapshot(c)
    finally:
        await c.disconnect()

    ts = snap["meta"]["captured_utc"].replace(":", "").replace("-", "")
    d = pathlib.Path(args.out) / args.name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ts}.json").write_text(json.dumps(snap, indent=2, sort_keys=True))
    (d / f"{ts}.txt").write_text(config_snapshot.to_human(snap))

    print(f"[{args.name}] SHA-256: {snap['meta']['sha256']}")
    if snap["meta"]["unstable_reads"]:
        print(f"[{args.name}] UNSTABLE: {snap['meta']['unstable_reads']}")
    print(f"[{args.name}] wrote {d}/{ts}.json and .txt")


if __name__ == "__main__":
    asyncio.run(main())
