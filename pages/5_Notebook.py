"""
Firing Notebook
===============
Record a firing session — the actual temperature curve, the program that ran,
free-form notes and photos — and let the app reflect on what the controller
ACTUALLY did versus what the program ASKED FOR, then suggest adjustments.

This is the tool for "I ran the same program twice and got different glass":
the glass responds to the real thermal history, and this page shows where two
runs diverged (peak reached, effective soak, time at 100% output, dropouts).

Every entry saves as a self-contained HTML you can email or archive.
"""
from __future__ import annotations

import streamlit as st
from datetime import datetime, timezone

import app_core as core
import notebook as nbmod

st.set_page_config(page_title="Firing Notebook", page_icon="📓", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("📓 Firing Notebook")
st.caption("Record a run, compare what happened to what the program asked for, "
           "and keep notes + photos with it. Analysis is heuristic and only as "
           "fine as the logging interval — treat rates and soaks as approximate.")

names = [d.name for d in devices]
if not names:
    st.info("Add a device first.")
    st.stop()

tab_new, tab_saved, tab_compare = st.tabs(
    ["➕ New entry", "📂 Saved entries", "⚖️ Compare two runs"])

# ---------------------------------------------------------------------------
# New entry
# ---------------------------------------------------------------------------
with tab_new:
    ctrl = st.selectbox("Controller", names, key="nb_ctrl")

    runs = core.detect_recent_runs(ctrl)
    st.caption("Pick a detected run, or set the window by hand.")
    if runs:
        def _label(r):
            return (f"program {r['program']} · {r['start'][:16]} → {r['end'][11:16]} "
                    f"· {r['samples']} samples")
        choice = st.selectbox("Detected runs", list(range(len(runs))),
                              format_func=lambda i: _label(runs[i]), key="nb_run")
        start_iso = runs[choice]["start"]; end_iso = runs[choice]["end"]
        prog_no = runs[choice]["program"]
    else:
        st.info("No completed runs detected in recent logs — set the window below.")
        start_iso = end_iso = None; prog_no = 1

    with st.expander("Set window manually", expanded=not runs):
        c1, c2 = st.columns(2)
        s_in = c1.text_input("Start (UTC ISO)", value=start_iso or "", key="nb_s")
        e_in = c2.text_input("End (UTC ISO)", value=end_iso or "", key="nb_e")
        p_in = st.number_input("Program slot that ran", 1, 20, value=int(prog_no or 1),
                               key="nb_pslot")
        if s_in and e_in:
            start_iso, end_iso, prog_no = s_in, e_in, int(p_in)

    title = st.text_input("Title", value=f"{ctrl} firing "
                          f"{(start_iso or '')[:10]}", key="nb_title")

    st.caption("The program definition is read from the controller so the "
               "comparison has a reference. If you've since changed the slot, "
               "load it from a matching config snapshot instead.")
    prog_source = st.radio("Program reference", ["Read from controller now",
                           "From latest config snapshot"], horizontal=True,
                           key="nb_progsrc")

    if st.button("🔬 Build entry & analyze", type="primary",
                 disabled=not (start_iso and end_iso)):
        with st.spinner("Loading telemetry and the program, analyzing…"):
            try:
                raw = core.load_telemetry_window(ctrl, start_iso, end_iso)
                samples = nbmod.parse_rows(raw)
                if not samples:
                    st.error("No telemetry in that window.")
                    st.stop()
                # get the intended program
                program = None
                if prog_source.startswith("Read"):
                    p = bridge.call(monitor.read_program_now(ctrl, int(prog_no)),
                                    timeout=60)
                    program = {"start_setpoint": getattr(p, "start_setpoint", 0.0),
                               "tolerance": p.tolerance, "link_to": p.link_to,
                               "segments": [{"setpoint": s.setpoint,
                                             "minutes": s.duration_minutes,
                                             "event": s.event} for s in p.segments]}
                else:
                    snaps = core.list_snapshots(ctrl)
                    if snaps:
                        snap = core.load_snapshot(snaps[0])
                        program = snap["data"]["programs"].get(str(int(prog_no)))
                    if not program:
                        st.error("No snapshot with that program — read from "
                                 "controller instead.")
                        st.stop()
                analysis = nbmod.analyze(program, samples)
                st.session_state["nb_doc"] = {
                    "meta": {"schema": nbmod.SCHEMA, "controller": ctrl,
                             "title": title, "program_slot": int(prog_no),
                             "created_utc": datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds"),
                             "window": {"start": start_iso, "end": end_iso}},
                    "program": program, "telemetry": samples,
                    "analysis": analysis, "notes": "", "images": []}
                st.success("Analyzed. Review below, add notes/photos, then save.")
            except Exception as e:
                st.error(f"build failed: {e}")

    # show/edit the in-progress entry
    doc = st.session_state.get("nb_doc")
    if doc and doc["meta"]["controller"] == ctrl:
        a = doc["analysis"]
        st.divider()
        st.subheader(doc["meta"]["title"])
        st.components.v1.html(nbmod.svg_chart(doc["program"], doc["telemetry"]),
                              height=360)
        hw = a["heat_work"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Heat-work", f"{hw['integral_Fh']:g} °F·h")
        m2.metric(f"Min near peak" + (f" (≥{hw['threshold']:g}°)" if hw['threshold'] else ""),
                  f"{hw['minutes_above']:g}")
        m3.metric("Samples", a["window"].get("samples", 0))

        st.markdown("**What happened vs what was asked**")
        import pandas as pd
        st.dataframe(pd.DataFrame([{
            "seg": r["seg"], "kind": r["kind"], "target": r["target"],
            "peak PV": r.get("pv_max", "—"), "actual min": r.get("dur_min", "—"),
            "avg out%": r.get("out_mean", "—"),
            "finding": r.get("finding") or r.get("note") or "ok"}
            for r in a["segments"]]), use_container_width=True, hide_index=True)

        st.markdown("**Suggested adjustments**")
        for s in a["suggestions"]:
            st.write(f"- {s}")
        if a["global_findings"]:
            st.markdown("**Run integrity**")
            for g in a["global_findings"]:
                st.write(f"- {g}")

        doc["notes"] = st.text_area("Notes (glass, thickness, mold, placement, "
                                    "observations…)", value=doc.get("notes", ""),
                                    height=140, key="nb_notes")
        ups = st.file_uploader("Photos", type=["png", "jpg", "jpeg", "webp"],
                               accept_multiple_files=True, key="nb_imgs")
        if ups:
            doc["images"] = [nbmod.image_to_entry(
                u.name, u.type or "image/jpeg", u.getvalue()) for u in ups]
            st.caption(f"{len(doc['images'])} image(s) attached.")

        if st.button("💾 Save entry (JSON + shareable HTML)", type="primary",
                     key="nb_save"):
            paths = core.save_notebook(ctrl, doc)
            st.success(f"Saved: {paths['json'].name} and {paths['html'].name}")
            st.download_button("⬇ Download shareable HTML",
                               nbmod.to_html(doc),
                               file_name=paths["html"].name, mime="text/html")

# ---------------------------------------------------------------------------
# Saved entries
# ---------------------------------------------------------------------------
with tab_saved:
    sctrl = st.selectbox("Controller", names, key="nb_sctrl")
    saved = core.list_notebooks(sctrl)
    if not saved:
        st.caption("No saved entries yet.")
    else:
        pick = st.selectbox("Entry", saved, format_func=lambda p: p.stem,
                            key="nb_pick")
        d = core.load_notebook(pick)
        st.subheader(d["meta"]["title"])
        st.caption(f"{d['meta']['controller']} · slot {d['meta']['program_slot']} "
                   f"· {d['meta']['window']['start']} → {d['meta']['window']['end']}")
        st.components.v1.html(nbmod.svg_chart(d["program"], d["telemetry"]),
                              height=360)
        for s in d["analysis"]["suggestions"]:
            st.write(f"- {s}")
        if d.get("notes"):
            st.markdown("**Notes**"); st.write(d["notes"])
        for im in d.get("images", []):
            st.image(f"data:{im['mime']};base64,{im['data_b64']}",
                     caption=im.get("name"), use_container_width=True)
        st.download_button("⬇ Download shareable HTML", nbmod.to_html(d),
                           file_name=pick.stem + ".html", mime="text/html")

# ---------------------------------------------------------------------------
# Compare two runs
# ---------------------------------------------------------------------------
with tab_compare:
    cctrl = st.selectbox("Controller", names, key="nb_cctrl")
    saved = core.list_notebooks(cctrl)
    if len(saved) < 2:
        st.caption("Save at least two entries for this controller to compare.")
    else:
        c1, c2 = st.columns(2)
        pa = c1.selectbox("Run A", saved, format_func=lambda p: p.stem, key="nb_ca")
        pb = c2.selectbox("Run B", saved, index=1,
                          format_func=lambda p: p.stem, key="nb_cb")
        if st.button("⚖️ Compare", key="nb_docmp"):
            da, db = core.load_notebook(pa), core.load_notebook(pb)
            st.markdown("**Why these runs differ**")
            for line in nbmod.compare_runs(da, db):
                st.write(f"- {line}")
            cc1, cc2 = st.columns(2)
            with cc1:
                st.caption(da["meta"]["title"])
                st.components.v1.html(nbmod.svg_chart(da["program"], da["telemetry"]),
                                      height=300)
            with cc2:
                st.caption(db["meta"]["title"])
                st.components.v1.html(nbmod.svg_chart(db["program"], db["telemetry"]),
                                      height=300)
