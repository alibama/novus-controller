"""
devices.py
==========
Persistent device registry: which controllers exist, what they're called,
and whether each is a FURNACE (temperature-critical, watched/notified) or a
KILN (annealer, run programs). Stored as JSON on the box so the Settings
page can add discovered devices and assign roles without hand-editing code.

A kiln can be flagged can_be_furnace=True to allow temporarily treating it
as a furnace (e.g. for tests) from the UI.

Device-list changes take effect on the next service restart — we don't
hot-swap the live monitor while a furnace is running.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

DEVICES_PATH = Path(__file__).parent / "devices.json"

ROLE_FURNACE = "furnace"
ROLE_KILN = "kiln"


@dataclass
class Device:
    name: str
    address: str
    role: str = ROLE_KILN          # "furnace" or "kiln"
    can_be_furnace: bool = False   # kiln/annealer that may double as a furnace
    expected_setpoint: float = 2100.0  # used by the watchdog when role==furnace
    power_kw: float = 0.0           # rated element power at 100% output (kW);
                                    # used to estimate energy/cost. 0 = unknown.

    @property
    def is_furnace(self) -> bool:
        return self.role == ROLE_FURNACE


def _seed_from_kiln_config() -> list[Device]:
    """First run: build the registry from the legacy KILNS list."""
    try:
        from kiln_config import KILNS
    except Exception:
        KILNS = []
    out = []
    for name, addr in KILNS:
        if name.lower() == "furnace":
            out.append(Device(name=name, address=addr, role=ROLE_FURNACE,
                              expected_setpoint=2100.0))
        else:
            out.append(Device(name=name, address=addr, role=ROLE_KILN,
                              can_be_furnace=True, expected_setpoint=896.0))
    return out


def load_devices() -> list[Device]:
    if DEVICES_PATH.exists():
        try:
            raw = json.loads(DEVICES_PATH.read_text())
            return [Device(**d) for d in raw]
        except Exception as e:
            print(f"[devices] failed to read {DEVICES_PATH}: {e}; reseeding")
    devs = _seed_from_kiln_config()
    save_devices(devs)
    return devs


def save_devices(devices: list[Device]) -> None:
    DEVICES_PATH.write_text(json.dumps([asdict(d) for d in devices], indent=2))


def furnaces(devices: list[Device]) -> list[Device]:
    return [d for d in devices if d.role == ROLE_FURNACE]


def kilns(devices: list[Device]) -> list[Device]:
    return [d for d in devices if d.role == ROLE_KILN]
