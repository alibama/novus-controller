"""
devices.py
==========
Persistent device registry: which controllers exist, what they're called,
what model they are, and what job they do:

  * FURNACE  — temperature-critical, kept hot, watched and auto-recovered
  * KILN     — runs firing programs (fuse, slump, anneal …)
  * OTHER    — a controller you just want to see/log (monitor only)

Stored as JSON (devices.json) next to the code, or under $KILN_DATA_DIR when
that is set, so the Devices page can add discovered controllers and change
roles without hand-editing code. Nothing here assumes a particular shop: an
empty registry is a valid starting point and the UI walks you through a scan.

Each device also records the controller `model` (e.g. "N20K48" — the PID
Ramp & Soak controller this app is verified on). Unverified models default to
read-only (`writes_allowed=False`) until you opt in.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, fields

import novus_models
import paths

DEVICES_PATH = paths.DEVICES_PATH

ROLE_FURNACE = "furnace"
ROLE_KILN = "kiln"
ROLE_OTHER = "other"
ROLES = (ROLE_KILN, ROLE_FURNACE, ROLE_OTHER)

DEFAULT_MODEL = "N20K48"
DEFAULT_SETPOINT = {ROLE_FURNACE: 2100.0, ROLE_KILN: 896.0, ROLE_OTHER: 0.0}


@dataclass
class Device:
    name: str
    address: str
    role: str = ROLE_KILN          # "furnace", "kiln" or "other"
    can_be_furnace: bool = False   # kiln/annealer that may double as a furnace
    expected_setpoint: float = 896.0  # hold target used by the watchdog
    power_kw: float = 0.0           # rated element power at 100% output (kW);
                                    # used to estimate energy/cost. 0 = unknown.
    model: str = DEFAULT_MODEL      # controller model, e.g. "N20K48"
    firmware: str = ""              # firmware revision read from the device
    product: str = ""               # full product code string from the device
    writes_allowed: bool = True     # False = this app will only READ from it

    @property
    def is_furnace(self) -> bool:
        return self.role == ROLE_FURNACE

    @property
    def model_info(self) -> novus_models.ModelInfo:
        return novus_models.info(self.model)

    @property
    def verified(self) -> bool:
        return self.model_info.verified

    def label(self) -> str:
        return f"{self.name} · {novus_models.describe(self.model)}"


_FIELDS = {f.name for f in fields(Device)}


def _from_dict(d: dict) -> Device:
    """Tolerate unknown keys (older/newer files) and fill defaults."""
    clean = {k: v for k, v in d.items() if k in _FIELDS}
    dev = Device(**clean)
    if dev.role not in ROLES:
        dev.role = ROLE_KILN
    dev.address = (dev.address or "").strip().upper()
    if not dev.model:
        dev.model = DEFAULT_MODEL
    return dev


def _seed_from_kiln_config() -> list[Device]:
    """First run: build the registry from the legacy KILNS list, if present."""
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
            return [_from_dict(d) for d in raw]
        except Exception as e:
            print(f"[devices] failed to read {DEVICES_PATH}: {e}; reseeding")
    devs = _seed_from_kiln_config()
    if devs:
        save_devices(devs)
    return devs


def save_devices(devices: list[Device]) -> None:
    DEVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEVICES_PATH.write_text(json.dumps([asdict(d) for d in devices], indent=2))


def new_device(name: str, address: str, role: str = ROLE_KILN,
               model: str = "", product: str = "", firmware: str = "") -> Device:
    """Build a Device with sensible defaults for its role and model."""
    model = (model or novus_models.model_from_text(product) or DEFAULT_MODEL).upper()
    mi = novus_models.info(model)
    return Device(
        name=name.strip(), address=address.strip().upper(), role=role,
        can_be_furnace=(role == ROLE_KILN),
        expected_setpoint=DEFAULT_SETPOINT.get(role, 896.0),
        model=model, product=product, firmware=firmware,
        writes_allowed=bool(mi.verified),
    )


def furnaces(devices: list[Device]) -> list[Device]:
    return [d for d in devices if d.role == ROLE_FURNACE]


def kilns(devices: list[Device]) -> list[Device]:
    return [d for d in devices if d.role == ROLE_KILN]


def validate(devices: list[Device]) -> list[str]:
    """Problems that would make the registry ambiguous (duplicate names or
    addresses, bad MAC format). Empty list == fine."""
    import re
    problems = []
    names = [d.name for d in devices]
    addrs = [d.address for d in devices]
    for n in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"duplicate name '{n}'")
    for a in sorted({a for a in addrs if addrs.count(a) > 1}):
        problems.append(f"duplicate address {a}")
    for d in devices:
        if not d.name.strip():
            problems.append(f"device {d.address} has no name")
        if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", d.address or ""):
            problems.append(f"'{d.name}' has an invalid BLE address '{d.address}'")
    return problems
