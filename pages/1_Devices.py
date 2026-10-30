"""
Devices
=======
Find controllers over Bluetooth, identify what they are, add them, and say what
job each one does (furnace / kiln / other). Nothing here assumes a particular
shop: start from an empty list, scan, add. Changes apply LIVE — no restart.
"""
from __future__ import annotations

import re

import streamlit as st

import app_core as core
import novus_models
from devices import (load_devices, save_devices, new_device, validate,
                     Device, ROLE_FURNACE, ROLE_KILN, ROLE_OTHER, ROLES,
                     DEFAULT_SETPOINT)

st.set_page_config(page_title="Devices", page_icon="📡", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("📡 Devices")
st.caption("Search for Novus controllers over Bluetooth, identify them, and "
           "choose each one's job. This app is built and verified on the "
           "**N20K48** (a PID controller with Ramp & Soak programs); other Novus "
           "models are found and read but kept read-only until you say otherwise.")

MAC_RE = re.compile(r"([0-9A-F]{2}:){5}[0-9A-F]{2}")
ROLE_LABEL = {ROLE_KILN: "Kiln (runs programs)",
              ROLE_FURNACE: "Furnace (keep hot, watch & recover)",
              ROLE_OTHER: "Other (monitor only)"}

if not core.is_admin():
    core.viewer_note("Scanning, adding, editing and diagnosing controllers")
    if not devices:
        st.caption("No controllers configured yet.")
    for d in devices:
        s = core.latest_state(monitor, d)
        head = {"furnace": "🔥", "kiln": "🪟", "other": "📟"}.get(d.role, "📟")
        live = (f"PV {s.pv:g}{core.unit_of(clients, d.name)} · SP {s.sp:g} · "
                f"output {s.output_pct:g}%" if s.connected and s.pv is not None
                else "no recent reading")
        st.markdown(f"- {head} **{d.name}** — {ROLE_LABEL.get(d.role, d.role)} · "
                    f"{novus_models.describe(d.model)} · {live}"
                    + ("" if d.writes_allowed else " · read-only"))
    st.stop()


def _apply_and_rerun(devs: list[Device], msg: str):
    problems = validate(devs)
    if problems:
        st.error("Not saved — fix these first: " + "; ".join(problems))
        return
    save_devices(devs)
    with st.spinner("Applying to the live monitor…"):
        try:
            core.reload_devices(bridge, clients, monitor)
        except Exception as e:
            st.warning(f"Saved, but the live monitor could not be updated: {e}. "
                       "Restart the service to apply.")
    st.success(msg)
    st.rerun()


# ===========================================================================
# 1. SEARCH
# ===========================================================================
st.header("1 · Search for controllers")
st.caption("Scanning borrows the Bluetooth radio for a few seconds, so "
           "monitoring pauses and resumes by itself. A controller that is "
           "**already connected** — to this app or to QuickTune on a phone — "
           "does not advertise, so it won't show up; disconnect it and rescan.")

s1, s2 = st.columns([3, 1])
sc_secs = s1.slider("Scan seconds", 5, 30, 10, key="scan_secs",
                    help="Longer scans catch controllers that advertise slowly.")
show_all = s2.toggle("Show non-Novus too", value=False,
                     help="Every BLE device in range, not just ones that look like Novus.")

if st.button(f"🔍 Scan for {sc_secs} seconds", type="primary", key="scan_go",
             disabled=st.session_state.get("scanning", False)):
    st.session_state["scanning"] = True
    with st.spinner("Pausing monitor and scanning…"):
        core.release_ble(bridge, clients, monitor)
        try:
            results = bridge.call(monitor.scan_now(float(sc_secs)),
                                  timeout=float(sc_secs) + 40)
            st.session_state["scan_results"] = results
            st.session_state.pop("ident", None)
        except Exception as e:
            msg = str(e).lower()
            if "in progress" in msg or "inprogress" in msg:
                st.error("Bluetooth is still finishing a previous scan. Wait a "
                         "few seconds and try again. If it persists, restart the "
                         "Bluetooth service (`sudo systemctl restart bluetooth`).")
            elif "powered" in msg or "not available" in msg or "no adapter" in msg \
                    or "bluetooth" in msg and "off" in msg:
                st.error("**Bluetooth is off or missing on this machine.** Turn the "
                         "adapter on (Linux: `rfkill unblock bluetooth && "
                         "bluetoothctl power on`; check `bluetoothctl show` says "
                         "*Powered: yes*), then scan again.")
            else:
                st.error(f"scan failed: {e}")
        finally:
            core.reclaim_ble(bridge, clients, monitor)
            st.session_state["scanning"] = False

results = st.session_state.get("scan_results")
known_addr = {d.address.upper(): d for d in devices}

if results is not None and not any(r["novus"] for r in results):
    st.warning(
        "No Novus controllers found"
        + (f" ({len(results)} other BLE device(s) seen)." if results else ".")
        + "\n\n- Is Bluetooth enabled on the controller's front panel?\n"
          "- Is it connected to QuickTune or another app right now? Disconnect it.\n"
          "- On the host: `rfkill unblock bluetooth`, then `bluetoothctl show` "
          "should say *Powered: yes*.\n"
          "- Try a longer scan and move within a few metres.\n\n"
          "You can also add a controller by address below (QuickTune shows it).")
elif results:
    shown = results if show_all else [r for r in results if r["novus"]]
    st.write(f"Found **{sum(1 for r in results if r['novus'])}** Novus-looking "
             f"controller(s) among {len(results)} BLE device(s).")
    ident = st.session_state.setdefault("ident", {})
    for r in shown:
        addr = r["address"]
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1.2, 1.2])
            tag = "🟢 Novus" if r["novus"] else "⚪ other"
            name_adv = r["name_adv"] or "(no name)"
            why = ", ".join(r.get("reasons") or []) or "no Novus fingerprint"
            guess = r.get("model_guess") or ""
            known = known_addr.get(addr)
            c1.markdown(f"{tag} · `{addr}` · **{name_adv}** · RSSI {r['rssi']}"
                        + (f" · looks like **{guess}**" if guess else "")
                        + (f" · already added as **{known.name}**" if known else ""))
            c1.caption(why)
            info = ident.get(addr)
            if info:
                if info.get("error") and not info.get("product"):
                    c1.error(f"identify failed: {info['error']}")
                else:
                    model = info.get("model") or guess or "?"
                    c1.success(
                        f"**{info.get('vendor') or 'Novus'}** · {info.get('product') or '?'}"
                        f" · firmware {info.get('revision') or '?'} → "
                        f"**{novus_models.describe(model)}**"
                        + (f" · PV {info['pv']:g}{info.get('unit','')}" if info.get("pv") is not None else "")
                        + (f" · SP {info['sp']:g}" if info.get("sp") is not None else ""))
                    if info.get("error"):
                        c1.caption(info["error"])
            if c2.button("🔎 Identify", key=f"id_{addr}", width="stretch",
                         help="Connect briefly and read the model, firmware and "
                              "current temperature. Read-only."):
                with st.spinner(f"Connecting to {addr}…"):
                    core.release_ble(bridge, clients, monitor)
                    try:
                        ident[addr] = bridge.call(monitor.identify_now(addr), timeout=45)
                    except Exception as e:
                        ident[addr] = {"error": str(e)}
                    finally:
                        core.reclaim_ble(bridge, clients, monitor)
                st.rerun()
            if not known:
                with c3.popover("➕ Add", width="stretch"):
                    info = ident.get(addr) or {}
                    model = (info.get("model") or guess or "").upper()
                    nm = st.text_input("Name", value=(model.lower() if model else "")
                                       + f"-{addr[-5:].replace(':', '')}",
                                       key=f"nm_{addr}")
                    role = st.selectbox("Job", ROLES, format_func=ROLE_LABEL.get,
                                        key=f"rl_{addr}")
                    model_in = st.text_input("Model", value=model or "N20K48",
                                             key=f"md_{addr}",
                                             help="Press Identify to read it from the "
                                                  "controller instead of guessing.")
                    if st.button("Add this controller", type="primary",
                                 key=f"addgo_{addr}"):
                        devs = load_devices()
                        if any(x.name == nm.strip() for x in devs):
                            st.error("That name is taken — pick a unique one.")
                        else:
                            d = new_device(nm, addr, role, model=model_in,
                                           product=info.get("product", ""),
                                           firmware=info.get("revision", ""))
                            devs.append(d)
                            _apply_and_rerun(devs, f"Added {d.name} ({d.model}). "
                                             "Monitoring starts on the next poll.")

with st.expander("➕ Add a controller by address (no scan needed)"):
    st.caption("Get the MAC from QuickTune Mobile's device list or the label on "
               "the Bluetooth module. Format `00:26:A4:xx:xx:xx`. Press "
               "*Identify* afterwards to confirm the model.")
    a1, a2, a3, a4 = st.columns([2, 3, 2, 2])
    m_name = a1.text_input("Name", key="madd_name")
    m_addr = a2.text_input("BLE address", key="madd_addr")
    m_role = a3.selectbox("Job", ROLES, format_func=ROLE_LABEL.get, key="madd_role")
    m_model = a4.text_input("Model", value="N20K48", key="madd_model")
    if st.button("Add controller", key="madd_go"):
        addr = m_addr.strip().upper()
        devs2 = load_devices()
        if not m_name.strip() or not MAC_RE.fullmatch(addr):
            st.error("Enter a name and a valid BLE address "
                     "(six hex pairs, e.g. 00:26:A4:1A:2B:3C).")
        elif any(x.address.upper() == addr for x in devs2):
            st.warning("That address is already in your list.")
        elif any(x.name == m_name.strip() for x in devs2):
            st.warning(f"A device named '{m_name.strip()}' already exists.")
        else:
            d = new_device(m_name, addr, m_role, model=m_model)
            devs2.append(d)
            _apply_and_rerun(devs2, f"Added {d.name} ({addr}).")

st.divider()

# ===========================================================================
# 2. YOUR CONTROLLERS
# ===========================================================================
st.header("2 · Your controllers")
devs = load_devices()
if not devs:
    st.info("No controllers yet. Scan above, or add one by address.")
    st.stop()

problems = validate(devs)
if problems:
    st.error("⚠️ " + "; ".join(problems) + ". Fix below and save.")

# --- the furnace picker: one simple choice -------------------------------
st.subheader("Which one is the furnace?")
st.caption("The furnace is the controller you want kept hot around the clock: "
           "it gets the watchdog, the big temperature card, and auto-recovery. "
           "Everything else runs programs (kiln) or is just watched (other). "
           "You can pick more than one furnace if you have them.")
fnames = [d.name for d in devs]
current_furnaces = [d.name for d in devs if d.role == ROLE_FURNACE]
picked = st.multiselect("Furnace(s)", fnames, default=current_furnaces, key="furnace_pick")
if set(picked) != set(current_furnaces):
    if st.button("Apply furnace choice", type="primary", key="furnace_apply"):
        for d in devs:
            if d.name in picked and d.role != ROLE_FURNACE:
                d.role = ROLE_FURNACE
                d.expected_setpoint = DEFAULT_SETPOINT[ROLE_FURNACE] \
                    if d.expected_setpoint in (896.0, 0.0) else d.expected_setpoint
            elif d.name not in picked and d.role == ROLE_FURNACE:
                d.role = ROLE_KILN
                d.can_be_furnace = True
                d.expected_setpoint = DEFAULT_SETPOINT[ROLE_KILN] \
                    if d.expected_setpoint == DEFAULT_SETPOINT[ROLE_FURNACE] else d.expected_setpoint
        _apply_and_rerun(devs, "Furnace choice applied.")

st.subheader("Details")
changed = False
for i, d in enumerate(devs):
    s = core.latest_state(monitor, d)
    with st.container(border=True):
        head = {"furnace": "🔥", "kiln": "🪟", "other": "📟"}.get(d.role, "📟")
        live = (f"PV {s.pv:g}{core.unit_of(clients, d.name)} · output {s.output_pct:g}%"
                if s.connected and s.pv is not None else "no reading yet")
        st.markdown(f"#### {head} {d.name} &nbsp; <small>{live}</small>",
                    unsafe_allow_html=True)
        mi = d.model_info
        st.caption(f"{novus_models.describe(d.model)}"
                   + (f" · {d.product}" if d.product else "")
                   + (f" · fw {d.firmware}" if d.firmware else "")
                   + (f" — {mi.notes}" if mi.notes else ""))
        if not d.verified:
            st.warning("This model is not verified with this app. It stays "
                       "**read-only** (monitor and log only) unless you enable "
                       "writes below — then you are testing the register map "
                       "yourself. Reading PV/SP is safe.")
        if core.unit_of(clients, d.name) == "°C":
            st.error(f"**{d.name} is set to °C.** The app, its templates and the "
                     "other controllers assume °F — a hold of 896 on this unit means "
                     "896 °C (≈1645 °F). Switch it to °F on the front panel before "
                     "writing any program to it.")

        c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
        new_name = c1.text_input("Name", value=d.name, key=f"dn_{i}")
        new_role = c2.selectbox("Job", ROLES, index=ROLES.index(d.role),
                                format_func=ROLE_LABEL.get, key=f"dr_{i}")
        new_sp = c3.number_input(
            "Hold target °", value=float(d.expected_setpoint), step=1.0, key=f"ds_{i}",
            help="Furnace: the hold the watchdog protects. Kiln: the program-1 "
                 "parking temperature. Other: ignored.")
        new_model = c4.text_input("Model", value=d.model, key=f"dm_{i}")
        c5, c6, c7, c8 = st.columns([3, 2, 2, 2])
        new_addr = c5.text_input(
            "BLE address", value=d.address, key=f"da_{i}",
            help="Swapped the controller? Paste the new MAC — the name and all "
                 "its settings stay, since everything is keyed by name.")
        new_kw = c6.number_input("Rated kW (for energy)", min_value=0.0,
                                 max_value=200.0, value=float(d.power_kw or 0.0),
                                 step=0.5, key=f"dk_{i}")
        new_pw = c7.text_input("Controller password", value=d.password or "",
                               key=f"dp_{i}", type="password",
                               help="PAS on the front panel (factory default 1111). "
                                    "When set, the app unlocks the controller "
                                    "(register 53) before every write — needed on "
                                    "protected controllers, harmless otherwise.")
        new_wr = c8.toggle("Allow writes", value=d.writes_allowed, key=f"dw_{i}",
                           help="Off = this app only reads from it.")
        if (new_name != d.name or new_role != d.role or new_sp != d.expected_setpoint
                or new_model.strip().upper() != d.model
                or new_addr.strip().upper() != d.address
                or new_kw != (d.power_kw or 0.0) or new_wr != d.writes_allowed
                or new_pw.strip() != (d.password or "")):
            devs[i] = Device(name=new_name.strip(), address=new_addr.strip().upper(),
                             role=new_role, can_be_furnace=(new_role == ROLE_KILN),
                             expected_setpoint=float(new_sp), power_kw=float(new_kw),
                             model=new_model.strip().upper() or "N20K48",
                             firmware=d.firmware, product=d.product,
                             writes_allowed=bool(new_wr), password=new_pw.strip())
            changed = True
        b0, b1, b2, b3 = st.columns([1.3, 1, 1, 3])
        if b0.button("🩺 Diagnose", key=f"diag_{i}", width="stretch",
                     help="Read protection, unit, mode, limits, program and PID "
                          "and explain why a write might not stick. Read-only."):
            with st.spinner("Reading the controller's state…"):
                try:
                    rep = bridge.call(monitor.diagnose_now(d.name), timeout=60)
                    st.session_state[f"diag_{d.address}"] = rep
                except Exception as e:
                    st.error(f"diagnose failed: {e}")
        rep = st.session_state.get(f"diag_{d.address}")
        if rep:
            st.markdown(f"**{rep['summary']}**")
            for fnd in rep["findings"]:
                {"danger": st.error, "warn": st.warning}.get(fnd["level"], st.info)(fnd["text"])
            with st.expander("Raw register values"):
                import diagnose as _dx
                st.code(_dx.to_text(rep), language="text")
        if b1.button("🔎 Identify", key=f"ident_{i}", width="stretch",
                     help="Read model/firmware from the controller and store it."):
            with st.spinner("Reading device identification…"):
                try:
                    info = bridge.call(monitor.identify_now(d.address, d.name), timeout=45)
                    if info.get("product"):
                        d.product = info["product"]; d.firmware = info.get("revision", "")
                        if info.get("model"):
                            d.model = info["model"]
                        save_devices(devs)
                        core.reload_devices(bridge, clients, monitor)
                        st.success(f"{d.name}: {info['product']} (fw {d.firmware}) → "
                                   f"{novus_models.describe(d.model)}")
                    else:
                        st.error(f"identify failed: {info.get('error')}")
                except Exception as e:
                    st.error(f"identify failed: {e}")
        if b2.button("🗑 Remove", key=f"rm_{i}", width="stretch"):
            devs.pop(i)
            _apply_and_rerun(devs, f"Removed {d.name}.")

if changed:
    if st.button("💾 Save changes (applies live)", type="primary", key="save_all"):
        # carry a rename across the live monitor so its state/thresholds follow
        old = {x.address.upper(): x.name for x in load_devices()}
        for d in devs:
            o = old.get(d.address.upper())
            if o and o != d.name and o in clients:
                monitor.rename_device(o, d.name)       # monitor.clients IS `clients`
                if o in clients:                       # (defensive, if ever decoupled)
                    clients[d.name] = clients.pop(o)
                # history follows the name: logs, notebooks, snapshots
                for base in (core.LOG_DIR, core.NOTEBOOK_DIR, core.SNAPSHOT_DIR):
                    src, dst = base / o, base / d.name
                    if src.exists() and not dst.exists():
                        try:
                            src.rename(dst)
                        except Exception as e:
                            st.warning(f"could not move {src} → {dst}: {e}")
        _apply_and_rerun(devs, "Saved and applied.")
else:
    st.caption("Edit any field above and a Save button appears. Changes apply "
               "to the running monitor immediately — no restart.")
