"""
notebook.py — firing lab notebook: record a run, compare what the program
ASKED FOR against what the controller ACTUALLY DID, and suggest adjustments.

The glass responds to the real thermal history, not the nominal program. Two
runs of the "same" program can deliver very different heat-work if the elements
lag, the output saturates, or the BLE link drops and a recovery perturbs the
curve. This module reconstructs a firing from the logged telemetry, aligns it
to the intended program segment-by-segment, and reports where they diverged.

Everything here is deterministic and explainable — it's arithmetic over the
log, not a guess. Suggestions are conservative starting points, and the whole
analysis is only as fine-grained as the logging interval (5 min normally, 30 s
in watchdog "worry" mode), so treat rates and soak times as approximate.
"""
from __future__ import annotations

import base64
import html as _html
import statistics
from datetime import datetime
from typing import Optional

SCHEMA = "novus-firing-notebook/1"


# --------------------------------------------------------------------------
# Telemetry parsing
# --------------------------------------------------------------------------

def parse_rows(rows: list[dict]) -> list[dict]:
    """Normalize raw CSV dict rows into typed telemetry samples, dropping ones
    with no usable PV. Expects keys: timestamp_utc, pv, sp, output_pct,
    active_program, active_segment, connected."""
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["timestamp_utc"]).timestamp()
        except Exception:
            continue
        def num(key):
            v = r.get(key, "")
            if v is None or v == "":
                return None
            try:
                return float(v)
            except Exception:
                return None
        pv = num("pv")
        out.append({
            "t": t, "iso": r["timestamp_utc"], "pv": pv, "sp": num("sp"),
            "output": num("output_pct"),
            "program": int(float(r.get("active_program") or 0)),
            "segment": int(float(r.get("active_segment") or 0)),
            "connected": int(float(r.get("connected") or 0)),
        })
    out.sort(key=lambda x: x["t"])
    return out


def _median_dt(samples: list[dict]) -> float:
    ts = [s["t"] for s in samples]
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(diffs) if diffs else 300.0


# --------------------------------------------------------------------------
# Heat-work proxies
# --------------------------------------------------------------------------

def heat_work(samples: list[dict], threshold: Optional[float] = None) -> dict:
    """Time-temperature integral (°F·hours) and minutes above `threshold`.
    A coarse but useful proxy for how much heat-work the glass received.
    Gaps in the log (BLE dropouts) are EXCLUDED — we don't know the temperature
    during them, so they must not inflate the integral."""
    with_pv = [s for s in samples if s["pv"] is not None]
    dt_med = _median_dt(with_pv) if len(with_pv) > 1 else 300.0
    gap_cap = max(3 * dt_med, 600.0)     # anything longer than this is a gap
    integral = 0.0
    mins_above = 0.0
    for a, b in zip(with_pv, with_pv[1:]):
        dt_s = b["t"] - a["t"]
        if dt_s <= 0 or dt_s > gap_cap:
            continue
        dt_h = dt_s / 3600.0
        avg_pv = (a["pv"] + b["pv"]) / 2.0
        integral += avg_pv * dt_h
        if threshold is not None and avg_pv >= threshold:
            mins_above += dt_h * 60.0
    return {"integral_Fh": round(integral, 1),
            "minutes_above": round(mins_above, 1),
            "threshold": threshold}


# --------------------------------------------------------------------------
# Segment alignment + per-segment analysis
# --------------------------------------------------------------------------

def _assign_segments(program: dict, samples: list[dict]) -> dict[int, list[dict]]:
    """Group samples by which intended segment they belong to (1-based index).
    Prefer the controller's reported segment column; fall back to cumulative
    intended time if that column is unusable."""
    n = len(program.get("segments", []))
    if n == 0:
        return {}
    seg_vals = {s["segment"] for s in samples if 1 <= s["segment"] <= n}
    groups: dict[int, list[dict]] = {i: [] for i in range(1, n + 1)}

    if seg_vals:
        # controller reports a valid current segment (1-based)
        for s in samples:
            k = s["segment"]
            if 1 <= k <= n:
                groups[k].append(s)
        return groups

    # fallback: slice by cumulative intended minutes from the first sample
    if not samples:
        return groups
    t0 = samples[0]["t"]
    bounds = []
    acc = 0.0
    for seg in program["segments"]:
        acc += max(0.0, float(seg["minutes"]))
        bounds.append(t0 + acc * 60.0)
    for s in samples:
        idx = 1
        for i, b in enumerate(bounds, start=1):
            if s["t"] <= b:
                idx = i
                break
            idx = i
        groups[idx].append(s)
    return groups


def analyze(program: dict, samples: list[dict]) -> dict:
    """Compare intended program to actual telemetry. Returns a structured
    analysis with per-segment rows, global findings, suggestions, and
    heat-work. `program` is {start_setpoint, tolerance, link_to, segments:[
    {setpoint, minutes, event}]}."""
    samples = [s for s in samples if s["pv"] is not None] or samples
    tol = max(float(program.get("tolerance") or 0), 5.0)
    segs = program.get("segments", [])
    peak_target = max((float(s["setpoint"]) for s in segs), default=None)

    groups = _assign_segments(program, samples)
    prev = float(program.get("start_setpoint") or (samples[0]["pv"] if samples else 0))

    seg_rows = []
    suggestions = []
    for i, seg in enumerate(segs, start=1):
        target = float(seg["setpoint"])
        intended_min = float(seg["minutes"])
        rows = groups.get(i, [])
        is_hold = abs(target - prev) < 1.0

        row = {"seg": i, "target": target, "prev": prev,
               "intended_min": intended_min, "kind": "hold" if is_hold else "ramp",
               "n": len(rows)}

        if not rows:
            row["note"] = "no telemetry captured (sampling gap or skipped)"
            seg_rows.append(row)
            prev = target
            continue

        pvs = [r["pv"] for r in rows]
        outs = [r["output"] for r in rows if r["output"] is not None]
        pv_start, pv_end, pv_max = pvs[0], pvs[-1], max(pvs)
        dur_min = (rows[-1]["t"] - rows[0]["t"]) / 60.0
        out_mean = statistics.mean(outs) if outs else None
        sat_frac = (sum(1 for o in outs if o >= 99) / len(outs)) if outs else 0.0
        reached = pv_max >= target - tol
        overshoot = max(0.0, pv_max - (target + tol))

        row.update({"pv_max": round(pv_max, 1), "pv_end": round(pv_end, 1),
                    "dur_min": round(dur_min, 1),
                    "out_mean": round(out_mean, 0) if out_mean is not None else None,
                    "sat_frac": round(sat_frac, 2), "reached": reached,
                    "overshoot": round(overshoot, 1)})

        if is_hold:
            # effective soak = time PV stayed within tolerance of target
            dt = _median_dt(rows) if len(rows) > 1 else 60.0
            in_band = sum(1 for p in pvs if abs(p - target) <= tol) * dt / 60.0
            row["soak_actual_min"] = round(in_band, 1)
            if not reached:
                short = target - pv_max
                row["finding"] = f"never reached {target:g}° (peaked {pv_max:g}°, {short:g}° short)"
                if sat_frac >= 0.5:
                    suggestions.append(
                        f"Seg {i} (soak {target:g}°): output was pinned "
                        f"{sat_frac*100:.0f}% of the time and still fell {short:g}° "
                        f"short — power-limited. The glass got less heat-work than "
                        f"the program implies. Fix the power/element issue, or lower "
                        f"this target to what the furnace can actually hold.")
            elif in_band < 0.7 * intended_min:
                row["finding"] = (f"effective soak ~{in_band:.0f} min vs "
                                  f"{intended_min:g} min requested")
                suggestions.append(
                    f"Seg {i} (soak {target:g}°): PV was only within ±{tol:g}° for "
                    f"~{in_band:.0f} of the {intended_min:g} requested minutes — the "
                    f"effective heat-work is lower than the program states. Extend "
                    f"the soak or improve the approach so it settles sooner.")
            elif overshoot > 0:
                row["finding"] = f"overshoot {overshoot:g}° above target"
        else:
            intended_rate = (abs(target - prev) / (intended_min / 60.0)
                             if intended_min > 1 else None)
            achieved_rate = (abs(pv_end - pv_start) / (dur_min / 60.0)
                             if dur_min > 0 else None)
            row["intended_rate"] = round(intended_rate) if intended_rate else "AFAP"
            row["achieved_rate"] = round(achieved_rate) if achieved_rate else None
            if not reached and sat_frac >= 0.5:
                short = target - pv_max
                row["finding"] = (f"couldn't reach {target:g}° (peaked {pv_max:g}°) "
                                  f"at {sat_frac*100:.0f}% output")
                sug = (f"Seg {i} (ramp to {target:g}°): output pinned and still "
                       f"{short:g}° short — power-limited. ")
                if intended_rate and achieved_rate and achieved_rate < intended_rate:
                    sug += (f"It managed ~{achieved_rate:.0f}°/hr against a requested "
                            f"{intended_rate:.0f}°/hr. Set the ramp to "
                            f"≤{achieved_rate:.0f}°/hr so the program matches reality, "
                            f"or address the hardware.")
                suggestions.append(sug)
            elif (intended_rate and achieved_rate
                  and achieved_rate < 0.8 * intended_rate):
                row["finding"] = (f"ramped ~{achieved_rate:.0f}°/hr vs "
                                  f"{intended_rate:.0f}°/hr requested (ran long)")
                suggestions.append(
                    f"Seg {i} (ramp to {target:g}°): achieved only "
                    f"~{achieved_rate:.0f}°/hr of the {intended_rate:.0f}°/hr asked "
                    f"({sat_frac*100:.0f}% output). Everything after it starts late. "
                    f"Lower the target rate to match, or reduce load / fix power.")
        seg_rows.append(row)
        prev = target

    # ---- global findings: dropouts, disconnects, interruptions ----
    global_findings = []
    dt_med = _median_dt(samples)
    gaps = []
    for a, b in zip(samples, samples[1:]):
        if (b["t"] - a["t"]) > max(3 * dt_med, 180):
            gaps.append((a["iso"], round((b["t"] - a["t"]) / 60.0, 1)))
    if gaps:
        total = sum(g[1] for g in gaps)
        global_findings.append(
            f"{len(gaps)} telemetry gap(s) totaling ~{total:.0f} min — likely BLE "
            f"dropouts. The real thermal history during those gaps is unknown, and "
            f"a recovery restart may have perturbed the curve.")
    disc = sum(1 for s in samples if s["connected"] == 0)
    if disc:
        global_findings.append(f"{disc} sample(s) logged with no connection.")
    progs = {s["program"] for s in samples if s["program"] > 0}
    if len(progs) > 1:
        global_findings.append(
            f"More than one program number appeared in this window ({sorted(progs)}) "
            f"— a restart or manual change happened mid-run.")

    hw = heat_work(samples, threshold=(peak_target - tol) if peak_target else None)
    if not suggestions and not global_findings:
        suggestions.append("Actual curve tracked the program within tolerance — "
                            "no adjustments indicated from this run alone.")

    return {"segments": seg_rows, "global_findings": global_findings,
            "suggestions": suggestions, "heat_work": hw, "tolerance": tol,
            "peak_target": peak_target,
            "window": {"start": samples[0]["iso"] if samples else None,
                       "end": samples[-1]["iso"] if samples else None,
                       "samples": len(samples)}}


def compare_runs(nb_a: dict, nb_b: dict) -> list[str]:
    """Compare two notebooks of (nominally) the same program and explain why
    their results differ. Returns human-readable lines."""
    a, b = nb_a["analysis"], nb_b["analysis"]
    la = nb_a["meta"].get("title", "Run A")
    lb = nb_b["meta"].get("title", "Run B")
    out = []

    ha, hb = a["heat_work"]["integral_Fh"], b["heat_work"]["integral_Fh"]
    if ha and hb:
        pct = (hb - ha) / ha * 100 if ha else 0
        out.append(f"Heat-work: {la} {ha:g} °F·h vs {lb} {hb:g} °F·h "
                   f"({pct:+.0f}%). The glass responds to this, not the program.")
    ma, mb = a["heat_work"]["minutes_above"], b["heat_work"]["minutes_above"]
    if a["heat_work"]["threshold"]:
        out.append(f"Minutes near peak (≥{a['heat_work']['threshold']:g}°): "
                   f"{la} {ma:g} min vs {lb} {mb:g} min.")

    for sa, sb in zip(a["segments"], b["segments"]):
        pa, pb = sa.get("pv_max"), sb.get("pv_max")
        if pa is not None and pb is not None and abs(pa - pb) >= 10:
            out.append(f"Seg {sa['seg']} (target {sa['target']:g}°): peak "
                       f"{la} {pa:g}° vs {lb} {pb:g}° — {abs(pa-pb):g}° apart.")
        fa, fb = sa.get("sat_frac", 0), sb.get("sat_frac", 0)
        if abs(fa - fb) >= 0.3:
            hotter = la if fa > fb else lb
            out.append(f"Seg {sa['seg']}: {hotter} spent much more time at 100% "
                       f"output ({fa*100:.0f}% vs {fb*100:.0f}%) — it was working "
                       f"harder to hold, a sign of the power shortfall.")
    if not out:
        out.append("The two runs are close on the metrics measured here; look to "
                   "notes/images for other differences (glass, mold, placement).")
    return out


# --------------------------------------------------------------------------
# SVG chart (self-contained, no JS/CDN — safe to email and open offline)
# --------------------------------------------------------------------------

def svg_chart(program: dict, samples: list[dict], width=760, height=340) -> str:
    pv = [(s["t"], s["pv"]) for s in samples if s["pv"] is not None]
    sp = [(s["t"], s["sp"]) for s in samples if s["sp"] is not None]
    if not pv:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    t0 = pv[0][0]
    tmax = max(p[0] for p in pv) - t0 or 1
    allv = [v for _, v in pv] + [v for _, v in sp] + \
           [float(s["setpoint"]) for s in program.get("segments", [])]
    vmin, vmax = min(allv), max(allv)
    pad = (vmax - vmin) * 0.08 or 10
    vmin -= pad; vmax -= 0; vmax += pad
    L, R, T, B = 54, 14, 12, 28
    def X(t): return L + (t - t0) / tmax * (width - L - R)
    def Y(v): return T + (1 - (v - vmin) / (vmax - vmin or 1)) * (height - T - B)

    def path(pts):
        return "M" + " L".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in pts)

    # intended target step line (piecewise from start through each segment end)
    steps = []
    acc = t0
    prev = float(program.get("start_setpoint") or pv[0][1])
    steps.append((acc, prev))
    for seg in program.get("segments", []):
        acc += max(0.0, float(seg["minutes"])) * 60.0
        steps.append((min(acc, t0 + tmax), float(seg["setpoint"])))
    step_pts = [(t, v) for t, v in steps if t <= t0 + tmax + 1]

    # y gridlines
    grid = ""
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = Y(v)
        grid += (f"<line x1='{L}' y1='{y:.1f}' x2='{width-R}' y2='{y:.1f}' "
                 f"stroke='var(--grid,#e2e2e2)' stroke-width='1'/>"
                 f"<text x='{L-6}' y='{y+3:.1f}' text-anchor='end' font-size='10' "
                 f"fill='var(--muted,#888)'>{v:.0f}</text>")

    return f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">
{grid}
<path d="{path(step_pts)}" fill="none" stroke="var(--target,#c9a227)" stroke-width="2" stroke-dasharray="5 4"/>
<path d="{path(sp)}" fill="none" stroke="var(--sp,#4a90d9)" stroke-width="1.4" opacity="0.8"/>
<path d="{path(pv)}" fill="none" stroke="var(--pv,#d9534f)" stroke-width="2.2"/>
<g font-size="11">
<rect x="{L}" y="{T}" width="12" height="3" fill="#d9534f"/><text x="{L+16}" y="{T+4}" fill="var(--muted,#555)">actual PV</text>
<rect x="{L+90}" y="{T}" width="12" height="3" fill="#4a90d9"/><text x="{L+106}" y="{T+4}" fill="var(--muted,#555)">controller SP</text>
<rect x="{L+210}" y="{T}" width="12" height="3" fill="#c9a227"/><text x="{L+226}" y="{T+4}" fill="var(--muted,#555)">program target</text>
</g>
</svg>"""


# --------------------------------------------------------------------------
# Self-contained HTML export
# --------------------------------------------------------------------------

def to_html(nb: dict) -> str:
    m, a = nb["meta"], nb["analysis"]
    esc = _html.escape
    segs_html = "".join(
        f"<tr><td>{r['seg']}</td><td>{r['kind']}</td><td>{r['target']:g}</td>"
        f"<td>{r.get('pv_max','—')}</td><td>{r.get('dur_min','—')}</td>"
        f"<td>{r.get('out_mean','—')}</td>"
        f"<td>{esc(str(r.get('finding') or r.get('note') or 'ok'))}</td></tr>"
        for r in a["segments"])
    sugg = "".join(f"<li>{esc(s)}</li>" for s in a["suggestions"])
    glob = "".join(f"<li>{esc(g)}</li>" for g in a["global_findings"]) or "<li>none</li>"
    notes = esc(nb.get("notes", "") or "").replace("\n", "<br>")
    imgs = "".join(
        f"<figure><img src='data:{i['mime']};base64,{i['data_b64']}' "
        f"style='max-width:100%;border-radius:6px'/>"
        f"<figcaption>{esc(i.get('name',''))}</figcaption></figure>"
        for i in nb.get("images", []))
    hw = a["heat_work"]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(m.get('title','Firing'))}</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#222}}
 h1{{margin-bottom:.2rem}} .muted{{color:#777}}
 table{{border-collapse:collapse;width:100%;margin:.5rem 0}}
 td,th{{border:1px solid #ddd;padding:4px 8px;font-size:14px;text-align:left}}
 figure{{margin:1rem 0}} li{{margin:.3rem 0}}
 .card{{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:1rem;margin:1rem 0}}
</style></head><body>
<h1>{esc(m.get('title','Firing'))}</h1>
<p class="muted">{esc(m.get('controller',''))} · program slot {m.get('program_slot','?')}
 · {esc(m.get('window',{}).get('start',''))} → {esc(m.get('window',{}).get('end',''))}
 · {a['window'].get('samples',0)} samples</p>
<div class="card">{svg_chart(nb['program'], nb['telemetry'])}</div>
<div class="card"><b>Heat-work</b>: {hw['integral_Fh']:g} °F·hours
 · {hw['minutes_above']:g} min near peak
 {f"(≥{hw['threshold']:g}°)" if hw['threshold'] else ""}</div>
<h2>What happened vs what was asked</h2>
<table><tr><th>seg</th><th>kind</th><th>target</th><th>peak PV</th>
<th>actual min</th><th>avg out%</th><th>finding</th></tr>{segs_html}</table>
<h2>Suggested adjustments</h2><ul>{sugg}</ul>
<h2>Run integrity</h2><ul>{glob}</ul>
<h2>Notes</h2><p>{notes or '<span class="muted">(none)</span>'}</p>
{f"<h2>Images</h2>{imgs}" if imgs else ""}
<hr><p class="muted" style="font-size:12px">Generated by novus-n20k48-ble firing
 notebook ({SCHEMA}). Analysis is heuristic and limited by the logging interval;
 treat rates and soak times as approximate.</p>
</body></html>"""


def image_to_entry(name: str, mime: str, raw: bytes) -> dict:
    return {"name": name, "mime": mime,
            "data_b64": base64.b64encode(raw).decode("ascii")}
