"""
Novus Kiln Controller BT Scanner
--------------------------------
Streamlit app for discovering Bluetooth Low Energy devices, with focus on
Novus-brand kiln/process controllers (QuickTune Mobile compatible).

Designed to run ON the machine with the Bluetooth radio 
and be accessed over Tailscale from anywhere.

Setup (on the shop laptop):
    pip install streamlit bleak

Run:
    streamlit run kiln_bt_scanner.py --server.address 0.0.0.0 --server.port 8501


"""

import asyncio
import platform
import socket
from datetime import datetime

import streamlit as st
from bleak import BleakScanner

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Kiln BT Scanner",
    page_icon="🔥",
    layout="wide",
)

st.title("🔥 Novus Kiln Controller Scanner")
st.caption(
    f"Scanning from **{socket.gethostname()}** · "
    f"{platform.system()} {platform.release()}"
)

# ---------------------------------------------------------------------------
# Novus heuristics
# ---------------------------------------------------------------------------
# Hint strings to flag a discovered device as "likely Novus".
# Add more as you observe what your specific controllers advertise.
NOVUS_NAME_HINTS = (
    "novus",
    "quicktune",
    "n1200",
    "n1040",
    "n2000",
    "n3204",
    "n480",
)


def looks_like_novus(name: str, service_uuids, mfg_data) -> bool:
    """Best-effort guess whether a BLE advertisement is from a Novus device."""
    n = (name or "").lower()
    if any(hint in n for hint in NOVUS_NAME_HINTS):
        return True
    for uuid in service_uuids or []:
        if any(hint in str(uuid).lower() for hint in NOVUS_NAME_HINTS):
            return True
    # If you discover Novus's BT SIG manufacturer ID, add a check here:
    # if 0xXXXX in (mfg_data or {}): return True
    return False


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
async def scan_ble(duration: int):
    """Run a BLE scan and return a list of normalized device dicts."""
    found = await BleakScanner.discover(timeout=duration, return_adv=True)
    rows = []
    for addr, (device, adv) in found.items():
        name = device.name or adv.local_name or "(unnamed)"
        rows.append(
            {
                "name": name,
                "address": addr,
                "rssi": adv.rssi,
                "tx_power": adv.tx_power,
                "service_uuids": list(adv.service_uuids or []),
                "mfg_data": {
                    k: v.hex() for k, v in (adv.manufacturer_data or {}).items()
                },
                "is_novus": looks_like_novus(
                    name, adv.service_uuids, adv.manufacturer_data
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    scan_duration = st.slider("Scan duration (s)", 3, 30, 10)
with c2:
    novus_only = st.checkbox("Likely Novus only", value=False)
with c3:
    name_filter = st.text_input("Filter: name contains", "").strip().lower()

if st.button("🔍 Scan now", type="primary", use_container_width=True):
    with st.spinner(f"Scanning for {scan_duration}s…"):
        try:
            results = asyncio.run(scan_ble(scan_duration))
        except Exception as e:
            st.error(f"Scan failed: {e}")
            st.info(
                "Checks: Bluetooth radio enabled? "
                "On Linux, the python process may need cap_net_raw or root. "
                "On Windows, ensure no other app holds an exclusive BLE session."
            )
            results = []
    st.session_state["last_scan"] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
if "last_scan" in st.session_state:
    data = st.session_state["last_scan"]
    results = data["results"]
    st.caption(f"Last scan: {data['ts']} · {len(results)} device(s) seen")

    # Filter
    filtered = results
    if novus_only:
        filtered = [r for r in filtered if r["is_novus"]]
    if name_filter:
        filtered = [r for r in filtered if name_filter in r["name"].lower()]

    novus_count = sum(1 for r in filtered if r["is_novus"])
    if novus_count:
        st.success(f"✓ {novus_count} likely Novus device(s) detected")
    elif results and not novus_only:
        st.warning(
            "No obvious Novus devices in this scan. "
            "Check the full list below — they may advertise under a generic name "
            "like the controller model number, or with no name at all."
        )

    # Sort: Novus first, then by RSSI (strongest first)
    filtered.sort(
        key=lambda r: (
            not r["is_novus"],
            -(r["rssi"] if r["rssi"] is not None else -200),
        )
    )

    if not filtered:
        st.info("No devices match the current filters.")
    else:
        table = [
            {
                "🎯": "✓" if r["is_novus"] else "",
                "Name": r["name"],
                "Address": r["address"],
                "RSSI (dBm)": r["rssi"],
                "Service UUIDs": ", ".join(r["service_uuids"]) or "—",
                "Mfg Data (hex)": (
                    ", ".join(f"id={k}:{v}" for k, v in r["mfg_data"].items())
                    or "—"
                ),
            }
            for r in filtered
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)

        with st.expander("🔧 Raw advertisement data"):
            st.json(results)
else:
    st.info("Click **Scan now** to discover nearby Bluetooth Low Energy devices.")

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Notes")
    st.markdown(
        """
**To pick up a Novus controller:**
- Bluetooth must be enabled on the controller (check front panel menu)
- Disconnect QuickTune Mobile on your phone first — most BLE controllers
  only allow one active connection at a time, and an existing pairing can
  prevent the device from advertising
- Move within ~5 m for the first scan to confirm discovery

**Reading RSSI:**
- `> -70 dBm` strong
- `-70 to -90` workable
- `< -90` unreliable

**Next steps after discovery:**
Once you confirm the address(es), you can use `bleak` to connect, list GATT
services/characteristics, and read/write controller registers — that's the
foundation for replacing or augmenting QuickTune Mobile.
        """
    )
