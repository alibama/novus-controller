"""
kiln_dashboard.py
=================
Streamlit dashboard for the Raging Glass kilns (bubba, freezy, calliope).

Live PV/SP/output, run/stop controls, auto-refresh. BLE connections are
held open across Streamlit reruns via st.cache_resource and an asyncio
loop running in a background thread.

Run:
    cd ~/kiln-bt-scanner
    source .venv/bin/activate
    streamlit run kiln_dashboard.py

Then open the URL it prints. Optionally expose over Tailscale:
    sudo tailscale serve --bg --https=443 http://localhost:8501
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Optional

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from novus_client import NovusClient, ControllerState


# ---------------------------------------------------------------------------
# Configuration: your three kilns
# ---------------------------------------------------------------------------
KILNS: list[tuple[str, str]] = [
    ("bubba",    "00:26:A4:00:5F:4E"),
    ("freezy",   "00:26:A4:00:7A:40"),
    ("calliope", "00:26:A4:00:89:42"),
]

REFRESH_INTERVAL_S = 5
HISTORY_LEN = 240    # ~20 min at 5s refresh


# ---------------------------------------------------------------------------
# Async bridge: a forever-running asyncio loop in a daemon thread,
# accessible from sync Streamlit code via run_coroutine_threadsafe.
# ---------------------------------------------------------------------------

class AsyncBridge:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="kiln-async-loop")
        self.thread.start()

    def call(self, coro, timeout: float = 10.0):
        """Run a coroutine on the bridge's loop and block until it returns."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


@st.cache_resource
def get_bridge() -> AsyncBridge:
    return AsyncBridge()


@st.cache_resource
def get_clients(_bridge: AsyncBridge) -> dict[str, NovusClient]:
    """Create one NovusClient per kiln. Connections are attempted but
    failures don't abort — disconnected kilns just show as offline."""
    clients: dict[str, NovusClient] = {}
    for name, addr in KILNS:
        c = NovusClient(name, addr)
        try:
            _bridge.call(c.connect(), timeout=15)
            print(f"[{name}] connected")
        except Exception as e:
            print(f"[{name}] connect failed: {e}")
        clients[name] = c
    return clients


# ---------------------------------------------------------------------------
# History buffer for PV/SP charting (kept in session state)
# ---------------------------------------------------------------------------

def init_history():
    if "history" not in st.session_state:
        st.session_state.history = {
            name: deque(maxlen=HISTORY_LEN) for name, _ in KILNS
        }
    if "started_at" not in st.session_state:
        st.session_state.started_at = time.time()


def push_history(name: str, state: ControllerState):
    if state.connected and state.pv is not None and state.sp is not None:
        st.session_state.history[name].append({
            "t": time.time() - st.session_state.started_at,
            "PV": state.pv,
            "SP": state.sp,
        })


# ---------------------------------------------------------------------------
# State refresh
# ---------------------------------------------------------------------------

def read_all_states(bridge: AsyncBridge,
                    clients: dict[str, NovusClient]) -> dict[str, ControllerState]:
    states: dict[str, ControllerState] = {}
    for name, client in clients.items():
        try:
            state = bridge.call(client.read_state(), timeout=5)
        except Exception as e:
            print(f"[{name}] read failed: {e}")
            state = ControllerState(name=name, address=client.address,
                                    connected=False, pv=None, sp=None)
            # Try to reconnect in the background; don't block the UI on it
            try:
                bridge.call(client.connect(), timeout=8)
            except Exception:
                pass
        states[name] = state
    return states


# ---------------------------------------------------------------------------
# Per-kiln panel
# ---------------------------------------------------------------------------

def render_kiln_panel(col, name: str, state: ControllerState,
                      bridge: AsyncBridge, client: NovusClient):
    with col:
        # Header with status indicator
        if not state.connected:
            st.subheader(f"⚫ {name}")
            st.error("disconnected")
            if st.button("reconnect", key=f"recon_{name}"):
                try:
                    bridge.call(client.connect(), timeout=10)
                    st.rerun()
                except Exception as e:
                    st.warning(f"reconnect failed: {e}")
            return

        if state.active_program:
            st.subheader(f"🔥 {name}")
        else:
            st.subheader(f"⚪ {name}")

        # Big metrics
        m1, m2, m3 = st.columns(3)
        m1.metric("PV", f"{state.pv:g}°" if state.pv is not None else "—")
        m2.metric("SP", f"{state.sp:g}°" if state.sp is not None else "—")
        m3.metric("Output",
                  f"{state.output_pct:g}%" if state.output_pct is not None else "—")

        # Program state
        if state.active_program:
            seg = (state.active_segment
                   if state.active_segment is not None else "?")
            st.success(f"**Running program {state.active_program}** · segment {seg}")
            if st.button("⏹ STOP", key=f"stop_{name}", use_container_width=True):
                try:
                    bridge.call(client.stop_program(), timeout=5)
                    time.sleep(0.3)
                    st.rerun()
                except Exception as e:
                    st.error(f"stop failed: {e}")
        else:
            st.info("idle")
            run_col1, run_col2 = st.columns([2, 1])
            with run_col1:
                program = st.selectbox("Program", range(1, 21),
                                       key=f"prog_{name}",
                                       label_visibility="collapsed")
            with run_col2:
                if st.button("▶ RUN", key=f"start_{name}", use_container_width=True):
                    try:
                        bridge.call(client.run_program(program), timeout=5)
                        time.sleep(0.3)
                        st.rerun()
                    except Exception as e:
                        st.error(f"start failed: {e}")

        # PV/SP history sparkline
        history = list(st.session_state.history[name])
        if len(history) >= 2:
            import pandas as pd
            df = pd.DataFrame(history).set_index("t")
            st.line_chart(df, height=140)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Crozet Glass Kilns", page_icon="🔥", layout="wide")

st.markdown("# 🔥 Crozet Glass — Kiln Dashboard")
st.caption(
    f"Auto-refresh every {REFRESH_INTERVAL_S}s · "
    f"{len(KILNS)} controllers · BLE via Bleak"
)

st_autorefresh(interval=REFRESH_INTERVAL_S * 1000, key="kiln-refresh")

init_history()
bridge = get_bridge()
clients = get_clients(bridge)
states = read_all_states(bridge, clients)
for name, state in states.items():
    push_history(name, state)

cols = st.columns(len(KILNS))
for col, (name, _) in zip(cols, KILNS):
    render_kiln_panel(col, name, states[name], bridge, clients[name])

# Footer
with st.expander("Diagnostics"):
    rows = []
    for name, state in states.items():
        rows.append({
            "name": name,
            "address": state.address,
            "connected": "✓" if state.connected else "✗",
            "PV": state.pv,
            "SP": state.sp,
            "output%": state.output_pct,
            "active_program": state.active_program,
            "active_segment": state.active_segment,
        })
    import pandas as pd
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
