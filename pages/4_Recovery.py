"""
Recovery & Logic
================
Everything the furnace/kiln watchdog is doing, made visible and editable:
  * live operational state per controller (armed? worried? recovering? stuck?),
  * every decision threshold and timer, editable and persisted,
  * a manual override to push a controller to any temperature right now,
  * a plain-language readout of exactly what will happen and when.

Nothing here is hidden in code — this page IS the control logic's UI.
"""
from __future__ import annotations

import time
import streamlit as st

import app_core as core
from devices import ROLE_FURNACE

st.set_page_config(page_title="Recovery & Logic", page_icon="🛟", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("🛟 Recovery & Logic")
st.caption("Program 1 is the 'set and forget' hold on every controller. The "
           "watchdog keeps a controller at its hold; it never disturbs a "
           "deliberate firing running on another program.")

# quick auto-refresh so the live state stays current
from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=5000, key="recovery-refresh")


def _fmt_secs(s):
    return f"{s/60:g} min" if s >= 60 else f"{s:g}s"


for dev in devices:
    _uid = dev.address
    cfg = monitor.watchdogs.get(dev.name)
    ws = monitor._watch.get(dev.name)
    s = core.latest_state(monitor, dev)
    armed = monitor.recovery_armed.get(dev.name, True)
    worried = dev.name in getattr(monitor, "worried", set())
    thr = monitor.recover_threshold.get(dev.name)
    low_line = thr if thr is not None else (
        (cfg.expected_setpoint - cfg.low_margin) if cfg else None)

    with st.container(border=True):
        head = "🔥" if dev.role == ROLE_FURNACE else "🪟"
        st.subheader(f"{head} {dev.name}  ·  {dev.role}")

        # ---- live operational state ----
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PV", f"{s.pv:g}°" if s.pv is not None else "—")
        c2.metric("SP", f"{s.sp:g}°" if s.sp is not None else "—")
        c3.metric("Output", f"{s.output_pct:g}%" if s.output_pct is not None else "—")
        c4.metric("Running", "yes" if getattr(s, "running", False) else "no")

        # ---- what state are we in, in plain words ----
        if not cfg or not cfg.enabled:
            st.info("Watchdog **disabled** for this controller.")
        elif ws and ws.needs_human:
            st.error("⚠️ **Needs a human.** The watchdog tried and gave up "
                     "(a restart didn't help, reading below floor, or too many "
                     "restarts). It has STOPPED acting. Fix the hardware, then "
                     "**Re-arm** below.")
        elif not armed:
            st.warning("🔴 **Auto-recovery DISARMED** (manual override). The "
                       "watchdog is watching and alerting but will NOT restart "
                       "this controller until you Re-arm.")
        elif worried:
            st.warning(f"👀 **Worried** — PV below {low_line:g}°. Polling fast and "
                       f"counting down to a restart.")
        else:
            st.success(f"🟢 **Armed & healthy.** Will auto-restart to program "
                       f"{cfg.hold_program} if it "
                       f"{'drops below' if cfg.keep_hot else 'is holding program 1 and drops below'} "
                       f"{low_line:g}°.")

        # internal counters
        det = []
        if ws:
            if ws.low_since:
                det.append(f"low for {(time.time()-ws.low_since)/60:.1f} min")
            if ws.recovering:
                det.append("in post-restart probation")
            if ws.last_good_program:
                det.append(f"last good program {ws.last_good_program}")
            det.append(f"{len(ws.recoveries)} restarts in the last hour")
        if det:
            st.caption(" · ".join(det))

        # ---- arm / disarm ----
        ac1, ac2, ac3 = st.columns(3)
        if armed:
            if ac1.button("🔴 Disarm auto-recovery", key=f"dis_{dev.name}_{_uid}",
                          use_container_width=True):
                monitor.recovery_armed[dev.name] = False
                st.rerun()
        else:
            if ac1.button("🟢 Re-arm auto-recovery", key=f"arm_{dev.name}_{_uid}",
                          type="primary", use_container_width=True):
                monitor.recovery_armed[dev.name] = True
                if ws:
                    ws.needs_human = False
                    ws.last_alerted_case = None
                st.rerun()

        # ---- manual override: push to any temperature now ----
        with ac2.popover("🎛 Manual override", use_container_width=True):
            st.caption("Hold a fixed temperature right now (no program). Use to "
                       "push higher, or hold a custom target. Disarms "
                       "auto-recovery so it won't fight you.")
            tgt = st.number_input("Target °F", min_value=100, max_value=2400,
                                  value=int(s.sp or cfg.expected_setpoint if cfg else 2100),
                                  step=10, key=f"ovr_{dev.name}_{_uid}")
            if st.button("Apply override", key=f"ovrgo_{dev.name}_{_uid}", type="primary"):
                with st.spinner("Grabbing Bluetooth and setting…"):
                    try:
                        bridge.call(monitor.control(dev.name, "set_manual_setpoint",
                                                    float(tgt)), timeout=30)
                        monitor.recovery_armed[dev.name] = False
                        st.success(f"Holding {int(tgt)}° manually. Auto-recovery "
                                   "disarmed — Re-arm when you go back to program 1.")
                    except Exception as e:
                        st.error(f"override failed: {e}")

        with ac3.popover("▶ Run a program", use_container_width=True):
            prog = st.selectbox("Program", range(1, 21), key=f"rp_{dev.name}_{_uid}")
            if st.button("Run", key=f"rpgo_{dev.name}_{_uid}", type="primary"):
                with st.spinner("Starting…"):
                    try:
                        bridge.call(monitor.control(dev.name, "run_program", int(prog)),
                                    timeout=30)
                        monitor.recovery_armed[dev.name] = (int(prog) == (cfg.hold_program if cfg else 1))
                        st.success(f"Running program {prog}.")
                    except Exception as e:
                        st.error(f"run failed: {e}")

        # ---- editable logic ----
        if cfg:
            with st.expander("⚙️ Edit this controller's recovery logic"):
                e1, e2, e3 = st.columns(3)
                cfg.enabled = e1.checkbox("Watchdog enabled", value=cfg.enabled,
                                          key=f"en_{dev.name}_{_uid}")
                cfg.keep_hot = e2.checkbox(
                    "Keep hot from any state", value=cfg.keep_hot, key=f"kh_{dev.name}_{_uid}",
                    help="On: restart even when stopped (a furnace, or a kiln you're "
                         "parking warm). Off: only rescue an active program-1 hold.")
                cfg.recover_on_cant_hold = e3.checkbox(
                    "Restart on 'can't hold'", value=cfg.recover_on_cant_hold,
                    key=f"ch_{dev.name}_{_uid}",
                    help="Restart when output is pinned but temperature is falling "
                         "(a re-latchable SSR/contactor fault).")

                f1, f2, f3 = st.columns(3)
                cfg.expected_setpoint = f1.number_input(
                    "Hold target °F", value=float(cfg.expected_setpoint), step=10.0,
                    key=f"es_{dev.name}_{_uid}")
                new_thr = f2.number_input(
                    "Restart if below °F", value=float(low_line or 0), step=10.0,
                    key=f"th_{dev.name}_{_uid}",
                    help="The trigger temperature. Below this (and not recovering) "
                         "→ restart.")
                cfg.hold_program = f3.number_input(
                    "Hold program #", min_value=1, max_value=20,
                    value=int(cfg.hold_program or 1), key=f"hp_{dev.name}_{_uid}")

                g1, g2, g3 = st.columns(3)
                cfg.dropout_confirm_s = g1.number_input(
                    "Dropout confirm (s)", value=float(cfg.dropout_confirm_s),
                    step=10.0, key=f"dc_{dev.name}_{_uid}",
                    help="Stopped + low: wait this long, then restart.")
                cfg.cant_hold_confirm_s = g2.number_input(
                    "Can't-hold confirm (s)", value=float(cfg.cant_hold_confirm_s),
                    step=10.0, key=f"cc_{dev.name}_{_uid}",
                    help="Running, pinned, falling: wait this long, then restart.")
                cfg.confirm_after_s = g3.number_input(
                    "Recovery confirm window (s)", value=float(cfg.confirm_after_s),
                    step=10.0, key=f"ca_{dev.name}_{_uid}",
                    help="After a restart, temperature must rise within this long.")

                h1, h2, h3 = st.columns(3)
                cfg.recover_floor = h1.number_input(
                    "Don't auto-drive below °F", value=float(cfg.recover_floor),
                    step=50.0, key=f"rf_{dev.name}_{_uid}",
                    help="Below this the reading is too low/uncertain to trust — "
                         "alert a human instead of driving.")
                cfg.max_recoveries = int(h2.number_input(
                    "Max restarts / hour", min_value=1, max_value=30,
                    value=int(cfg.max_recoveries), key=f"mr_{dev.name}_{_uid}",
                    help="After this many in an hour, give up and stop (recurring "
                         "fault needs a human)."))
                cfg.low_margin = h3.number_input(
                    "Low margin °F (if no trigger set)", value=float(cfg.low_margin),
                    step=10.0, key=f"lm_{dev.name}_{_uid}")

                if st.button("💾 Save this controller's logic", key=f"sv_{dev.name}_{_uid}",
                             type="primary"):
                    monitor.recover_threshold[dev.name] = float(new_thr)
                    core.save_watchdog_settings(monitor)
                    if ws:
                        ws.last_alerted_case = None
                    st.success("Saved and applied. (Persists across restarts.)")
                    st.rerun()

st.divider()
st.subheader("Recent events")
st.caption("The watchdog writes every detection, action, and confirmation to "
           "`logs/<controller>/events.log` — independent of notifications.")
import pathlib
picked = st.selectbox("Controller", [d.name for d in devices], key="evt_dev")
evt = pathlib.Path(core.LOG_DIR) / picked / "events.log"
if evt.exists():
    lines = evt.read_text().splitlines()[-40:]
    st.code("\n".join(lines) or "(empty)", language="text")
else:
    st.caption("No events logged yet.")
