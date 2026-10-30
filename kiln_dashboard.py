"""
kiln_dashboard.py  —  HOME page
===============================
The front page for the crew:
  * Furnaces (temperature-critical) shown as big temperature cards up top.
  * Kilns shown below with program status and run/stop controls.
  * One Bluetooth control: how often to read, and "hand this controller to
    QuickTune for N minutes" — the monitor steps off that controller only and
    resumes by itself.
  * With no controllers yet, a short walkthrough that starts a scan.

Other pages (sidebar): Devices, Analytics, Settings, Recovery & Logic,
Notebook, Learn, Open Data & API. Shared infrastructure lives in app_core.py
so every page reuses one monitor and one set of Bluetooth connections.
"""
from __future__ import annotations

import time
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import app_core as core
from devices import ROLE_FURNACE, ROLE_OTHER

st.set_page_config(page_title=core.studio_title(), page_icon="🔥", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()
core.init_history(devices)

st.markdown(f"# 🔥 {core.studio_title()}")

# --- First run: no controllers yet -----------------------------------------
if not devices:
    st.info("**No controllers yet.** Novus controllers advertise over Bluetooth; "
            "scan from this machine, identify what you find, and say which one "
            "is the furnace. Nothing is assumed about your shop.")
    c1, c2 = st.columns([1, 2])
    if c1.button("🔍 Find controllers", type="primary", width="stretch"):
        st.switch_page("pages/1_Devices.py")
    c2.caption("Or open **Devices** in the sidebar. You can also add a "
               "controller by its address if you already know it.")
    st.stop()

furnaces = [d for d in devices if d.role == ROLE_FURNACE]
others = [d for d in devices if d.role == ROLE_OTHER]
kilns = [d for d in devices if d not in furnaces and d not in others]
names = [d.name for d in devices]

# --- Top row: status + Bluetooth control left, FURNACE temps right ---------
left, right = st.columns([1, 2])
with left:
    if monitor.last_poll_at:
        age = int(time.time() - monitor.last_poll_at)
        fresh = f"{age}s ago" if age < 90 else f"{age // 60} min ago"
    else:
        fresh = "waiting for first poll…"
    st.caption(f"Reads every {monitor.poll_interval_s/60:g} min · last reading {fresh}")

    b1, b2 = st.columns(2)
    if b1.button("🔄 Read now", width="stretch"):
        with st.spinner("Reading every controller…"):
            try:
                bridge.call(monitor._poll_once(), timeout=90)
            except Exception as e:
                st.warning(f"poll failed: {e}")
        st.rerun()

    if core.is_admin():
        # ---- the one Bluetooth control ----------------------------------------
        _ms = core.load_monitor_settings()
        with b2.popover("📶 Bluetooth", width="stretch",
                        help="Hand a controller to QuickTune, or change how often "
                             "the monitor reads."):
            # Forms, so the 5-second auto-refresh can't swallow a half-made choice:
            # nothing happens until the button is pressed.
            with st.form("bt_pause_form", border=False):
                st.markdown("**Hand a controller to QuickTune**")
                st.caption("A controller talks to one device at a time. While you use "
                           "the phone app, the monitor stays off *that* controller and "
                           "keeps watching the rest, then resumes on its own.")
                who = st.multiselect("Controller(s)", names, placeholder="choose one or more")
                every = st.checkbox("All of them (e.g. calibration, maintenance)")
                mins = st.radio("For how long", [15, 30, 60, 120, 240], index=1,
                                horizontal=True, format_func=lambda m: f"{m} min")
                if st.form_submit_button("⏸ Pause monitoring", type="primary",
                                         width="stretch"):
                    if not (who or every):
                        st.warning("Pick a controller (or All of them) first.")
                    else:
                        if every:
                            core.release_ble(bridge, clients, monitor, minutes=mins)
                        else:
                            for n in who:
                                monitor.pause_device(n, mins)
                                try:
                                    bridge.call(clients[n].disconnect(), timeout=8)
                                except Exception:
                                    pass
                        monitor._write_live()
                        st.rerun()
            st.divider()
            with st.form("bt_settings_form", border=False):
                st.markdown("**How often to read**")
                poll_min = st.slider("Read every (minutes)", 1, 15,
                                     int(round(_ms.get("poll_interval_s", 300) / 60)),
                                     help="Each read holds a controller's Bluetooth for "
                                          "a second or two. Longer = fewer collisions "
                                          "with QuickTune, coarser charts. 3–5 is a good "
                                          "balance; 1 only while troubleshooting.")
                coop = st.toggle("Cooperative (disconnect between reads)",
                                 value=bool(_ms.get("cooperative", True)),
                                 help="Keep this on. Off = the monitor holds connections "
                                      "and QuickTune can never get in.")
                if st.form_submit_button("Save", width="stretch"):
                    core.save_monitor_settings({"cooperative": bool(coop),
                                                "poll_interval_s": float(poll_min * 60)})
                    monitor.cooperative = bool(coop)
                    monitor.poll_interval_s = float(poll_min * 60)
                    st.rerun()

    else:
        b2.caption("👁 read-only")
    paused_names = [n for n in names if monitor.is_paused(n)]
    if monitor.is_paused():
        st.warning("⏸ **All monitoring paused for QuickTune** — resumes "
                   f"{core.fmt_remaining(monitor.pause_remaining_s())}. The "
                   "watchdog is off while paused.")
        if core.is_admin() and st.button("▶ Resume everything now", type="primary",
                                         width="stretch"):
            core.reclaim_ble(bridge, clients, monitor)
            st.rerun()
    elif paused_names:
        st.info("⏸ Handed to QuickTune: " + ", ".join(paused_names)
                + ". Everything else is still watched.")
    if monitor.worried:
        st.warning("👀 Watching closely: " + ", ".join(sorted(monitor.worried)))

with right:
    if furnaces:
        st.markdown("#### Furnaces")
        fcols = st.columns(len(furnaces))
        for fcol, dev in zip(fcols, furnaces):
            s = core.latest_state(monitor, dev)
            core.push_history(dev.name, s)
            unit = core.unit_of(clients, dev.name)
            with fcol:
                pv = f"{s.pv:g}{unit}" if s.pv is not None else "—"
                target = dev.expected_setpoint
                delta = (s.pv - target) if s.pv is not None else None
                st.metric(
                    dev.name, pv,
                    delta=(f"{delta:+.0f}° vs {target:g}" if delta is not None else None),
                    delta_color="inverse",
                )
                if monitor.is_paused(dev.name):
                    st.caption("⏸ handed to QuickTune")
                elif not s.connected:
                    st.caption("⚫ no recent reading")
                elif s.output_pct is not None:
                    st.caption(f"output {s.output_pct:g}% · SP {s.sp:g}{unit}" if s.sp is not None
                               else f"output {s.output_pct:g}%")
    else:
        st.info("No furnace chosen yet. Pick one on the **Devices** page if you "
                "have a controller that should be kept hot and watched.")

st.divider()

st_autorefresh(interval=core.REFRESH_INTERVAL_S * 1000, key="home-refresh")

# --- Kilns -----------------------------------------------------------------
if kilns:
    st.markdown("#### Kilns")
    cols = st.columns(len(kilns))
    for col, dev in zip(cols, kilns):
        s = core.latest_state(monitor, dev)
        core.push_history(dev.name, s)
        core.render_kiln_panel(col, dev, s, bridge, monitor, clients=clients)

# --- Furnace control panels (same controls as kilns) -----------------------
if furnaces:
    st.markdown("#### Furnace controls")
    fcols = st.columns(len(furnaces))
    for col, dev in zip(fcols, furnaces):
        s = core.latest_state(monitor, dev)
        core.render_kiln_panel(col, dev, s, bridge, monitor, is_furnace=True,
                               clients=clients)

# --- Monitor-only devices --------------------------------------------------
if others:
    st.markdown("#### Monitored only")
    ocols = st.columns(len(others))
    for col, dev in zip(ocols, others):
        s = core.latest_state(monitor, dev)
        core.push_history(dev.name, s)
        core.render_kiln_panel(col, dev, s, bridge, monitor, clients=clients)

# --- Footer controls -------------------------------------------------------
st.divider()
c1, c2 = st.columns(2)
with c1:
    if st.button("📋 Load all programs", width="stretch",
                 help="Read every program's segments from all controllers."):
        for d in devices:
            monitor.request_programs[d.name] = True
        with st.spinner("Reading program tables…"):
            try:
                bridge.call(monitor._poll_once(), timeout=180)
            except Exception as e:
                st.warning(f"program read failed: {e}")
        st.rerun()
with c2:
    if st.button("🌐 Open data & API", width="stretch",
                 help="Harvest firings for glassdatabase.org, API status and keys."):
        st.switch_page("pages/7_Open_Data.py")
