"""
Settings page
=============
Three jobs:
  1. Scan for Bluetooth controllers (pauses monitoring during the scan).
  2. List devices and assign each as a furnace or a kiln (+ optional
     "can double as furnace"). Saves to devices.json — restart to apply.
  3. Write a standard program into a permanent slot on a chosen controller.
"""
from __future__ import annotations

import asyncio
import streamlit as st

import app_core as core
from devices import (load_devices, save_devices, Device,
                     ROLE_FURNACE, ROLE_KILN)
from programs_library import LIBRARY, total_minutes
from novus_client import ProgramSegment

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("⚙️ Settings")

NOVUS_OUI = "00:26:A4"   # Novus controllers; also advertise mfg id 511

# ===========================================================================
# 1. SCAN
# ===========================================================================
st.header("1 · Find controllers")
st.caption("Scanning needs the Bluetooth radio, so this pauses monitoring "
           "for a few seconds, then resumes.")


async def _scan(seconds: float = 8.0):
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        is_novus = (str(addr).upper().startswith(NOVUS_OUI)
                    or 511 in (adv.manufacturer_data or {}))
        out.append({
            "address": str(addr).upper(),
            "name_adv": (adv.local_name or getattr(dev, "name", "") or ""),
            "rssi": adv.rssi,
            "novus": is_novus,
        })
    out.sort(key=lambda r: (not r["novus"], -(r["rssi"] or -999)))
    return out

if st.button("🔍 Scan for 8 seconds", type="primary"):
    with st.spinner("Pausing monitor and scanning…"):
        core.release_ble(bridge, clients, monitor)
        try:
            results = bridge.call(_scan(8.0), timeout=20)
            st.session_state["scan_results"] = results
        except Exception as e:
            st.error(f"scan failed: {e}")
        finally:
            core.reclaim_ble(bridge, clients, monitor)

results = st.session_state.get("scan_results")
if results:
    known = {d.address.upper() for d in devices}
    st.write(f"Found {len(results)} device(s). Novus controllers listed first.")
    for r in results:
        tag = "🟢 Novus" if r["novus"] else "⚪️ other"
        already = " · already in your list" if r["address"] in known else ""
        line = f"{tag} · `{r['address']}` · {r['name_adv'] or '(no name)'} · RSSI {r['rssi']}{already}"
        cols = st.columns([4, 1])
        cols[0].write(line)
        if r["novus"] and r["address"] not in known:
            if cols[1].button("Add", key=f"add_{r['address']}"):
                devs = load_devices()
                devs.append(Device(name=f"new-{r['address'][-5:].replace(':','')}",
                                   address=r["address"], role=ROLE_KILN,
                                   can_be_furnace=True, expected_setpoint=896.0))
                save_devices(devs)
                st.success("Added. Edit its name/role below, then restart the service.")
                st.rerun()

st.divider()

# ===========================================================================
# 2. DEVICE LIST / ROLES
# ===========================================================================
st.header("2 · Devices & roles")
st.caption("Furnaces are temperature-tracked and watched for dropouts. "
           "Kilns run programs. Changes save immediately but **take effect "
           "after a service restart** (we don't disturb a live furnace).")

devs = load_devices()
if not devs:
    st.info("No devices yet — scan above and add one.")
else:
    changed = False
    for i, d in enumerate(devs):
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
            new_name = c1.text_input("Name", value=d.name, key=f"nm_{i}")
            new_role = c2.selectbox(
                "Role", [ROLE_KILN, ROLE_FURNACE],
                index=(1 if d.role == ROLE_FURNACE else 0), key=f"rl_{i}")
            new_sp = c3.number_input(
                "Target °F", value=float(d.expected_setpoint),
                step=1.0, key=f"sp_{i}",
                help="Furnace hold target the watchdog checks against.")
            new_dbl = c4.checkbox(
                "Can double as furnace", value=d.can_be_furnace, key=f"db_{i}",
                help="Allow temporarily treating this kiln as a furnace for tests.")
            c4.caption(f"`{d.address}`")
            if (new_name != d.name or new_role != d.role
                    or new_sp != d.expected_setpoint or new_dbl != d.can_be_furnace):
                devs[i] = Device(name=new_name, address=d.address, role=new_role,
                                 can_be_furnace=new_dbl, expected_setpoint=new_sp)
                changed = True
            if st.button("🗑 Remove", key=f"rm_{i}"):
                devs.pop(i)
                save_devices(devs)
                st.rerun()

    if changed:
        if st.button("💾 Save changes", type="primary"):
            save_devices(devs)
            st.success("Saved. Restart the service to apply: "
                       "`sudo systemctl restart kiln-dashboard`")

st.divider()

# ===========================================================================
# 3. WRITE A STANDARD PROGRAM
# ===========================================================================
st.header("3 · Write a standard program to a controller")
st.warning("These schedules are **starting templates** (Bullseye / COE-90, "
           "~6mm). Verify and adjust for your glass, thickness, and molds "
           "before trusting a firing.")

names = [d.name for d in devices]
if not names:
    st.info("Add a device first.")
else:
    p1, p2, p3 = st.columns([2, 3, 1])
    target = p1.selectbox("Controller", names)
    prog_key = p2.selectbox(
        "Standard program", list(LIBRARY.keys()),
        format_func=lambda k: LIBRARY[k].name)
    slot = p3.number_input("Slot", min_value=1, max_value=20, value=1)

    sp = LIBRARY[prog_key]
    st.markdown(f"**{sp.name}** — {sp.description}")
    hrs = total_minutes(sp) / 60.0
    st.caption(f"Start {sp.start_setpoint:g}°F · tolerance ±{sp.tolerance:g}° · "
               f"{len(sp.segments)} segments · ~{hrs:.1f}h total (holds may extend this)")

    import pandas as pd
    prev_sp = sp.start_setpoint
    rows = [{"seg": "start", "target °F": f"{sp.start_setpoint:g}",
             "minutes": "—", "ramp/hold": "—"}]
    for i, seg in enumerate(sp.segments, start=1):
        if seg.setpoint == prev_sp:
            kind = "hold"
        elif seg.minutes <= 1:
            kind = "AFAP cool" if seg.setpoint < prev_sp else "AFAP heat"
        else:
            rate = abs(seg.setpoint - prev_sp) / (seg.minutes / 60.0)
            kind = f"{'cool' if seg.setpoint < prev_sp else 'heat'} {rate:.0f}°/hr"
        rows.append({"seg": str(i), "target °F": f"{seg.setpoint:g}",
                     "minutes": seg.minutes, "ramp/hold": kind})
        prev_sp = seg.setpoint
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    pw = st.text_input(
        "Controller password (only if writes are protected — leave blank otherwise)",
        type="password",
        help="Some controllers require unlocking (register 53) before config writes.")

    st.info(f"This will **overwrite program slot {slot} on {target}** with "
            f"“{sp.name}”. The controller stores it permanently.")
    confirm = st.checkbox(f"Yes, overwrite slot {slot} on {target}")
    if st.button("✍️ Write program", type="primary", disabled=not confirm):
        segs = [ProgramSegment(setpoint=s.setpoint,
                               duration_minutes=s.minutes, event=s.event)
                for s in sp.segments]
        with st.spinner("Grabbing Bluetooth and writing…"):
            try:
                written = bridge.call(
                    monitor.write_program_now(
                        target, int(slot), segs,
                        tolerance=sp.tolerance,
                        start_setpoint=sp.start_setpoint,
                        password=(int(pw) if pw.strip().isdigit() else None)),
                    timeout=60)
                st.success(f"Wrote and verified “{sp.name}” to slot {slot} on {target}.")
                core.render_segments(target, int(slot), monitor)
            except Exception as e:
                st.error(f"write failed: {e}")
                if pw.strip() and not pw.strip().isdigit():
                    st.caption("Note: the password must be numeric for the Novus session register.")

st.divider()

# ===========================================================================
# 4. SET GUARANTEED-SOAK TOLERANCE ON AN EXISTING PROGRAM
# ===========================================================================
st.header("4 · Guaranteed-soak tolerance (fix programs in place)")
st.caption("The tolerance band makes a segment's timer pause whenever PV drifts "
           "more than ±band from the setpoint — so a hold actually happens AT "
           "temperature instead of on the wall clock. 0 = runs on the clock "
           "(soaks may complete while still under temperature). Set ±5–9°F on "
           "programs whose soaks matter. This edits the slot in place — no need "
           "to re-enter the program.")

if not names:
    st.info("Add a device first.")
else:
    t1, t2 = st.columns([2, 1])
    tctrl = t1.selectbox("Controller", names, key="tol_ctrl")
    tslot = t2.number_input("Slot", min_value=1, max_value=20, value=1, key="tol_slot")
    cc1, cc2, cc3 = st.columns([1, 1, 1])
    if cc1.button("📖 Read current"):
        with st.spinner("Reading…"):
            try:
                cur = bridge.call(
                    monitor.control(tctrl, "get_tolerance", int(tslot)), timeout=30)
                st.info(f"Slot {tslot} on {tctrl}: currently ±{cur:g}°"
                        + ("  (0 = runs on the clock — soaks not guaranteed)"
                           if not cur else ""))
            except Exception as e:
                st.error(f"read failed: {e}")
    newtol = cc2.number_input("New ± band (°F)", min_value=0.0, max_value=100.0,
                              value=5.0, step=1.0, key="tol_new")
    if cc3.button("✍️ Set tolerance", type="primary"):
        with st.spinner("Grabbing Bluetooth and setting…"):
            try:
                bridge.call(
                    monitor.control(tctrl, "set_tolerance", int(tslot), float(newtol)),
                    timeout=30)
                st.success(f"Set slot {tslot} on {tctrl} to ±{newtol:g}°. "
                           "Takes effect next time that program runs.")
            except Exception as e:
                st.error(f"set failed: {e}")

st.divider()

# ===========================================================================
# 5. TEST NOTIFICATIONS
# ===========================================================================
st.header("5 · Test notifications")
import notify
configured = notify.channels_configured()
if configured:
    st.caption("Configured channels: " + ", ".join(configured))
else:
    st.warning("No channels configured in this process. If alerts aren't "
               "arriving, the service isn't loading `notify.env` — add "
               "`EnvironmentFile=/home/youruser/novus-n20k48-ble/notify.env` under "
               "`[Service]` in the unit file and restart.")

tcol1, tcol2 = st.columns([3, 1])
test_msg = tcol1.text_input("Test message",
                            value="Test from the kiln dashboard 🔥",
                            label_visibility="collapsed")
if tcol2.button("📨 Send test", use_container_width=True, disabled=not configured):
    try:
        results = notify.send(test_msg, title="Kiln dashboard test", priority="default")
        for chan, ok in results.items():
            (st.success if ok else st.error)(
                f"{chan}: {'sent ✓' if ok else 'failed ✗'}")
        if not results:
            st.info("No channels fired (none configured).")
    except Exception as e:
        st.error(f"send failed: {e}")

st.divider()

# ===========================================================================
# 6. REGISTER CONSOLE (advanced)
# ===========================================================================
st.header("6 · Register console (advanced)")
st.warning("Direct read/write of controller registers. Reading is safe; "
           "**writing can change how the controller behaves** — double-check "
           "the Novus manual. Temperatures here are raw whole degrees "
           "(decimal places = 0).")

# Friendly names. Address 253 isn't confirmed in our map — included so you can
# read it and compare to the manual (likely the power-return / resume mode).
KNOWN_REGS = {
    51:  "PROTECTION — password level (1–4)",
    53:  "OPEN_SESSION — write password to unlock config writes",
    77:  "UNIT — 0=°C, 1=°F",
    200: "SETPOINT",
    201: "PV (measured temperature)",
    202: "OUTPUT — 0..1000 = 0..100.0%",
    213: "CTRL_AUTO — 0=manual, 1=automatic",
    214: "CTRL_RUN — 0=stopped, 1=running",
    219: "HYST — hysteresis",
    220: "SPLL — setpoint lower limit",
    221: "SPHL — setpoint upper limit",
    224: "OULL — output lower limit",
    225: "OUHL — output upper limit",
    226: "SOFT_START — soft-start time (s)",
    247: "RS_PRN_EXEC — program SELECT (0..20)",
    248: "RS_PRN_EDIT — program to view/edit",
    249: "RS_SEG — current segment",
    250: "RS_SEG_TIME — elapsed time in segment (s)",
    252: "RS_TBASE — 0=seconds, 1=minutes",
    253: "??? — CHECK MANUAL (likely power-return / resume mode)",
    254: "RS_PROG_TYPE — 0=none, 1=ramp-to-soak, 2=R&S program",
    257: "TUNE_AUTO",
    258: "TUNE_PB — proportional band",
    259: "TUNE_IR — integral rate",
    260: "TUNE_DT — derivative time (s)",
    261: "TUNE_CT — PWM cycle (s)",
    279: "DPPO — decimal point position",
}

if not names:
    st.info("Add a device first.")
else:
    rc_ctrl = st.selectbox("Controller", names, key="reg_ctrl")

    pick = st.selectbox(
        "Register", ["(custom address)"] + [f"{a} — {KNOWN_REGS[a]}" for a in sorted(KNOWN_REGS)],
        key="reg_pick")
    if pick.startswith("("):
        addr = st.number_input("Address", min_value=0, max_value=9999, value=253,
                               key="reg_addr_custom")
    else:
        addr = int(pick.split(" — ")[0])

    read_col, _ = st.columns([1, 3])
    if read_col.button("📖 Read register", use_container_width=True):
        with st.spinner("Reading…"):
            try:
                vals = bridge.call(
                    monitor.read_registers_now(rc_ctrl, int(addr), 1), timeout=30)
                st.success(f"`{rc_ctrl}` register **{addr}** = **{vals[0]}**"
                           if vals else "no value returned")
            except Exception as e:
                st.error(f"read failed: {e}")

    with st.expander("✍️ Write this register (careful)"):
        wval = st.number_input("Value to write (raw integer)", value=0, step=1,
                               key="reg_wval")
        wpw = st.text_input("Controller password (if writes are protected)",
                            type="password", key="reg_wpw")
        st.caption(f"Will write **{int(wval)}** to register **{addr}** on "
                   f"**{rc_ctrl}**, then read it back to confirm.")
        wconfirm = st.checkbox(f"Yes, write {int(wval)} to register {addr}",
                               key="reg_confirm")
        if st.button("Write register", type="primary", disabled=not wconfirm):
            with st.spinner("Grabbing Bluetooth and writing…"):
                try:
                    rb = bridge.call(
                        monitor.write_register_now(
                            rc_ctrl, int(addr), int(wval),
                            password=(int(wpw) if wpw.strip().isdigit() else None)),
                        timeout=45)
                    if rb == int(wval):
                        st.success(f"Wrote and verified: register {addr} = {rb}.")
                    else:
                        st.warning(f"Wrote {int(wval)} but read back {rb}. "
                                   "The controller may have rejected or clamped it "
                                   "(check password / valid range).")
                except Exception as e:
                    st.error(f"write failed: {e}")
