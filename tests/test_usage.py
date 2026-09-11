"""
Usage/energy tests: kWh estimation (with gap exclusion), cost, run
summarization, CSV columns, and the shared run detector. No hardware.

Run with:  pytest tests/test_usage.py
"""
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta
import notebook as nb
import usage
import runs as runs_mod


def _samples(n=24, dt_min=5, out_lo=20, out_hi=100, hi_n=18,
             start="2026-07-02T10:00:00+00:00", drop_at=None):
    t = datetime.fromisoformat(start); rows = []
    for i in range(n):
        out = out_hi if i < hi_n else out_lo
        rows.append({"timestamp_utc": t.isoformat(), "pv": min(2100, 200 + i * 110),
                     "sp": 2100, "output_pct": out, "active_program": 1,
                     "active_segment": 1, "connected": 1})
        t += timedelta(minutes=dt_min)
        if drop_at == i:
            t += timedelta(minutes=45)          # a big gap
    return nb.parse_rows(rows)


def test_energy_scales_with_power_and_output():
    s = _samples()
    e12 = usage.energy_kwh(s, 12.0)
    e6 = usage.energy_kwh(s, 6.0)
    assert e12 is not None and abs(e12 - 2 * e6) < 0.01     # linear in kW


def test_energy_none_without_rating():
    assert usage.energy_kwh(_samples(), 0.0) is None


def test_energy_excludes_gaps():
    full = usage.energy_kwh(_samples(), 12.0)
    gapped = usage.energy_kwh(_samples(drop_at=10), 12.0)
    assert gapped < full          # the 45-min gap is not billed


def test_summarize_run_cost_and_fields():
    s = _samples()
    kwh = usage.energy_kwh(s, 12.0)
    row = usage.summarize_run("furnace", "furnace", 1, s, power_kw=12.0,
                              rate=0.12, currency="USD", studio="RBG")
    assert row["cost"] == round(kwh * 0.12, 2)
    assert row["studio"] == "RBG" and row["schema"] == usage.SCHEMA
    assert row["peak_temp_F"] == 2100.0 and row["program"] == 1


def test_summarize_without_rate_leaves_cost_blank():
    row = usage.summarize_run("kiln1", "kiln", 1, _samples(), power_kw=0.0)
    assert row["energy_kwh"] is None and row["cost"] is None


def test_csv_has_declared_columns():
    row = usage.summarize_run("f", "furnace", 1, _samples(), 12.0, 0.12)
    csv = usage.to_csv([row])
    assert csv.splitlines()[0] == ",".join(usage.COLUMNS)


def test_run_detector_splits_on_idle():
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "k").mkdir()
    lines = ["timestamp_utc,pv,sp,output_pct,active_program,active_segment,connected"]
    t = datetime.fromisoformat("2026-07-02T10:00:00+00:00")
    for i in range(6):
        lines.append(f"{t.isoformat()},1000,1465,100,3,1,1"); t += timedelta(minutes=5)
    for i in range(3):
        lines.append(f"{t.isoformat()},800,896,0,0,0,1"); t += timedelta(minutes=5)
    for i in range(4):
        lines.append(f"{t.isoformat()},1200,1465,100,3,1,1"); t += timedelta(minutes=5)
    (tmp / "k" / "2026-07-02.csv").write_text("\n".join(lines) + "\n")
    runs = runs_mod.detect_recent_runs(tmp, "k")
    assert len(runs) == 2
    win = runs_mod.load_telemetry_window(tmp, "k", runs[-1]["start"], runs[-1]["end"])
    assert len(win) == 6
