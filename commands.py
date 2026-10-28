"""
commands.py — a tiny file-based command queue between the open-data API and
the monitor.

Why a queue? The Bluetooth link to each controller can be held by only ONE
process, and that process is the dashboard (its background monitor owns the
radio). The API runs as a separate service, so instead of touching Bluetooth
it writes a small JSON file; the monitor picks it up within a couple of
seconds, executes it with the same safety discipline as a button in the UI,
and writes the result back into the same file. Anyone (authenticated) can
queue a command; anyone can read its status.

Command file: <COMMANDS_DIR>/<id>.json
  {"id", "device", "action", "args", "source", "created_utc",
   "status": "pending" | "running" | "done" | "failed" | "rejected",
   "result", "error", "finished_utc"}

Allowed actions and their arguments:
  run_program          {"program": 1..20}
  stop_program         {}
  set_manual_setpoint  {"setpoint": float}
  poll                 {}            (take a fresh reading now)
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ACTIONS = {
    "run_program": ("program",),
    "stop_program": (),
    "set_manual_setpoint": ("setpoint",),
    "poll": (),
}
WRITE_ACTIONS = {"run_program", "stop_program", "set_manual_setpoint"}
TERMINAL = {"done", "failed", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate(action: str, args: dict | None) -> tuple[bool, str]:
    """(ok, message). Checks the action name and its argument shape."""
    args = args or {}
    if action not in ALLOWED_ACTIONS:
        return False, f"unknown action '{action}' (allowed: {sorted(ALLOWED_ACTIONS)})"
    for key in ALLOWED_ACTIONS[action]:
        if key not in args:
            return False, f"action '{action}' needs argument '{key}'"
    if action == "run_program":
        try:
            n = int(args["program"])
        except Exception:
            return False, "program must be an integer 1..20"
        if not 1 <= n <= 20:
            return False, "program must be 1..20"
    if action == "set_manual_setpoint":
        try:
            float(args["setpoint"])
        except Exception:
            return False, "setpoint must be a number"
    return True, "ok"


def enqueue(commands_dir: Path, device: str, action: str, args: dict | None = None,
            source: str = "api") -> dict:
    """Write a pending command file; returns the command dict (with its id)."""
    ok, msg = validate(action, args)
    if not ok:
        raise ValueError(msg)
    commands_dir = Path(commands_dir)
    commands_dir.mkdir(parents=True, exist_ok=True)
    cid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    cmd = {"id": cid, "device": device, "action": action, "args": args or {},
           "source": source, "created_utc": _now(), "status": "pending",
           "result": None, "error": None, "finished_utc": None}
    _write(commands_dir / f"{cid}.json", cmd)
    return cmd


def _write(path: Path, cmd: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd, indent=2))
    os.replace(tmp, path)


def load(commands_dir: Path, cid: str) -> dict | None:
    p = Path(commands_dir) / f"{cid}.json"
    if not p.exists() or "/" in cid or "\\" in cid or ".." in cid:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def update(commands_dir: Path, cid: str, **fields) -> dict | None:
    cmd = load(commands_dir, cid)
    if cmd is None:
        return None
    cmd.update(fields)
    if cmd.get("status") in TERMINAL and not cmd.get("finished_utc"):
        cmd["finished_utc"] = _now()
    _write(Path(commands_dir) / f"{cid}.json", cmd)
    return cmd


def pending(commands_dir: Path) -> list[dict]:
    """Oldest-first list of commands still waiting to run."""
    d = Path(commands_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            c = json.loads(p.read_text())
        except Exception:
            continue
        if c.get("status") == "pending":
            out.append(c)
    return out


def recent(commands_dir: Path, limit: int = 50) -> list[dict]:
    """Newest-first list of all commands (any status)."""
    d = Path(commands_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


def prune(commands_dir: Path, keep: int = 500) -> None:
    """Keep the queue directory from growing forever."""
    d = Path(commands_dir)
    if not d.exists():
        return
    files = sorted(d.glob("*.json"))
    for p in files[:-keep] if len(files) > keep else []:
        try:
            p.unlink()
        except Exception:
            pass
