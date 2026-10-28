"""
kiln_dashboard.py  —  HOME page
===============================
The front page for the crew:
  * Furnaces (temperature-critical) shown as big temperature cards up top.
  * Kilns shown below with program status and run/stop controls.
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

# --- Released state --------------------------------------------------------
if runtime["ble_released"]:
    st.warning("**Bluetooth released — controllers are free for QuickTune Mobile.** "
               "Monitoring is paused.")
    if st.button("🔌 Take Bluetooth back & resume monitoring", type="primary",
                 width="stretch"):
        core.reclaim_ble(bridge, clients, monitor)
        st.rerun()
    st.stop()

# --- Top row: poll status left, FURNACE temps right ------------------------
furnaces = [d for d in devices if d.role == ROLE_FURNACE]
others = [d for d in devices if d.role == ROLE_OTHER]
kilns = [d for d in devices if d not in furnaces and d not in others]

left, right = st.columns([1, 2])
with left:
    if monitor.last_poll_at:
        age = int(time.time() - monitor.last_poll_at)
        fresh = f"{age}s ago" if age < 90 else f"{age // 60} min ago"
    else:
        fresh = "waiting for first poll…"
    st.caption(f"Reads every {monitor.poll_interval_s/60:g} min · last reading {fresh}")
    if st.button("🔄 Read now", width="stretch"):
        with st.spinner("Reading every controller…"):
            try:
                bridge.call(monitor._poll_once(), timeout=90)
            except Exception as e:
                st.warning(f"poll failed: {e}")
        st.rerun()
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
                if not s.connected:
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
c1, c2, c3 = st.columns(3)
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
    if st.button("📲 Release Bluetooth for QuickTune", width="stretch",
                 help="Pause polling and free Bluetooth for QuickTune."):
        with st.spinner("Releasing Bluetooth…"):
            core.release_ble(bridge, clients, monitor)
        st.rerun()
with c3:
    if st.button("🌐 Open data & API", width="stretch",
                 help="Harvest firings for glassdatabase.org, API status and keys."):
        st.switch_page("pages/7_Open_Data.py")
