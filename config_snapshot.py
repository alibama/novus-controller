"""
config_snapshot.py
==================
Capture a controller's full configuration as cleanly as possible, store it in
two forms — a canonical machine-readable JSON and a human-readable text dump —
and make it tamper-evident with a SHA-256 over the canonical data.

Why: these are cheap controllers on a flaky BLE link, and the software writes to
them (recovery restarts, program writes, manual overrides). A snapshot lets you
prove, later, exactly what the config was, and diff two snapshots to confirm
nothing changed that you didn't change on purpose.

"As clean a read as possible": every register and every program table is read
several times and only a value that agrees across reads is trusted. Anything
that won't settle is recorded as UNSTABLE rather than silently accepted, so a
noisy read can never masquerade as a real config change.

The hash covers only the DATA (config + programs), never the timestamp — so an
identical configuration produces an identical hash every time, and any real
change shows up as a hash change.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

import novus_protocol as P

SCHEMA = "novus-config-snapshot/1"

# Config registers worth capturing, in a sensible reading order, with the
# friendly names used in the human-readable dump. (Program tables 1..20 are
# captured separately via read_program.)
CONFIG_REGISTERS: list[tuple[int, str]] = [
    (P.REG_UNIT,        "Temperature unit (0=°C, 1=°F)"),
    (P.REG_DPPO,        "Decimal point (DPPO)"),
    (P.REG_PROTECTION,  "Protection level"),
    (P.REG_SETPOINT,    "Setpoint (SP)"),
    (P.REG_SPLL,        "Setpoint lower limit"),
    (P.REG_SPHL,        "Setpoint upper limit"),
    (P.REG_OULL,        "Output lower limit"),
    (P.REG_OUHL,        "Output upper limit"),
    (P.REG_HYST,        "Hysteresis"),
    (P.REG_SOFT_START,  "Soft-start time (s)"),
    (P.REG_CTRL_AUTO,   "Auto/manual (213)"),
    (P.REG_CTRL_RUN,    "Run/stop (214)"),
    (P.REG_RS_PRN_EXEC, "Program selected to execute (247)"),
    (P.REG_RS_PRN_EDIT, "Program selected to edit (248)"),
    (P.REG_RS_TBASE,    "R&S time base (0=s, 1=min)"),
    (253,               "Power-return / resume mode (253, verify vs manual)"),
    (P.REG_RS_PROG_TYPE,"R&S program type (254)"),
    (P.REG_TUNE_AUTO,   "Autotune (257)"),
    (P.REG_TUNE_PB,     "Proportional band (258)"),
    (P.REG_TUNE_IR,     "Integral rate (259)"),
    (P.REG_TUNE_DT,     "Derivative time (260)"),
    (P.REG_TUNE_CT,     "PWM cycle time (261)"),
]


# --------------------------------------------------------------------------
# Pure helpers (no I/O) — these are the unit-tested core.
# --------------------------------------------------------------------------

def _canonical(data: dict) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def canonical_hash(data: dict) -> str:
    """SHA-256 over the canonical form of the DATA section only."""
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def build_snapshot(device_name: str, address: str, decimal_places: int,
                   config: dict, programs: dict,
                   unstable: Optional[list] = None) -> dict:
    """Wrap raw readings into a self-describing, hashed snapshot document.

    `config`  : {register_addr(int): value(int|None)}
    `programs`: {program_no(int): program_dict}
    """
    body = {
        "device": device_name,
        "address": address,
        "decimal_places": decimal_places,
        "config": {str(a): config[a] for a in sorted(config)},
        "programs": {str(n): programs[n] for n in sorted(programs)},
    }
    return {
        "meta": {
            "schema": SCHEMA,
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": canonical_hash(body),
            "unstable_reads": unstable or [],
        },
        "data": body,
    }


def verify_snapshot(snapshot: dict) -> bool:
    """Re-hash the data and confirm it matches the stored hash (tamper check)."""
    return canonical_hash(snapshot["data"]) == snapshot["meta"]["sha256"]


def _prog_to_dict(p) -> dict:
    return {
        "start_setpoint": getattr(p, "start_setpoint", 0.0),
        "tolerance": p.tolerance,
        "link_to": p.link_to,
        "segments": [
            {"setpoint": s.setpoint, "minutes": s.duration_minutes, "event": s.event}
            for s in p.segments
        ],
    }


def diff_snapshots(old: dict, new: dict) -> list[str]:
    """Human-readable list of differences between two snapshots' DATA.
    Empty list == identical configuration."""
    changes: list[str] = []
    od, nd = old["data"], new["data"]

    if od.get("decimal_places") != nd.get("decimal_places"):
        changes.append(f"decimal_places: {od.get('decimal_places')} → {nd.get('decimal_places')}")

    names = {a: n for a, n in CONFIG_REGISTERS}
    oc, nc = od.get("config", {}), nd.get("config", {})
    for key in sorted(set(oc) | set(nc), key=lambda k: int(k)):
        if oc.get(key) != nc.get(key):
            label = names.get(int(key), f"register {key}")
            changes.append(f"{label} (reg {key}): {oc.get(key)} → {nc.get(key)}")

    op, np_ = od.get("programs", {}), nd.get("programs", {})
    for key in sorted(set(op) | set(np_), key=lambda k: int(k)):
        if op.get(key) != np_.get(key):
            changes.append(f"program {key} changed")
    return changes


def to_human(snapshot: dict) -> str:
    """Render a snapshot as a plain-text report you can eyeball against the
    controller's front panel."""
    m, d = snapshot["meta"], snapshot["data"]
    unit = "°F" if d["config"].get(str(P.REG_UNIT)) == 1 else "°C"
    lines = [
        f"Novus controller config snapshot",
        f"================================",
        f"Device        : {d['device']}",
        f"BLE address   : {d['address']}",
        f"Captured (UTC): {m['captured_utc']}",
        f"Schema        : {m['schema']}",
        f"SHA-256       : {m['sha256']}",
        f"Decimal places: {d['decimal_places']}   Unit: {unit}",
    ]
    if m.get("unstable_reads"):
        lines.append(f"UNSTABLE READS: {', '.join(str(x) for x in m['unstable_reads'])}")
        lines.append("  (values above did not agree across reads — treat with suspicion)")
    lines.append("")
    lines.append("Config registers")
    lines.append("----------------")
    names = {str(a): n for a, n in CONFIG_REGISTERS}
    order = [str(a) for a, _ in CONFIG_REGISTERS]
    for key in order:
        if key in d["config"]:
            lines.append(f"  {names[key]:<42} = {d['config'][key]}   (reg {key})")
    # any registers present that weren't in our labelled list
    for key in sorted(d["config"], key=lambda k: int(k)):
        if key not in names:
            lines.append(f"  register {key:<34} = {d['config'][key]}")

    lines.append("")
    lines.append("Programs (1..20)")
    lines.append("----------------")
    for key in sorted(d["programs"], key=lambda k: int(k)):
        p = d["programs"][key]
        segs = p.get("segments", [])
        if not segs and not p.get("start_setpoint"):
            lines.append(f"  Program {int(key):>2}: (empty)")
            continue
        lines.append(f"  Program {int(key):>2}: start {p.get('start_setpoint')}{unit}, "
                     f"tolerance {p.get('tolerance')}, link→{p.get('link_to')}")
        for i, s in enumerate(segs, 1):
            lines.append(f"      seg {i}: {s['minutes']:>5} min → {s['setpoint']}{unit}"
                         f"   (event {s['event']})")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The clean read (needs a connected client). Kept here so the CLI tool and the
# app share exactly one implementation.
# --------------------------------------------------------------------------

async def _stable_read_register(client, addr: int, tries: int = 3):
    """Read one register up to `tries` times; return (value, stable?) where
    stable means at least two reads agreed."""
    vals = []
    for _ in range(tries):
        try:
            vals.append((await client.read_registers(addr, 1))[0])
        except Exception:
            vals.append(None)
    for v in vals:
        if v is not None and vals.count(v) >= 2:
            return v, True
    # nothing agreed — return the last non-None (or None) and flag unstable
    non_null = [v for v in vals if v is not None]
    return (non_null[-1] if non_null else None), False


async def _stable_read_program(client, num: int, tries: int = 3):
    """Read a program table up to `tries` times; trust a value seen twice."""
    seen = []
    for _ in range(tries):
        try:
            seen.append(_prog_to_dict(await client.read_program(num)))
        except Exception:
            seen.append(None)
    for v in seen:
        if v is not None and seen.count(v) >= 2:
            return v, True
    non_null = [v for v in seen if v is not None]
    return (non_null[-1] if non_null else None), False


async def read_snapshot(client, program_range=range(1, 21)) -> dict:
    """Read config registers and all program tables from a CONNECTED client,
    verifying each by multiple reads, and return a hashed snapshot document."""
    config, unstable = {}, []
    for addr, _label in CONFIG_REGISTERS:
        val, stable = await _stable_read_register(client, addr)
        config[addr] = val
        if not stable:
            unstable.append(f"reg {addr}")

    programs = {}
    for n in program_range:
        prog, stable = await _stable_read_program(client, n)
        programs[n] = prog
        if not stable:
            unstable.append(f"program {n}")

    return build_snapshot(client.name, client.address,
                          getattr(client, "decimal_places", 0),
                          config, programs, unstable)
