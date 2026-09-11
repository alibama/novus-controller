"""
usage.py — turn logged firings into energy + cost records for a cost estimator
and for open-data export (e.g. into glassdatabase.org).

Energy is ESTIMATED from the output-power percentage the controller reports:
each sample contributes  power_kw × (output% / 100) × dt.  That is the standard
kiln-energy approximation. It is exact for time-proportioned (SSR) control and a
good approximation for phase-angle (SCR) control. Two caveats, stated plainly in
the export:
  * it assumes output% maps linearly to delivered power, and
  * `power_kw` is the *rated* element power — as SiC elements age they draw less,
    so a rated figure over-estimates. Calibrate `power_kw` from a clamp-meter
    reading at 100% output for the truest numbers.

Gaps in the log (BLE dropouts) are excluded — unknown time can't be billed.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Optional

import notebook as _nb

SCHEMA = "novus-kiln-usage/1"

# One row per firing. Column order used in the CSV/XLSX export.
COLUMNS = [
    "studio", "controller", "role", "program", "date_utc",
    "start_utc", "end_utc", "duration_h", "peak_temp_F", "avg_output_pct",
    "power_kw_rated", "energy_kwh", "rate_per_kwh", "cost", "currency",
    "heat_work_Fh", "minutes_at_peak", "samples", "gaps", "schema",
]

DATA_DICTIONARY = {
    "studio": "Studio/operator name (attribution for the open dataset).",
    "controller": "Controller/device name.",
    "role": "furnace or kiln.",
    "program": "Program slot number that was running.",
    "date_utc": "UTC date the run started (YYYY-MM-DD).",
    "start_utc": "Run start timestamp (UTC ISO 8601).",
    "end_utc": "Run end timestamp (UTC ISO 8601).",
    "duration_h": "Elapsed hours from first to last sample.",
    "peak_temp_F": "Highest process value reached (°F).",
    "avg_output_pct": "Mean output power percentage over the run.",
    "power_kw_rated": "Rated element power at 100% output (kW).",
    "energy_kwh": "Estimated energy used (kWh) = sum of power_kw x output/100 x dt.",
    "rate_per_kwh": "Electricity price used for the cost estimate.",
    "cost": "Estimated energy cost = energy_kwh x rate_per_kwh.",
    "currency": "Currency of rate_per_kwh and cost.",
    "heat_work_Fh": "Time-temperature integral (°F-hours), a heat-work proxy.",
    "minutes_at_peak": "Minutes spent near the peak target temperature.",
    "samples": "Number of telemetry samples in the run.",
    "gaps": "Count of excluded log gaps (BLE dropouts) in the run.",
    "schema": "Dataset schema tag.",
}


def _median_dt(samples: list[dict]) -> float:
    ts = [s["t"] for s in samples]
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(diffs) if diffs else 300.0


def energy_kwh(samples: list[dict], power_kw: float) -> Optional[float]:
    """Estimated energy (kWh) from the output% integral, excluding log gaps."""
    if not power_kw:
        return None
    rows = [s for s in samples if s.get("output") is not None]
    if len(rows) < 2:
        return 0.0
    gap_cap = max(3 * _median_dt(rows), 600.0)
    e = 0.0
    for a, b in zip(rows, rows[1:]):
        dt_s = b["t"] - a["t"]
        if dt_s <= 0 or dt_s > gap_cap:
            continue
        frac = ((a["output"] + b["output"]) / 2.0) / 100.0
        e += power_kw * frac * (dt_s / 3600.0)
    return round(e, 3)


def count_gaps(samples: list[dict]) -> int:
    if len(samples) < 2:
        return 0
    gap_cap = max(3 * _median_dt(samples), 600.0)
    return sum(1 for a, b in zip(samples, samples[1:])
               if (b["t"] - a["t"]) > gap_cap)


def summarize_run(controller: str, role: str, program: int, samples: list[dict],
                  power_kw: float = 0.0, rate: Optional[float] = None,
                  currency: str = "USD", studio: str = "") -> dict:
    """Build one open-data usage row from a run's parsed telemetry."""
    pvs = [s["pv"] for s in samples if s["pv"] is not None]
    outs = [s["output"] for s in samples if s["output"] is not None]
    start_t = samples[0]["t"] if samples else 0
    end_t = samples[-1]["t"] if samples else 0
    dur_h = round((end_t - start_t) / 3600.0, 2)
    peak = max(pvs) if pvs else None
    hw = _nb.heat_work(samples, threshold=(peak - 10) if peak else None)
    kwh = energy_kwh(samples, power_kw)
    cost = round(kwh * rate, 2) if (kwh is not None and rate) else None
    return {
        "studio": studio,
        "controller": controller,
        "role": role,
        "program": program,
        "date_utc": samples[0]["iso"][:10] if samples else "",
        "start_utc": samples[0]["iso"] if samples else "",
        "end_utc": samples[-1]["iso"] if samples else "",
        "duration_h": dur_h,
        "peak_temp_F": round(peak, 1) if peak is not None else None,
        "avg_output_pct": round(statistics.mean(outs), 1) if outs else None,
        "power_kw_rated": power_kw or None,
        "energy_kwh": kwh,
        "rate_per_kwh": rate,
        "cost": cost,
        "currency": currency if rate else "",
        "heat_work_Fh": hw["integral_Fh"],
        "minutes_at_peak": hw["minutes_above"],
        "samples": len(samples),
        "gaps": count_gaps(samples),
        "schema": SCHEMA,
    }


def to_csv(rows: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
