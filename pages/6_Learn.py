"""
Learn page
==========
A plain-English guide to the two things this app sits between: the Novus
N20K48 controller (what PID does, what a ramp & soak program is, what every
setting means) and the glass (why it is annealed, what happens at each
temperature, why two identical programs give different pieces).

Reads come from glass_science.py (pure data + calculators). The only writes
on this page are the optional "write this anneal schedule to a slot" section,
which follows the Settings page's confirm-before-overwrite pattern.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import app_core as core
import glass_science as gs
from programs_library import LIBRARY, total_minutes
from novus_client import ProgramSegment

st.set_page_config(page_title="Learn", page_icon="📚", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("📚 Learn")
st.caption("How the controller thinks, how the glass behaves, and how to read "
           "what happened. Every temperature here is °F — the app currently "
           "assumes °F throughout.")

names = [d.name for d in devices]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _segment_rows(start_sp: float, segments) -> list[dict]:
    """Same ramp/hold wording as the Settings page: hold / AFAP / 'heat 400°/hr'.
    `segments` is [(minutes, setpoint), ...] or objects with .minutes/.setpoint."""
    rows = [{"seg": "start", "target °F": f"{start_sp:g}",
             "minutes": "—", "ramp/hold": "—"}]
    prev_sp = start_sp
    for i, seg in enumerate(segments, start=1):
        if isinstance(seg, (tuple, list)):
            minutes, sp = int(seg[0]), float(seg[1])
        else:
            minutes, sp = int(seg.minutes), float(seg.setpoint)
        if sp == prev_sp:
            kind = "hold"
        elif minutes <= 1:
            kind = "AFAP cool" if sp < prev_sp else "AFAP heat"
        else:
            rate = abs(gs.effective_rate(prev_sp, sp, minutes))
            kind = f"{'cool' if sp < prev_sp else 'heat'} {rate:.0f}°/hr"
        rows.append({"seg": str(i), "target °F": f"{sp:g}",
                     "minutes": minutes, "ramp/hold": kind})
        prev_sp = sp
    return rows


def _show_svg(svg: str, height: int = 320):
    st.components.v1.html(svg, height=height)


def _hours(minutes: int) -> str:
    return f"{minutes / 60.0:.1f}h"


tab_ctrl, tab_prog, tab_anneal, tab_proc, tab_hw = st.tabs([
    "🎛 Your controller", "📋 Programs (ramp & soak)", "❄️ Annealing",
    "🔥 Fusing, slumping, casting", "📈 Heat-work & reading your logs",
])


# ===========================================================================
# 1. YOUR CONTROLLER
# ===========================================================================
with tab_ctrl:
    st.header("The N20K48 in one paragraph")
    st.markdown(
        "The Novus N20K48 is a **PID process controller** with a built-in "
        "**ramp & soak program engine**. It reads one thermocouple, compares "
        "that reading (the *process variable*, PV) to the temperature it wants "
        "(the *setpoint*, SP), and switches the kiln's elements on and off to "
        "close the gap. The program engine is what moves the setpoint over "
        "time — 'take two hours to reach 1000°F, hold for twenty minutes, "
        "then …' — and the PID loop is what chases it.")

    st.subheader("What PID does on a kiln, without the maths")
    st.markdown(
        "Imagine driving to a stop line. A beginner floors it until the line, "
        "then slams the brakes, and overshoots. A good driver eases off as the "
        "line gets closer (**proportional**), notices they keep stopping a "
        "little short and learns to roll a touch further (**integral**), and "
        "brakes earlier when they're arriving fast (**derivative**).\n\n"
        "- **Proportional** — the further below the setpoint, the more power. "
        "Inside the *proportional band* power tapers off as the kiln closes in.\n"
        "- **Integral** — if the kiln sits a few degrees under the setpoint for "
        "a while, the integral term quietly adds power until the gap closes. "
        "Without it a kiln never quite gets there.\n"
        "- **Derivative** — if the temperature is climbing fast, pull power "
        "early so the kiln's momentum doesn't carry it past the setpoint.\n\n"
        "A kiln is slow and heavy: once it's hot it keeps heating for a while "
        "after the elements go off. Good PID settings account for that, which "
        "is why the controller's **autotune** is usually a better starting "
        "point than guessing.")

    st.subheader("Every tuning setting, in plain English")
    st.caption("Register numbers are the N20K48 holding registers this app "
               "reads and writes. 'Too high' and 'too low' describe what you "
               "would see on the kiln.")
    st.dataframe(pd.DataFrame([{
        "setting": t["name"], "register": t["register"], "unit": t["unit"],
        "what it does": t["plain"], "too high": t["too_high"],
        "too low": t["too_low"],
    } for t in gs.PID_TERMS.values()]), width="stretch", hide_index=True)

    st.subheader("Read the live PID settings from a controller")
    st.caption("Reads registers 257–261 (autotune, Pb, Ir, dt, Ct). This grabs "
               "Bluetooth briefly — the background monitor waits for it.")
    if not names:
        st.info("No controllers configured yet — add one on the Settings page.")
    else:
        rc1, rc2 = st.columns([2, 1])
        read_name = rc1.selectbox("Controller", names, key="learn_pid_ctrl")
        if rc2.button("Read PID settings now", key="learn_pid_read",
                      width="stretch"):
            with st.spinner("Grabbing Bluetooth and reading…"):
                try:
                    regs = bridge.call(
                        monitor.read_registers_now(read_name, gs.REG_TUNE_AUTO, 5),
                        timeout=30)
                    order = ["autotune", "pb", "ir", "dt", "ct"]
                    rows = []
                    for key, raw in zip(order, regs):
                        t = gs.PID_TERMS[key]
                        rows.append({"setting": t["name"], "register": t["register"],
                                     "raw value": raw, "unit": t["unit"],
                                     "meaning": t["plain"]})
                    st.success(f"Read {len(regs)} registers from {read_name}.")
                    st.dataframe(pd.DataFrame(rows), width="stretch",
                                 hide_index=True)
                    st.caption("Raw register values are shown as the controller "
                               "stores them; the proportional band and timing "
                               "fields may carry a decimal place depending on "
                               "the controller's display setting.")
                except Exception as e:
                    st.error(f"read failed: {e}")


# ===========================================================================
# 2. PROGRAMS (RAMP & SOAK)
# ===========================================================================
with tab_prog:
    st.header("How a ramp & soak program works")
    st.markdown(
        "A program is a table of up to **9 segments**. Each segment says: "
        "*take this many minutes to reach this setpoint*. The controller draws "
        "a straight line from the previous setpoint to the new one over that "
        "time and chases it. A **hold** is simply a segment whose setpoint is "
        "the same as the one before. The controller stores 20 such programs, "
        "and this app's Settings page writes into any of those slots.")

    st.subheader("Why tolerance is the setting that matters most")
    st.markdown(
        "With **tolerance 0**, the timer runs on the wall clock. A 10-minute "
        "fuse hold starts counting the moment the setpoint reaches 1465 — even "
        "if the kiln is still at 1420 and climbing. With **tolerance ±5°F** "
        "(a *guaranteed soak*), the timer only counts while the real "
        "temperature is within 5° of the setpoint, so the hold is ten minutes "
        "*at temperature*. The same program, a slow kiln and a fast kiln: with "
        "tolerance set, they give the same glass; without it, they don't.")

    st.subheader("Terms")
    st.dataframe(pd.DataFrame([{"term": t["name"], "meaning": t["plain"]}
                               for t in gs.RS_TERMS.values()]),
                 width="stretch", hide_index=True)

    st.subheader("The standard templates, drawn")
    st.caption("These are the programs the Settings page can write. Profiles "
               "show the setpoint the controller will chase; with guaranteed "
               "soak on, real time will be longer wherever the kiln lags.")
    for key, prog in LIBRARY.items():
        with st.expander(f"{prog.name} — {_hours(total_minutes(prog))} programmed"):
            st.markdown(prog.description)
            st.caption(f"Start {prog.start_setpoint:g}°F · tolerance "
                       f"±{prog.tolerance:g}° · {len(prog.segments)} segments")
            _show_svg(gs.program_svg(
                [(s.minutes, s.setpoint) for s in prog.segments],
                prog.start_setpoint))
            st.dataframe(pd.DataFrame(_segment_rows(prog.start_setpoint, prog.segments)),
                         width="stretch", hide_index=True)


# ===========================================================================
# 3. ANNEALING
# ===========================================================================
with tab_anneal:
    st.header("Why glass has to be annealed")
    st.markdown(
        "Hot glass is a liquid that is slowly becoming a solid. If the outside "
        "of a piece stiffens while the inside is still shrinking, the two "
        "fight, and the piece is left with stress locked inside. It may look "
        "fine, then crack on the shelf a week later, or shatter when it's "
        "cold-worked. **Annealing** is the cure: hold the whole piece at a "
        "temperature where it can still relax (**the anneal point, ~900°F for "
        "COE-90**), long enough for the core to catch up with the surface, "
        "then cool it slowly enough that no new stress forms until it is below "
        "the **strain point (~700°F)**, where the glass is rigid and only "
        "thermal shock can hurt it. Below that you can cool faster.\n\n"
        "Thickness is everything. Heat moves through glass slowly, so a thick "
        "piece needs a longer soak for the centre to reach 900°F and a far "
        "slower cool so the centre and surface stay close in temperature.")

    st.subheader("Anneal schedule by thickness (COE-90, °F)")
    st.warning("Starting points from Bullseye-style 'Annealing Thick Slabs' "
               "guidance (Bullseye Glass TechNotes). **Verify these for your "
               "glass** and err on the slow side; nobody ever regretted "
               "annealing a little longer.")
    st.dataframe(pd.DataFrame([{
        "thickness": f"{r.thickness_mm} mm ({r.thickness_label})",
        "soak at 900°F": _hours(r.soak_minutes),
        "cool to 800 (°F/hr)": r.rate_to_800,
        "cool to 700 (°F/hr)": r.rate_to_700,
        "cool to 70 (°F/hr)": r.rate_to_70,
    } for r in gs.ANNEAL_TABLE]), width="stretch", hide_index=True)

    st.subheader("Calculator")
    mm = st.slider("Glass thickness (mm) — use the THICKEST part of the piece",
                   min_value=3, max_value=120, value=12, step=1, key="learn_mm")
    sched = gs.anneal_schedule(mm)
    if sched["extrapolated"]:
        st.error(f"{mm} mm is thicker than the chart goes ({gs.ANNEAL_TABLE[-1].thickness_mm} mm). "
                 "Showing the thickest row — this is NOT enough for your piece; "
                 "consult the full Bullseye chart or a casting reference.")
    st.markdown(
        f"**{mm} mm → using the {sched['thickness_mm']} mm "
        f"({sched['thickness_label']}) row** (nearest or next thicker, never "
        f"thinner): soak **{_hours(sched['soak_minutes'])} at {sched['soak_temp']:g}°F**, "
        f"then cool **{sched['rate_to_800']:g}°F/hr to 800**, "
        f"**{sched['rate_to_700']:g}°F/hr to 700**, "
        f"**{sched['rate_to_70']:g}°F/hr to 70**.")

    segs = gs.anneal_segments(mm, start_temp=gs.ANNEAL_POINT_F)
    tot = gs.anneal_total_minutes(segs)
    st.caption(f"As a controller program starting at {gs.ANNEAL_POINT_F:g}°F "
               f"(the kiln is already at the anneal temperature after a crash-"
               f"cool): {len(segs)} segments, ~{_hours(tot)} programmed.")
    _show_svg(gs.program_svg(segs, gs.ANNEAL_POINT_F))
    st.dataframe(pd.DataFrame(_segment_rows(gs.ANNEAL_POINT_F, segs)),
                 width="stretch", hide_index=True)

    st.subheader("Write this anneal schedule to a controller slot")
    st.caption("Writes the program above (start setpoint 900°F, tolerance ±5°) "
               "into a permanent slot. Run it right after your process hold "
               "has crash-cooled to 900, or chain to it from another program. "
               "This grabs Bluetooth briefly.")
    if not core.is_admin():
        st.caption("Read-only session: sign in as admin to write programs.")
    elif not names:
        st.info("No controllers configured yet — add one on the Devices page.")
    else:
        w1, w2 = st.columns([2, 1])
        w_name = w1.selectbox("Controller", names, key="learn_an_ctrl")
        w_slot = w2.number_input("Slot", min_value=1, max_value=20, value=2,
                                 key="learn_an_slot")
        w_pw = st.text_input(
            "Controller password (only if writes are protected — leave blank otherwise)",
            type="password", key="learn_an_pw",
            help="Some controllers require unlocking (register 53) before config writes.")
        st.info(f"This will **overwrite program slot {int(w_slot)} on {w_name}** "
                f"with the {sched['thickness_mm']} mm anneal schedule. The "
                f"controller stores it permanently.")
        w_ok = st.checkbox(f"Yes, overwrite slot {int(w_slot)} on {w_name}",
                           key="learn_an_confirm")
        if st.button("✍️ Write anneal program", type="primary", disabled=not w_ok,
                     key="learn_an_write"):
            pseg = [ProgramSegment(setpoint=float(sp), duration_minutes=int(m), event=0)
                    for m, sp in segs]
            with st.spinner("Grabbing Bluetooth and writing…"):
                try:
                    bridge.call(
                        monitor.write_program_now(
                            w_name, int(w_slot), pseg,
                            tolerance=5.0, start_setpoint=gs.ANNEAL_POINT_F,
                            password=(int(w_pw) if w_pw.strip().isdigit() else None)),
                        timeout=60)
                    st.success(f"Wrote and verified the {sched['thickness_mm']} mm "
                               f"anneal schedule to slot {int(w_slot)} on {w_name}.")
                    core.render_segments(w_name, int(w_slot), monitor)
                except Exception as e:
                    st.error(f"write failed: {e}")
                    if w_pw.strip() and not w_pw.strip().isdigit():
                        st.caption("Note: the password must be numeric for the "
                                   "Novus session register.")


# ===========================================================================
# 4. FUSING, SLUMPING, CASTING
# ===========================================================================
with tab_proc:
    st.header("What happens to COE-90 glass at each temperature")
    st.caption("Typical ranges for Bullseye-compatible (COE-90) glass. Other "
               "glasses — float, COE-96, borosilicate — have their own numbers.")

    def _card(p: dict):
        lo, hi = p["range"]
        rng = f"{lo:g}–{hi:g}°F" if lo <= hi else f"{lo:g} → {hi:g}°F"
        with st.container(border=True):
            st.markdown(f"**{p['name']}** · {rng}")
            st.write(p["what"])
            for tip in p["tips"]:
                st.markdown(f"- {tip}")

    order = ["full_fuse", "contour_fuse", "tack_fuse", "fire_polish", "slump",
             "drape", "casting", "furnace"]
    cols = st.columns(2)
    for i, key in enumerate(order):
        with cols[i % 2]:
            _card(gs.PROCESSES[key])

    st.subheader("Two holds on the way up and down that save pieces")
    c1, c2 = st.columns(2)
    with c1:
        _card(gs.PROCESSES["bubble_squeeze"])
        _card(gs.PROCESSES["crash_cool"])
    with c2:
        _card(gs.PROCESSES["devit"])
        _card(gs.PROCESSES["annealing"])

    st.markdown(
        "**Putting it together** — a full fuse program reads as: heat at a "
        "moderate rate to ~1000°F and equalize; climb into the **bubble "
        "squeeze** and hold; climb to the **fuse** temperature and hold "
        "briefly; **crash-cool** through the **devit window** to the anneal "
        "point; **anneal** for the thickness; cool slowly to the strain "
        "point; then let the kiln cool to room temperature. Every template "
        "on the Programs tab follows that shape.")


# ===========================================================================
# 5. HEAT-WORK & READING YOUR LOGS
# ===========================================================================
with tab_hw:
    st.header("Heat-work")
    st.write(gs.HEAT_WORK_INTRO)
    st.subheader("Why two identical programs give different glass")
    st.write(gs.HEAT_WORK_WHY_DIFFERENT)

    st.subheader("A quick rate calculator")
    h1, h2, h3 = st.columns(3)
    r_from = h1.number_input("From °F", value=70.0, step=10.0, key="learn_r_from")
    r_to = h2.number_input("To °F", value=1000.0, step=10.0, key="learn_r_to")
    r_min = h3.number_input("Minutes", value=140, min_value=1, step=5, key="learn_r_min")
    rate = gs.effective_rate(r_from, r_to, r_min)
    st.markdown(f"That segment asks for **{abs(rate):.0f}°F/hr** "
                f"({'heating' if rate > 0 else 'cooling' if rate < 0 else 'hold'}). "
                f"If the kiln can't deliver it, the real curve lags and the "
                f"guaranteed-soak timer waits.")

    st.subheader("What the Firing Notebook measures")
    st.write(gs.HEAT_WORK_NOTEBOOK)
    st.markdown(
        "- **Didn't reach target** — the peak PV stayed below the segment's "
        "setpoint minus the tolerance.\n"
        "- **Power-limited ramp** — output sat at 100% and the slope was "
        "still below what the program asked; the kiln (or an output limit) "
        "is the bottleneck.\n"
        "- **Short soak** — time within the tolerance band was less than the "
        "programmed hold.\n"
        "- **Dropouts** — gaps in the log where Bluetooth lost the "
        "controller; those minutes are excluded from the heat-work integral "
        "rather than guessed.")

    st.subheader("What the furnace watchdog does")
    st.write(gs.WATCHDOG_PLAIN)
    st.caption("Set the trigger temperature per furnace on the Home page "
               "('Auto-restart trigger'); arm and disarm it there too. The "
               "Recovery page shows the watchdog's history.")
