"""
api.py — the studio's OPEN DATA API.

  * Unauthenticated READ: live readings, per-firing energy & cost, rollups,
    per-device aggregates, watchdog events, notes. CORS-open, CC-BY-4.0, and
    public-safe — NO Bluetooth addresses, NO secrets, ever.
  * Authorized WRITE (Bearer token from api.env): post a note against a
    device/firing, or queue a controller command (run/stop a program, hold a
    setpoint, take a reading). Commands are executed by the dashboard's
    monitor — the only process that owns the Bluetooth radio — within a couple
    of seconds, with the same safety rules as the on-screen buttons.

Run it:
    uvicorn api:app --host 0.0.0.0 --port 8000
Interactive docs at /docs. The installer runs it as the `kiln-api` service.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import commands as cmdq
import notebook as nb
import runs as runs_mod
import usage as usage_mod
from devices import load_devices

HERE = Path(__file__).resolve().parent
# Data dir is overridable so the API can run from anywhere (and be tested).
DATA_DIR = Path(os.environ.get("KILN_DATA_DIR", HERE))
LOG_DIR = DATA_DIR / "logs"
LIVE_PATH = DATA_DIR / "state" / "live.json"
COMMANDS_DIR = DATA_DIR / "commands"
NOTES_PATH = DATA_DIR / "notes" / "notes.jsonl"
API_ENV = DATA_DIR / "api.env"

LICENSE = "CC-BY-4.0"
VERSION = "2.0.0"
_cache = {"t": 0.0, "rows": None}


# ---------------------------------------------------------------------------
# Config: token + flags (environment first, then api.env next to the data)
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    if v is not None:
        return v
    if API_ENV.exists():
        for line in API_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def write_token() -> str:
    return _env("API_WRITE_TOKEN", "")


def control_allowed() -> bool:
    return _env("API_ALLOW_CONTROL", "1").strip().lower() in ("1", "true", "yes", "on")


def require_token(authorization: Optional[str] = Header(None)) -> str:
    """Bearer-token gate for every write endpoint."""
    expected = write_token()
    if not expected:
        raise HTTPException(503, "writes disabled: no API_WRITE_TOKEN configured "
                                 "(see api.env.example)")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    given = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(given, expected):
        raise HTTPException(403, "invalid token")
    return "ok"


def _studio() -> dict:
    p = DATA_DIR / "studio_settings.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"studio": "", "rate_per_kwh": None, "currency": "USD"}


def _all_rows(ttl: float = 60.0) -> list[dict]:
    """Every detected firing across all controllers, as usage rows. Cached."""
    now = time.time()
    if _cache["rows"] is not None and (now - _cache["t"]) < ttl:
        return _cache["rows"]
    ss = _studio()
    rows = []
    for dev in load_devices():
        for run in runs_mod.detect_recent_runs(LOG_DIR, dev.name, max_runs=1000):
            samples = nb.parse_rows(runs_mod.load_telemetry_window(
                LOG_DIR, dev.name, run["start"], run["end"]))
            if not samples:
                continue
            rows.append(usage_mod.summarize_run(
                dev.name, dev.role, run["program"], samples,
                power_kw=getattr(dev, "power_kw", 0.0) or 0.0,
                rate=(ss.get("rate_per_kwh") or None),
                currency=ss.get("currency", "USD"),
                studio=ss.get("studio", "")))
    rows.sort(key=lambda r: r.get("start_utc", ""), reverse=True)
    _cache.update(t=now, rows=rows)
    return rows


app = FastAPI(
    title="Kiln open-data API",
    description="Live readings, energy & cost data for studio kilns/furnaces "
                f"({LICENSE}). Reads are public; writes need a bearer token.",
    version=VERSION)

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])


@app.get("/")
def index():
    ss = _studio()
    return {
        "name": "Kiln open-data API",
        "version": VERSION,
        "studio": ss.get("studio", ""),
        "license": LICENSE,
        "attribution": ss.get("studio") or "kiln open data",
        "data_dictionary": usage_mod.DATA_DICTIONARY,
        "endpoints": {
            "GET /live": "latest reading per controller (PV, SP, output, "
                         "program, watchdog state)",
            "GET /firings": "per-firing energy & cost (JSON). "
                            "filters: controller, since, until, limit",
            "GET /firings.csv": "same data as CSV (same filters)",
            "GET /summary": "totals and rollups by controller and month. "
                            "filters: since, until (date or ISO; until is "
                            "inclusive of the whole day)",
            "GET /devices": "per-device aggregates and model (no addresses)",
            "GET /telemetry/{controller}": "raw logged samples (PV/SP/output/"
                                           "program). filters: since, until, limit",
            "GET /events": "watchdog/audit events. filters: controller, limit",
            "GET /notes": "notes posted via the API. filters: controller, limit",
            "GET /commands": "recent queued commands and their status",
            "GET /commands/{id}": "one command's status",
            "POST /notes": "(bearer token) attach a note to a controller/firing",
            "POST /commands": "(bearer token) queue run_program / stop_program / "
                              "set_manual_setpoint / poll / diagnose for a controller",
            "GET /health": "liveness check",
            "GET /docs": "interactive OpenAPI docs",
        },
        "write_access": {
            "how": "Authorization: Bearer <API_WRITE_TOKEN from api.env>",
            "enabled": bool(write_token()),
            "control_commands_enabled": control_allowed(),
        },
    }


@app.get("/health")
def health():
    live_age = None
    if LIVE_PATH.exists():
        live_age = round(time.time() - LIVE_PATH.stat().st_mtime, 1)
    return {"ok": True, "live_age_s": live_age,
            "writes_enabled": bool(write_token())}


# ---------------------------------------------------------------------------
# Live state (written by the dashboard's monitor after every poll)
# ---------------------------------------------------------------------------
@app.get("/live")
def live():
    """Latest reading for every controller. Address-free by construction —
    the monitor never writes addresses into live.json."""
    if not LIVE_PATH.exists():
        return {"schema": "novus-kiln-live/1", "updated_utc": None,
                "devices": {}, "note": "no readings yet — is the dashboard running?"}
    try:
        data = json.loads(LIVE_PATH.read_text())
    except Exception as e:
        raise HTTPException(503, f"live state unreadable: {e}")
    data["age_s"] = round(time.time() - LIVE_PATH.stat().st_mtime, 1)
    data["license"] = LICENSE
    return data


# ---------------------------------------------------------------------------
# Firings / summary / devices
# ---------------------------------------------------------------------------
def _filter(rows, controller=None, since=None, until=None):
    """Filter firing rows by controller and an inclusive [since, until] date/time
    range. `since`/`until` accept a date (YYYY-MM-DD) or a full ISO timestamp;
    a bare `until` date includes the whole day. Comparisons are lexicographic on
    the ISO start time, which is correct for ISO 8601."""
    if controller:
        rows = [r for r in rows if r["controller"] == controller]
    if since:
        rows = [r for r in rows if (r.get("start_utc") or "") >= since]
    if until:
        upper = until if len(until) > 10 else until + "T23:59:59+00:00"
        rows = [r for r in rows if (r.get("start_utc") or "") <= upper]
    return rows


@app.get("/firings")
def firings(controller: str | None = Query(None),
            since: str | None = Query(None, description="lower bound, date or ISO"),
            until: str | None = Query(None, description="upper bound, date or ISO"),
            limit: int = Query(1000, ge=1, le=10000)):
    rows = _filter(_all_rows(), controller, since, until)[:limit]
    return {"count": len(rows), "since": since, "until": until,
            "license": LICENSE, "firings": rows}


@app.get("/firings.csv", response_class=PlainTextResponse)
def firings_csv(controller: str | None = Query(None),
                since: str | None = Query(None),
                until: str | None = Query(None)):
    rows = _filter(_all_rows(), controller, since, until)
    return usage_mod.to_csv(rows)


@app.get("/summary")
def summary(since: str | None = Query(None, description="lower bound, date or ISO"),
            until: str | None = Query(None, description="upper bound, date or ISO")):
    rows = _filter(_all_rows(), None, since, until)

    def _sum(rs, k):
        return round(sum((r.get(k) or 0) for r in rs), 2)
    by_ctrl = {}
    by_month = {}
    for r in rows:
        by_ctrl.setdefault(r["controller"], []).append(r)
        by_month.setdefault((r.get("date_utc") or "")[:7], []).append(r)
    ss = _studio()
    n = len(rows)
    energy_kwh = _sum(rows, "energy_kwh")
    cost = _sum(rows, "cost")
    dates = [r.get("date_utc") for r in rows if r.get("date_utc")]
    first = min(dates, default=None)
    last = max(dates, default=None)
    return {
        "schema": "novus-kiln-usage-summary/1",
        "license": LICENSE,
        "studio": ss.get("studio", ""),
        "currency": ss.get("currency", "USD"),
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # requested window (null when unfiltered), echoed back for clarity
        "range": {"since": since, "until": until},
        # --- top-level keys for the Glass Database data contract (+ aliases) ---
        "firings": n,
        "count": n,
        "energy_kwh": energy_kwh,
        "kwh": energy_kwh,
        "cost_usd": cost,
        "cost": cost,
        "since": first,          # earliest firing date in the returned set
        "start": first,
        "until": last,           # latest firing date in the returned set
        "end": last,
        # --- richer detail, preserved ---
        "totals": {"firings": n, "energy_kwh": energy_kwh, "cost": cost},
        "by_controller": {c: {"firings": len(rs),
                              "energy_kwh": _sum(rs, "energy_kwh"),
                              "cost": _sum(rs, "cost")}
                          for c, rs in by_ctrl.items()},
        "by_month": {m: {"firings": len(rs),
                        "energy_kwh": _sum(rs, "energy_kwh"),
                        "cost": _sum(rs, "cost")}
                     for m, rs in sorted(by_month.items()) if m},
    }


@app.get("/telemetry/{controller}")
def telemetry(controller: str,
              since: str | None = Query(None, description="ISO start (default: 24 h ago)"),
              until: str | None = Query(None, description="ISO end (default: now)"),
              limit: int = Query(5000, ge=1, le=100000)):
    """Raw logged samples for one controller: timestamp, PV, SP, output %,
    program, segment, connected. What the dashboard's charts are drawn from.
    Address-free (the CSVs never contain one)."""
    if controller not in _known_names():
        raise HTTPException(404, f"unknown controller '{controller}'")
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    s = since or (now - timedelta(hours=24)).isoformat(timespec="seconds")
    u = until or now.isoformat(timespec="seconds")
    rows = runs_mod.load_telemetry_window(LOG_DIR, controller, s, u)
    if not rows and (since or until):
        # a bare date is a common way to ask; widen it to the whole day(s)
        if since and len(since) == 10:
            s = since + "T00:00:00+00:00"
        if until and len(until) == 10:
            u = until + "T23:59:59+00:00"
        rows = runs_mod.load_telemetry_window(LOG_DIR, controller, s, u)
    out = []
    for r in rows[-limit:]:
        def num(k):
            v = r.get(k, "")
            try:
                return float(v) if v not in ("", None) else None
            except Exception:
                return None
        out.append({"time_utc": r.get("timestamp_utc"), "pv": num("pv"), "sp": num("sp"),
                    "output_pct": num("output_pct"),
                    "program": int(num("active_program") or 0),
                    "segment": int(num("active_segment") or 0),
                    "connected": bool(int(num("connected") or 0))})
    return {"controller": controller, "since": s, "until": u, "count": len(out),
            "license": LICENSE, "samples": out}


@app.get("/devices")
def devices_endpoint():
    """Per-device rollups. Deliberately omits BLE addresses."""
    rows = _all_rows()
    out = []
    for dev in load_devices():
        drows = [r for r in rows if r["controller"] == dev.name]
        out.append({
            "name": dev.name,
            "role": dev.role,
            "model": dev.model,
            "verified": dev.verified,
            "power_kw_rated": getattr(dev, "power_kw", 0.0) or None,
            "firings": len(drows),
            "energy_kwh": round(sum((r.get("energy_kwh") or 0) for r in drows), 1),
            "cost": round(sum((r.get("cost") or 0) for r in drows), 2),
        })
    return {"devices": out, "license": LICENSE}


# ---------------------------------------------------------------------------
# Events (watchdog audit trail) and notes
# ---------------------------------------------------------------------------
_KNOWN = None


def _known_names() -> set[str]:
    return {d.name for d in load_devices()}


@app.get("/events")
def events(controller: str | None = Query(None), limit: int = Query(100, ge=1, le=2000)):
    """Watchdog/audit events (detections, restarts, confirmations, API commands)."""
    names = [controller] if controller else sorted(_known_names())
    out = []
    for name in names:
        p = LOG_DIR / name / "events.log"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ts, _, msg = line.partition("\t")
            out.append({"controller": name, "time_utc": ts, "event": msg})
    out.sort(key=lambda e: e["time_utc"], reverse=True)
    return {"count": min(len(out), limit), "events": out[:limit]}


class NoteIn(BaseModel):
    controller: str = Field(..., description="device name as shown in the dashboard")
    text: str = Field(..., min_length=1, max_length=4000)
    firing_start_utc: str | None = Field(None, description="optional ISO start of the firing this note is about")
    author: str | None = Field(None, max_length=120)
    tags: list[str] = Field(default_factory=list)


@app.get("/notes")
def notes(controller: str | None = Query(None), limit: int = Query(100, ge=1, le=2000)):
    if not NOTES_PATH.exists():
        return {"count": 0, "notes": []}
    out = []
    for line in NOTES_PATH.read_text().splitlines():
        try:
            n = json.loads(line)
        except Exception:
            continue
        if controller and n.get("controller") != controller:
            continue
        out.append(n)
    out.reverse()
    return {"count": min(len(out), limit), "notes": out[:limit]}


@app.post("/notes", status_code=201)
def post_note(note: NoteIn, _: str = Depends(require_token)):
    """Attach a note (glass, mold, observations…) to a controller or firing.
    Appends to notes/notes.jsonl; the dashboard's Open Data page shows them."""
    if note.controller not in _known_names():
        raise HTTPException(404, f"unknown controller '{note.controller}'")
    entry = {"id": uuid.uuid4().hex[:12], "controller": note.controller,
             "text": note.text, "firing_start_utc": note.firing_start_utc,
             "author": note.author or "", "tags": note.tags,
             "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NOTES_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ---------------------------------------------------------------------------
# Commands (queued for the dashboard's monitor, which owns Bluetooth)
# ---------------------------------------------------------------------------
class CommandIn(BaseModel):
    controller: str
    action: str = Field(..., description="run_program | stop_program | set_manual_setpoint | poll | diagnose")
    program: int | None = Field(None, ge=1, le=20)
    setpoint: float | None = None
    source: str | None = Field(None, max_length=120, description="who/what is asking (for the audit log)")


def _public_cmd(c: dict) -> dict:
    return {k: c.get(k) for k in ("id", "device", "action", "args", "source",
                                  "created_utc", "status", "result", "error",
                                  "finished_utc")}


@app.post("/commands", status_code=202)
def post_command(cmd: CommandIn, _: str = Depends(require_token)):
    """Queue a controller command. The monitor executes it within a few
    seconds and records the outcome; poll GET /commands/{id} for status.
    Read-only (unverified) controllers reject write actions."""
    if cmd.controller not in _known_names():
        raise HTTPException(404, f"unknown controller '{cmd.controller}'")
    if cmd.action in cmdq.WRITE_ACTIONS and not control_allowed():
        raise HTTPException(403, "control commands are disabled on this install "
                                 "(API_ALLOW_CONTROL=0)")
    args = {}
    if cmd.program is not None:
        args["program"] = cmd.program
    if cmd.setpoint is not None:
        args["setpoint"] = cmd.setpoint
    try:
        entry = cmdq.enqueue(COMMANDS_DIR, cmd.controller, cmd.action, args,
                             source=cmd.source or "api")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _public_cmd(entry)


@app.get("/commands")
def list_commands(limit: int = Query(50, ge=1, le=500)):
    return {"commands": [_public_cmd(c) for c in cmdq.recent(COMMANDS_DIR, limit)]}


@app.get("/commands/{cid}")
def get_command(cid: str):
    c = cmdq.load(COMMANDS_DIR, cid)
    if c is None:
        raise HTTPException(404, "no such command")
    return _public_cmd(c)
