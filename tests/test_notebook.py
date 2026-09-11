"""
Firing-notebook analysis tests: intended-vs-actual findings, heat-work with
gap exclusion, run comparison, and HTML export. Pure functions over synthetic
telemetry — no hardware.

Run with:  pytest tests/test_notebook.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta
import notebook as nb

PROGRAM = {"start_setpoint": 70.0, "tolerance": 5.0, "link_to": 0,
           "segments": [{"setpoint": 1465.0, "minutes": 279, "event": 0},
                        {"setpoint": 1465.0, "minutes": 10, "event": 0}]}


def _run(peak, sat, start="2026-07-01T14:00:00+00:00", dt_s=300, drop=False):
    t = datetime.fromisoformat(start); rows = []; pv = 70.0
    for i in range(60):
        pv = min(peak, pv + (peak - 70) / 40)
        seg = 1 if pv < peak - 2 else 2
        out = sat if pv < peak - 1 else 40
        rows.append({"timestamp_utc": t.isoformat(), "pv": round(pv, 1), "sp": 1465,
                     "output_pct": out, "active_program": 1, "active_segment": seg,
                     "connected": 1})
        t += timedelta(seconds=dt_s)
        if drop and i == 30:
            t += timedelta(seconds=1800)      # 30-min BLE gap
    for _ in range(4):
        rows.append({"timestamp_utc": t.isoformat(), "pv": round(peak, 1), "sp": 1465,
                     "output_pct": 40, "active_program": 1, "active_segment": 2,
                     "connected": 1})
        t += timedelta(seconds=dt_s)
    return nb.parse_rows(rows)


def test_healthy_run_needs_no_adjustment():
    a = nb.analyze(PROGRAM, _run(peak=1465, sat=80))
    assert any("within tolerance" in s for s in a["suggestions"])
    assert a["heat_work"]["minutes_above"] > 0


def test_power_limited_run_is_flagged():
    a = nb.analyze(PROGRAM, _run(peak=1418, sat=100))
    assert any("power-limited" in s for s in a["suggestions"])
    # never reached the 1465 target
    assert not a["segments"][0]["reached"]


def test_dropout_is_reported():
    a = nb.analyze(PROGRAM, _run(peak=1465, sat=80, drop=True))
    assert any("gap" in g for g in a["global_findings"])


def test_heat_work_excludes_gaps():
    healthy = nb.analyze(PROGRAM, _run(peak=1465, sat=80))
    limited = nb.analyze(PROGRAM, _run(peak=1418, sat=100, drop=True))
    # the power-limited run delivered less heat-work despite the (excluded) gap
    assert limited["heat_work"]["integral_Fh"] < healthy["heat_work"]["integral_Fh"]


def test_compare_runs_explains_divergence():
    a = nb.analyze(PROGRAM, _run(peak=1465, sat=80))
    b = nb.analyze(PROGRAM, _run(peak=1418, sat=100, drop=True))
    lines = nb.compare_runs({"meta": {"title": "A"}, "analysis": a},
                            {"meta": {"title": "B"}, "analysis": b})
    assert any("Heat-work" in ln for ln in lines)
    assert any("apart" in ln or "100% output" in ln for ln in lines)


def test_html_export_is_self_contained():
    a = nb.analyze(PROGRAM, _run(peak=1465, sat=80))
    doc = {"meta": {"title": "T", "controller": "kiln1", "program_slot": 3,
                    "window": {"start": "s", "end": "e"}},
           "program": PROGRAM, "telemetry": _run(1465, 80), "analysis": a,
           "notes": "6mm clear", "images": []}
    h = nb.to_html(doc)
    assert "<svg" in h and "Heat-work" in h and "Suggested adjustments" in h
    assert "http://" not in h.replace("http://www.w3.org", "")  # no external deps


def test_short_soak_detected():
    prog = {"start_setpoint": 1400.0, "tolerance": 5.0, "link_to": 0,
            "segments": [{"setpoint": 1465.0, "minutes": 5, "event": 0},
                         {"setpoint": 1465.0, "minutes": 60, "event": 0}]}
    # reaches target but the hold barely stays in band (peaks then drifts)
    t = datetime.fromisoformat("2026-07-01T14:00:00+00:00"); rows = []
    for i in range(3):
        rows.append({"timestamp_utc": t.isoformat(), "pv": 1465, "sp": 1465,
                     "output_pct": 60, "active_program": 1, "active_segment": 1,
                     "connected": 1}); t += timedelta(minutes=5)
    for i in range(12):     # "hold" but drifting well below band most of the time
        pv = 1465 if i < 2 else 1440
        rows.append({"timestamp_utc": t.isoformat(), "pv": pv, "sp": 1465,
                     "output_pct": 100, "active_program": 1, "active_segment": 2,
                     "connected": 1}); t += timedelta(minutes=5)
    a = nb.analyze(prog, nb.parse_rows(rows))
    hold = a["segments"][1]
    assert "soak" in (hold.get("finding") or "") or any(
        "soak" in s for s in a["suggestions"])
