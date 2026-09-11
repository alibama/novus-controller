#!/usr/bin/env python3
"""
export_usage.py — emit per-firing energy/cost records for the cost estimator and
for open-data publishing (drops into the glassdatabase.org importer).

    python tools/export_usage.py [--out DIR] [--rate 0.12] [--currency USD] \
                                 [--studio "Raging Buffalo Glass"]

Writes kiln_firings.csv and kiln_firings.xlsx (sheet 'kiln_firings' + a
'data_dictionary' sheet) covering every detected run across all controllers in
devices.json. Rated power per controller comes from devices.json (power_kw);
rate/currency/studio come from the flags or studio_settings.json.

Good as a cron job: publish the CSV to an open-data page, or point the
glass-database importer at the folder:
    python -m central.ingest build --uploads /path/to/this/out/dir
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import argparse
import json

import runs as runs_mod
import notebook as nb
import usage as usage_mod
from devices import load_devices

HERE = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = HERE / "logs"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--rate", type=float, default=None, help="price per kWh")
    ap.add_argument("--currency", default=None)
    ap.add_argument("--studio", default=None)
    args = ap.parse_args()

    # studio settings from file, overridden by flags
    ss = {"studio": "", "rate_per_kwh": None, "currency": "USD"}
    sp = HERE / "studio_settings.json"
    if sp.exists():
        try:
            ss.update(json.loads(sp.read_text()))
        except Exception:
            pass
    rate = args.rate if args.rate is not None else ss.get("rate_per_kwh")
    currency = args.currency or ss.get("currency", "USD")
    studio = args.studio if args.studio is not None else ss.get("studio", "")

    rows = []
    for dev in load_devices():
        for run in runs_mod.detect_recent_runs(LOG_DIR, dev.name, max_runs=100):
            samples = nb.parse_rows(
                runs_mod.load_telemetry_window(LOG_DIR, dev.name,
                                               run["start"], run["end"]))
            if not samples:
                continue
            rows.append(usage_mod.summarize_run(
                dev.name, dev.role, run["program"], samples,
                power_kw=getattr(dev, "power_kw", 0.0) or 0.0,
                rate=(rate or None), currency=currency, studio=studio))
    rows.sort(key=lambda r: r.get("start_utc", ""), reverse=True)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kiln_firings.csv").write_text(usage_mod.to_csv(rows))
    try:
        import pandas as pd
        with pd.ExcelWriter(out / "kiln_firings.xlsx", engine="openpyxl") as xw:
            pd.DataFrame(rows, columns=usage_mod.COLUMNS).to_excel(
                xw, sheet_name="kiln_firings", index=False)
            pd.DataFrame([{"column": k, "description": v}
                          for k, v in usage_mod.DATA_DICTIONARY.items()]).to_excel(
                xw, sheet_name="data_dictionary", index=False)
        xlsx = " and kiln_firings.xlsx"
    except Exception as e:
        xlsx = f" (xlsx skipped: {e})"

    total_kwh = sum(r["energy_kwh"] or 0 for r in rows)
    print(f"{len(rows)} firings · ~{total_kwh:.0f} kWh est. → "
          f"{out}/kiln_firings.csv{xlsx}")


if __name__ == "__main__":
    main()
