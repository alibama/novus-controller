"""
kiln_dashboard.py  —  HOME page
===============================
Simple front page for the crew:
  * Furnaces (temperature-critical) shown as big temp cards, top-right.
  * Kilns shown below with program status and run/stop controls.

Analytics and Settings are separate pages (see the pages/ folder).
Shared infrastructure lives in app_core.py so every page reuses one monitor.
"""
from __future__ import annotations

import time
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import app_core as core
from devices import ROLE_FURNACE

st.set_page_config(page_title="Crozet Glass Kilns", page_icon="🔥", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()
core.init_history(devices)

st.markdown("# 🔥 Crozet Glass")

# --- Released state --------------------------------------------------------
if runtime["ble_released"]:
    st.warning("**BLE released — controllers are free for QuickTune Mobile.** "
               "Monitoring is paused.")
    if st.button("🔌 Take BLE back & resume monitoring", type="primary",
                 use_container_width=True):
        with st.spinner("Reconnecting…"):
            core.reclaim_ble(bridge, clients, monitor)
        st.rerun()
    st.stop()

# --- Top row: title left, FURNACE temps top-right --------------------------
furnaces = [d for d in devices
            if d.role == ROLE_FURNACE
            or st.session_state.get(f"promote_{d.name}")]
kilns = [d for d in devices if d not in furnaces]

left, right = st.columns([1, 2])
with left:
    if monitor.last_poll_at:
        age = int(time.time() - monitor.last_poll_at)
        fresh = f"{age}s ago" if age < 90 else f"{age // 60} min ago"
    else:
        fresh = "waiting…"
    st.caption(f"Polls every {core.POLL_INTERVAL_S/60:g} min · last reading {fresh}")
    if st.button("🔄 Poll now", use_container_width=True):
        with st.spinner("Polling…"):
            try:
                bridge.call(monitor._poll_once(), timeout=60)
            except Exception as e:
                st.warning(f"poll failed: {e}")
        st.rerun()

with right:
    if furnaces:
        st.markdown("#### Furnaces")
        fcols = st.columns(len(furnaces))
        for fcol, dev in zip(fcols, furnaces):
            s = core.latest_state(monitor, dev)
            core.push_history(dev.name, s)
            with fcol:
                pv = f"{s.pv:g}°" if s.pv is not None else "—"
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
                    st.caption(f"output {s.output_pct:g}% · SP {s.sp:g}°" if s.sp is not None
                               else f"output {s.output_pct:g}%")
    else:
        st.info("No furnaces configured. Assign one on the Settings page.")

st.divider()

st_autorefresh(interval=core.REFRESH_INTERVAL_S * 1000, key="home-refresh")

# --- Kilns -----------------------------------------------------------------
st.markdown("#### Kilns")
if kilns:
    cols = st.columns(len(kilns))
    for col, dev in zip(cols, kilns):
        s = core.latest_state(monitor, dev)
        core.push_history(dev.name, s)
        core.render_kiln_panel(col, dev, s, bridge, monitor)
else:
    st.caption("No kilns configured.")

# --- Furnace control panels (same controls as kilns) -----------------------
if furnaces:
    st.markdown("#### Furnace controls")
    fcols = st.columns(len(furnaces))
    for col, dev in zip(fcols, furnaces):
        s = core.latest_state(monitor, dev)
        core.render_kiln_panel(col, dev, s, bridge, monitor, is_furnace=True)

# --- Footer controls -------------------------------------------------------
st.divider()
c1, c2 = st.columns(2)
with c1:
    if st.button("📋 Load all programs", use_container_width=True,
                 help="Read every program's segments from all controllers."):
        for d in devices:
            monitor.request_programs[d.name] = True
        with st.spinner("Reading program tables…"):
            try:
                bridge.call(monitor._poll_once(), timeout=120)
            except Exception as e:
                st.warning(f"program read failed: {e}")
        st.rerun()
with c2:
    if st.button("📲 Release BLE for QuickTune", use_container_width=True,
                 help="Pause polling and free Bluetooth for QuickTune."):
        with st.spinner("Releasing Bluetooth…"):
            core.release_ble(bridge, clients, monitor)
        st.rerun()

# --- Open Data Studio ------------------------------------------------------
st.divider()
with st.container(border=True):
    st.subheader("🌐 Open Data Studio")
    ss = core.load_studio_settings()
    studio_name = ss.get("studio") or "This studio"
    st.caption(f"{studio_name} publishes its kiln & furnace operating data as "
               "open data (CC-BY-4.0) so anyone can study how real studios use "
               "energy. Harvest it below — one row per firing, plus the raw "
               "telemetry. Feeds a cost estimator and pipes straight into "
               "glassdatabase.org.")

    try:
        rows = core.build_usage_records()
    except Exception as e:
        rows = []
        st.caption(f"(usage build unavailable: {e})")

    if rows:
        import usage as _usage
        total_kwh = sum(r["energy_kwh"] or 0 for r in rows)
        total_cost = sum(r["cost"] or 0 for r in rows)
        cur = next((r["currency"] for r in rows if r.get("currency")), "")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Firings logged", len(rows))
        m2.metric("Energy (est.)", f"{total_kwh:,.0f} kWh")
        m3.metric("Cost (est.)", f"{total_cost:,.2f} {cur}" if total_cost else "—")
        m4.metric("Since", min(r["date_utc"] for r in rows if r["date_utc"]) or "—")

        d1, d2, d3 = st.columns(3)
        d1.download_button("⬇ Firings (CSV)", _usage.to_csv(rows),
                           file_name="kiln_firings.csv", mime="text/csv",
                           use_container_width=True)
        d2.download_button("⬇ Firings (XLSX · glass-database ready)",
                           core.usage_xlsx_bytes(rows),
                           file_name="kiln_firings.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
        d3.download_button("⬇ Raw telemetry (ZIP of CSVs)",
                           core.telemetry_zip_bytes(),
                           file_name="kiln_telemetry.zip", mime="application/zip",
                           use_container_width=True)
        with st.expander("Pipe into glassdatabase.org"):
            st.markdown(
                "1. Download **Firings (XLSX)** above.\n"
                "2. Drop it in the importer's uploads folder and build:\n"
                "   ```\n"
                "   python -m central.ingest build --uploads /path/to/folder\n"
                "   ```\n"
                "3. It registers as a browsable dataset (chart/map/download) in "
                "the Explore app. The energy numbers are estimates from output% "
                "× rated kW — calibrate each controller's kW in Settings for the "
                "truest cost. Set your studio name and electricity rate in "
                "Settings too.")
        with st.expander("Machine-readable open API"):
            st.markdown(
                "There's also a read-only HTTP API (CC-BY, CORS-open, no device "
                "addresses) for programmatic access — `/firings`, `/summary`, "
                "`/devices`, `/firings.csv`. Run it alongside the dashboard:\n"
                "```\n"
                "pip install -r requirements-api.txt\n"
                "uvicorn api:app --host 0.0.0.0 --port 8000\n"
                "```\n"
                "See `docs/API.md`. Expose it read-only via your reverse proxy "
                "or a Tailscale Funnel on a separate port.")
    else:
        st.info("No completed firings logged yet — run a program and the harvest "
                "fills in. Set your studio name, electricity rate, and each "
                "controller's rated kW under **Settings → Energy & open data**.")
