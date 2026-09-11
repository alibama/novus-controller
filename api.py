"""
api.py — read-only OPEN DATA API for kiln/furnace usage.

Serves consumption (kWh) and cost per firing plus per-device aggregates as JSON
(and CSV), so anyone can consume the studio's operating data for global insight.
It is deliberately public-safe:
  * read-only (GET only), CORS open to any origin,
  * NO Bluetooth addresses, NO secrets, NO raw personal data — just firing
    summaries and rollups,
  * data is CC-BY-4.0 (attribution requested).

Run it:
    pip install -r requirements-api.txt
    uvicorn api:app --host 0.0.0.0 --port 8000
Interactive docs at /docs. Pairs with the glassdatabase.org Explore app.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

import runs as runs_mod
import notebook as nb
import usage as usage_mod
from devices import load_devices

HERE = Path(__file__).resolve().parent
# Data dir is overridable so the API can run from anywhere (and be tested).
DATA_DIR = Path(os.environ.get("KILN_DATA_DIR", HERE))
LOG_DIR = DATA_DIR / "logs"

LICENSE = "CC-BY-4.0"
_cache = {"t": 0.0, "rows": None}


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
    description="Read-only energy & cost data for studio kilns/furnaces "
                f"({LICENSE}).",
    version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET"], allow_headers=["*"])


@app.get("/")
def index():
    ss = _studio()
    return {
        "name": "Kiln open-data API",
        "studio": ss.get("studio", ""),
        "license": LICENSE,
        "attribution": ss.get("studio") or "kiln open data",
        "data_dictionary": usage_mod.DATA_DICTIONARY,
        "endpoints": {
            "/firings": "per-firing energy & cost (JSON). "
                        "filters: controller, since, limit",
            "/firings.csv": "same data as CSV",
            "/summary": "totals and rollups by controller and month",
            "/devices": "per-device aggregates (no addresses)",
            "/health": "liveness check",
            "/docs": "interactive OpenAPI docs",
        },
    }


@app.get("/health")
def health():
    return {"ok": True}


def _filter(rows, controller, since):
    if controller:
        rows = [r for r in rows if r["controller"] == controller]
    if since:
        rows = [r for r in rows if (r.get("start_utc") or "") >= since]
    return rows


@app.get("/firings")
def firings(controller: str | None = Query(None),
            since: str | None = Query(None, description="UTC ISO lower bound"),
            limit: int = Query(1000, ge=1, le=10000)):
    rows = _filter(_all_rows(), controller, since)[:limit]
    return {"count": len(rows), "license": LICENSE, "firings": rows}


@app.get("/firings.csv", response_class=PlainTextResponse)
def firings_csv(controller: str | None = Query(None),
                since: str | None = Query(None)):
    rows = _filter(_all_rows(), controller, since)
    return usage_mod.to_csv(rows)


@app.get("/summary")
def summary():
    rows = _all_rows()
    def _sum(rs, k):
        return round(sum((r.get(k) or 0) for r in rs), 2)
    by_ctrl = {}
    by_month = {}
    for r in rows:
        by_ctrl.setdefault(r["controller"], []).append(r)
        by_month.setdefault((r.get("date_utc") or "")[:7], []).append(r)
    ss = _studio()
    return {
        "license": LICENSE,
        "studio": ss.get("studio", ""),
        "currency": ss.get("currency", "USD"),
        "totals": {"firings": len(rows),
                   "energy_kwh": _sum(rows, "energy_kwh"),
                   "cost": _sum(rows, "cost")},
        "by_controller": {c: {"firings": len(rs),
                              "energy_kwh": _sum(rs, "energy_kwh"),
                              "cost": _sum(rs, "cost")}
                          for c, rs in by_ctrl.items()},
        "by_month": {m: {"firings": len(rs),
                        "energy_kwh": _sum(rs, "energy_kwh"),
                        "cost": _sum(rs, "cost")}
                     for m, rs in sorted(by_month.items()) if m},
    }


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
            "power_kw_rated": getattr(dev, "power_kw", 0.0) or None,
            "firings": len(drows),
            "energy_kwh": round(sum((r.get("energy_kwh") or 0) for r in drows), 1),
            "cost": round(sum((r.get("cost") or 0) for r in drows), 2),
        })
    return {"devices": out, "license": LICENSE}
