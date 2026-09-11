"""
runs.py — pure helpers for reading logged telemetry and detecting firing runs.
No Streamlit, no BLE — just the CSV logs on disk. Shared by the app (app_core)
and the headless CLI (tools/export_usage.py).
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


def load_telemetry_window(log_dir, controller: str, start_iso: str,
                          end_iso: str) -> list[dict]:
    """Return raw CSV rows for `controller` whose timestamp is within
    [start_iso, end_iso]. Spans day-files."""
    d = Path(log_dir) / controller
    if not d.exists():
        return []
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except Exception:
        return []
    rows = []
    for csv_path in sorted(d.glob("*.csv")):
        try:
            with csv_path.open(newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        t = datetime.fromisoformat(r["timestamp_utc"])
                    except Exception:
                        continue
                    if start <= t <= end:
                        rows.append(r)
        except Exception as e:
            print(f"[runs] read {csv_path} failed: {e}")
    rows.sort(key=lambda r: r["timestamp_utc"])
    return rows


def detect_recent_runs(log_dir, controller: str, max_runs: int = 6,
                       gap_min: float = 20.0) -> list[dict]:
    """Find contiguous windows where a program was running (active_program > 0).
    A new run starts on a not-running→running edge or after a time gap. Returns
    newest-first {'start','end','program','samples'}."""
    d = Path(log_dir) / controller
    if not d.exists():
        return []
    samples = []
    for csv_path in sorted(d.glob("*.csv"))[-8:]:
        try:
            with csv_path.open(newline="") as f:
                samples.extend(list(csv.DictReader(f)))
        except Exception:
            pass
    runs, cur, last_t, prev_running = [], None, None, False
    for r in samples:
        try:
            t = datetime.fromisoformat(r["timestamp_utc"])
            prog = int(float(r.get("active_program") or 0))
        except Exception:
            continue
        running = prog > 0
        if running:
            new_run = ((not prev_running) or cur is None or
                       (last_t and (t - last_t).total_seconds() > gap_min * 60))
            if new_run:
                if cur:
                    runs.append(cur)
                cur = {"start": r["timestamp_utc"], "end": r["timestamp_utc"],
                       "program": prog, "samples": 1}
            else:
                cur["end"] = r["timestamp_utc"]; cur["samples"] += 1
                cur["program"] = prog
            last_t = t
        prev_running = running
    if cur:
        runs.append(cur)
    return list(reversed(runs))[:max_runs]
