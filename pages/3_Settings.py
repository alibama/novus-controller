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
           "for a few seconds, then resumes. A controller that's currently "
           "connected — to this app or to QuickTune on a phone — won't appear "
           "in a scan, because it stops advertising while connected.")

sc_secs = st.slider("Scan seconds", 5, 30, 8, key="scan_secs",
                    help="Longer scans catch controllers that advertise slowly.")
if st.button(f"🔍 Scan for {sc_secs} seconds", type="primary",
             disabled=st.session_state.get("scanning", False)):
    st.session_state["scanning"] = True
    with st.spinner("Pausing monitor and scanning…"):
        core.release_ble(bridge, clients, monitor)
        try:
            # scan_now serializes on the poll lock, frees the radio, always
            # stops discovery, and retries through BlueZ "in progress" states.
            results = bridge.call(monitor.scan_now(float(sc_secs), NOVUS_OUI),
                                  timeout=float(sc_secs) + 40)
            st.session_state["scan_results"] = results
        except Exception as e:
            msg = str(e)
            if "in progress" in msg.lower() or "inprogress" in msg.lower():
                st.error("Bluetooth is still finishing a previous scan. Wait a "
                         "few seconds and try again. If it persists, the adapter "
                         "is stuck — restart the Bluetooth service "
                         "(`sudo systemctl restart bluetooth`) or the app.")
            else:
                st.error(f"scan failed: {e}")
        finally:
            core.reclaim_ble(bridge, clients, monitor)
            st.session_state["scanning"] = False

results = st.session_state.get("scan_results")
if results is not None and not results:
    st.warning(
        "Scan finished but found **no devices**. Things to check:\n"
        "- A controller that's **connected** (to this app or QuickTune) won't "
        "show — disconnect QuickTune and make sure the monitor released the "
        "radio, then rescan.\n"
        "- The Bluetooth adapter may be off or blocked — on the host: "
        "`rfkill unblock bluetooth` and `bluetoothctl show` (look for "
        "*Powered: yes*).\n"
        "- Under a service user, discovery needs permission — the user should "
        "be in the `bluetooth` group, or verify manually with "
        "`bluetoothctl scan on`.\n"
        "- Try a longer scan (slider above) and move a controller within a few "
        "metres.\n\n"
        "You don't have to wait on the scanner — add controllers by address "
        "under **Devices & roles → Add a controller by address** below.")
elif results:
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

# Everything (connections, watchdogs, logs, thresholds) is keyed by device
# NAME, so duplicate names collapse two controllers into one and must be fixed.
from collections import Counter as _Counter
_dupes = [n for n, c in _Counter(d.name for d in devs).items() if c > 1]
if _dupes:
    st.error(
        f"⚠️ Duplicate device name(s): **{', '.join(_dupes)}**. Two controllers "
        "with the same name collapse into one — only one gets monitored, and the "
        "UI can misbehave. Give each a unique name below (e.g. rename the old one "
        "to `furnace-old`) or remove the stale one, then **Save** and restart.")

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
            new_addr = st.text_input(
                "BLE address", value=d.address, key=f"ad_{i}",
                help="Swapped this controller? Paste the new MAC here — the name "
                     "and all its settings (thresholds, rated kW) stay put, since "
                     "everything is keyed by name, not address.")
            if (new_name != d.name or new_role != d.role
                    or new_sp != d.expected_setpoint or new_dbl != d.can_be_furnace
                    or new_addr.strip().upper() != d.address.upper()):
                devs[i] = Device(name=new_name, address=new_addr.strip().upper(),
                                 role=new_role, can_be_furnace=new_dbl,
                                 expected_setpoint=new_sp,
                                 power_kw=getattr(d, "power_kw", 0.0))
                changed = True
            if st.button("🗑 Remove", key=f"rm_{i}"):
                devs.pop(i)
                save_devices(devs)
                st.rerun()

    if changed:
        _newdupes = [n for n, c in _Counter(x.name for x in devs).items() if c > 1]
        if _newdupes:
            st.warning(f"Fix duplicate name(s) first: {', '.join(_newdupes)}.")
        elif st.button("💾 Save changes", type="primary"):
            save_devices(devs)
            st.success("Saved. Restart the service to apply: "
                       "`sudo systemctl restart kiln-dashboard`")

with st.expander("➕ Add a controller by address (no scan needed)"):
    st.caption("If the scanner won't cooperate, add controllers by hand. Get "
               "each MAC from QuickTune Mobile's device list (or the label on "
               "the module). Format like `00:26:A4:xx:xx:xx`.")
    a1, a2, a3 = st.columns([2, 3, 2])
    m_name = a1.text_input("Name", key="madd_name")
    m_addr = a2.text_input("BLE address", key="madd_addr")
    m_role = a3.selectbox("Role", [ROLE_KILN, ROLE_FURNACE], key="madd_role")
    if st.button("Add controller", key="madd_go"):
        import re as _re
        addr = m_addr.strip().upper()
        ok_mac = bool(_re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", addr))
        devs2 = load_devices()
        if not m_name.strip() or not ok_mac:
            st.error("Enter a name and a valid BLE address "
                     "(six hex pairs, e.g. 00:26:A4:1A:2B:3C).")
        elif any(x.address.upper() == addr for x in devs2):
            st.warning("That address is already in your list.")
        elif any(x.name == m_name.strip() for x in devs2):
            st.warning(f"A device named '{m_name.strip()}' already exists — "
                       "pick a unique name.")
        else:
            devs2.append(Device(
                name=m_name.strip(), address=addr, role=m_role,
                can_be_furnace=(m_role == ROLE_KILN),
                expected_setpoint=(2100.0 if m_role == ROLE_FURNACE else 896.0)))
            save_devices(devs2)
            st.success(f"Added {m_name.strip()} ({addr}). Restart the service to "
                       "start monitoring it: `sudo systemctl restart kiln-dashboard`")
            st.rerun()

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
st.header("3b · Build or edit a custom program")
st.caption("Type any ramp/soak program and write it to any slot. Each row is a "
           "segment: how many minutes to reach a target and hold there. Use "
           "0 (or 1) minutes for an as-fast-as-possible jump. Load a template "
           "or the controller's current slot as a starting point, then edit. "
           "Max 9 segments per program (the controller's limit).")

if not names:
    st.info("Add a device first.")
else:
    import pandas as pd
    ss = st.session_state
    ss.setdefault("pe_start", 70.0)
    ss.setdefault("pe_tol", 0.0)
    ss.setdefault("pe_link", 0)
    ss.setdefault("pe_nonce", 0)
    ss.setdefault("pe_df", pd.DataFrame(
        [{"minutes": 60, "target °F": 1000, "event": 0}]))

    e1, e2 = st.columns([2, 1])
    pe_ctrl = e1.selectbox("Controller", names, key="pe_ctrl")
    pe_slot = e2.number_input("Slot", min_value=1, max_value=20, value=1, key="pe_slot")

    # prefill options
    l1, l2, l3 = st.columns([3, 1, 1])
    pe_tmpl = l1.selectbox("Template to load", list(LIBRARY.keys()),
                           format_func=lambda k: LIBRARY[k].name, key="pe_tmpl")
    if l2.button("Load template", use_container_width=True):
        sp = LIBRARY[pe_tmpl]
        ss.pe_start = float(sp.start_setpoint)
        ss.pe_tol = float(sp.tolerance)
        ss.pe_link = 0
        ss.pe_df = pd.DataFrame([{"minutes": s.minutes, "target °F": s.setpoint,
                                  "event": s.event} for s in sp.segments])
        ss.pe_nonce += 1
        st.rerun()
    if l3.button("Load current", use_container_width=True,
                 help="Read whatever is in this slot on the controller now."):
        with st.spinner("Reading the slot from the controller…"):
            try:
                prog = bridge.call(monitor.read_program_now(pe_ctrl, int(pe_slot)),
                                   timeout=60)
                ss.pe_start = float(getattr(prog, "start_setpoint", 0.0))
                ss.pe_tol = float(prog.tolerance)
                ss.pe_link = int(prog.link_to)
                ss.pe_df = pd.DataFrame(
                    [{"minutes": s.duration_minutes, "target °F": s.setpoint,
                      "event": s.event} for s in prog.segments]
                    or [{"minutes": 0, "target °F": 0, "event": 0}])
                ss.pe_nonce += 1
                st.success(f"Loaded slot {int(pe_slot)} from {pe_ctrl}.")
                st.rerun()
            except Exception as ex:
                st.error(f"couldn't read the slot: {ex}")

    # program-level params
    q1, q2, q3 = st.columns(3)
    pe_start = q1.number_input("Start setpoint °F", value=float(ss.pe_start),
                               min_value=0.0, max_value=2400.0, step=5.0,
                               key="pe_start")
    pe_tol = q2.number_input("Tolerance ± ° (0 = off)", value=float(ss.pe_tol),
                             min_value=0.0, max_value=200.0, step=1.0, key="pe_tol",
                             help="Guaranteed soak: the timer pauses until PV is "
                                  "within this band of the target. 0 disables it.")
    pe_link = q3.number_input("Link to program # (0 = none)", value=int(ss.pe_link),
                              min_value=0, max_value=20, step=1, key="pe_link",
                              help="Chain to another program when this one ends.")

    # the editable segment table
    edited = st.data_editor(
        ss.pe_df, num_rows="dynamic", use_container_width=True,
        key=f"pe_editor_{ss.pe_nonce}",
        column_config={
            "minutes": st.column_config.NumberColumn(
                "minutes", min_value=0, max_value=9999, step=1,
                help="Time to reach & hold this target. 0–1 = as fast as possible."),
            "target °F": st.column_config.NumberColumn(
                "target °F", min_value=0, max_value=2400, step=5),
            "event": st.column_config.NumberColumn(
                "event", min_value=0, max_value=255, step=1,
                help="Digital event/output flags. Leave 0 unless you use them."),
        })

    # clean + validate the rows
    segs = []
    prev = pe_start
    preview = []
    for _, r in edited.iterrows():
        mins = r.get("minutes")
        tgt = r.get("target °F")
        if pd.isna(mins) or pd.isna(tgt):
            continue
        mins = int(mins); tgt = float(tgt); ev = int(r.get("event") or 0)
        segs.append(ProgramSegment(setpoint=tgt, duration_minutes=mins, event=ev))
        if tgt == prev:
            kind = "hold"
        elif mins <= 1:
            kind = "AFAP " + ("cool" if tgt < prev else "heat")
        else:
            rate = abs(tgt - prev) / (mins / 60.0)
            kind = f"{'cool' if tgt < prev else 'heat'} {rate:.0f}°/hr"
        preview.append({"seg": len(segs), "target °F": f"{tgt:g}",
                        "minutes": mins, "ramp/hold": kind})
        prev = tgt

    total_h = sum(s.duration_minutes for s in segs) / 60.0
    if preview:
        st.caption(f"{len(segs)} segment(s) · ~{total_h:.1f}h of ramps "
                   "(holds/soaks add more) · start "
                   f"{pe_start:g}°F, tolerance ±{pe_tol:g}°")
        st.dataframe(pd.DataFrame(preview), use_container_width=True, hide_index=True)

    pe_pw = st.text_input("Controller password (only if writes are protected)",
                          type="password", key="pe_pw")

    problems = []
    if not segs:
        problems.append("add at least one segment")
    if len(segs) > 9:
        problems.append(f"too many segments ({len(segs)}); the controller allows 9")
    if problems:
        st.warning("Before writing: " + "; ".join(problems) + ".")

    st.info(f"This will **overwrite slot {int(pe_slot)} on {pe_ctrl}** with the "
            "program above. The controller stores it permanently.")
    pe_confirm = st.checkbox(f"Yes, overwrite slot {int(pe_slot)} on {pe_ctrl}",
                             key="pe_confirm")
    if st.button("✍️ Write custom program", type="primary",
                 disabled=bool(problems) or not pe_confirm):
        with st.spinner("Grabbing Bluetooth and writing…"):
            try:
                bridge.call(
                    monitor.write_program_now(
                        pe_ctrl, int(pe_slot), segs,
                        tolerance=float(pe_tol), start_setpoint=float(pe_start),
                        link_to=int(pe_link),
                        password=(int(pe_pw) if pe_pw.strip().isdigit() else None)),
                    timeout=60)
                ss.pe_df = edited
                st.success(f"Wrote and verified {len(segs)} segments to slot "
                           f"{int(pe_slot)} on {pe_ctrl}.")
                core.render_segments(pe_ctrl, int(pe_slot), monitor)
                st.caption("Tip: to run it, use the Recovery & Logic page's "
                           "“Run a program” control, or set it as program 1 to "
                           "make it this controller's set-and-forget hold.")
            except Exception as ex:
                st.error(f"write failed: {ex}")

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


# ---------------------------------------------------------------------------
# Energy & open data
# ---------------------------------------------------------------------------
st.divider()
st.header("⚡ Energy & open data")
st.caption("Set your studio name, electricity rate, and each controller's rated "
           "power so the app can estimate energy and cost per firing — and so the "
           "Open Data Studio harvest on the home page is attributed and priced. "
           "Energy is estimated from output% × rated kW; calibrate the kW from a "
           "clamp-meter reading at 100% output for the truest numbers.")

_ss = core.load_studio_settings()
es1, es2, es3 = st.columns([2, 1, 1])
studio_name = es1.text_input("Studio name (attribution)", value=_ss.get("studio", ""))
rate = es2.number_input("Electricity rate / kWh", min_value=0.0, max_value=2.0,
                        value=float(_ss.get("rate_per_kwh", 0.0)), step=0.01,
                        format="%.3f")
currency = es3.text_input("Currency", value=_ss.get("currency", "USD"))

st.markdown("**Rated power per controller (kW at 100% output)**")
pk_cols = st.columns(max(1, len(devices)))
new_kw = {}
for _ki, (col, dev) in enumerate(zip(pk_cols, devices)):
    new_kw[dev.name] = col.number_input(
        f"{dev.name}", min_value=0.0, max_value=200.0,
        value=float(getattr(dev, "power_kw", 0.0) or 0.0), step=0.5,
        key=f"kw_{dev.name}_{_ki}")

if st.button("💾 Save energy settings", type="primary"):
    core.save_studio_settings({"studio": studio_name,
                               "rate_per_kwh": float(rate),
                               "currency": currency or "USD"})
    from devices import save_devices
    for dev in devices:
        dev.power_kw = float(new_kw.get(dev.name, 0.0))
    save_devices(devices)
    st.success("Saved. The home-page Open Data Studio harvest now uses these.")
    st.rerun()

# ---------------------------------------------------------------------------
# Controller config snapshot & audit
# ---------------------------------------------------------------------------
st.divider()
st.header("🗄 Controller config snapshot & audit")
st.caption("Capture a controller's full configuration (all 20 program tables "
           "plus the key config registers) with multi-read verification, and "
           "store it two ways: a canonical JSON with a SHA-256 fingerprint, and "
           "a human-readable text dump. Use it to confirm nothing gets changed "
           "along the way — re-snapshot any time and diff against a previous one.")

snap_ctrl = st.selectbox("Controller", [d.name for d in devices], key="snap_ctrl")

sc1, sc2 = st.columns([1, 2])
if sc1.button("📸 Read & save snapshot", type="primary", key="snap_go"):
    with st.spinner("Reading all programs and config registers (verified, "
                    "takes ~30–60s)…"):
        try:
            snap = bridge.call(monitor.snapshot_config_now(snap_ctrl), timeout=180)
            saved = core.save_snapshot(snap_ctrl, snap)
            st.session_state["last_snap"] = snap
            st.session_state["last_snap_ctrl"] = snap_ctrl
            st.success(f"Saved. SHA-256: `{saved['sha256']}`")
            if snap["meta"]["unstable_reads"]:
                st.warning("Some reads did not settle and are flagged UNSTABLE: "
                           + ", ".join(map(str, snap["meta"]["unstable_reads"]))
                           + ". Re-run if you need a clean capture.")
        except Exception as e:
            st.error(f"snapshot failed: {e}")

# Show the most recent capture (this session) + offer downloads
snap = st.session_state.get("last_snap")
if snap and st.session_state.get("last_snap_ctrl") == snap_ctrl:
    import config_snapshot
    human = config_snapshot.to_human(snap)
    tampered = not config_snapshot.verify_snapshot(snap)
    if tampered:
        st.error("Hash does NOT match the data — snapshot integrity check failed.")
    st.text_area("Human-readable snapshot", human, height=320, key="snap_human")
    dl1, dl2 = st.columns(2)
    import json as _json
    dl1.download_button("⬇ JSON (canonical)",
                        _json.dumps(snap, indent=2, sort_keys=True),
                        file_name=f"{snap_ctrl}_config.json", mime="application/json")
    dl2.download_button("⬇ Text (human-readable)", human,
                        file_name=f"{snap_ctrl}_config.txt", mime="text/plain")

# Stored snapshots + diff
stored = core.list_snapshots(snap_ctrl)
if stored:
    st.subheader("Stored snapshots")
    st.caption(f"{len(stored)} saved under `config_snapshots/{snap_ctrl}/`. "
               "Compare any two to confirm what (if anything) changed.")
    labels = [p.stem for p in stored]
    d1, d2 = st.columns(2)
    a = d1.selectbox("Older", labels, index=min(1, len(labels) - 1), key="diff_a")
    b = d2.selectbox("Newer", labels, index=0, key="diff_b")
    if st.button("🔍 Compare", key="snap_diff"):
        import config_snapshot
        sa = core.load_snapshot(stored[labels.index(a)])
        sb = core.load_snapshot(stored[labels.index(b)])
        if not config_snapshot.verify_snapshot(sa) or not config_snapshot.verify_snapshot(sb):
            st.error("One of the snapshots fails its own hash check — it was "
                     "edited outside this tool. Diff shown anyway.")
        changes = config_snapshot.diff_snapshots(sa, sb)
        if not changes:
            st.success("Identical configuration — nothing changed. "
                       f"(Both hash to `{sa['meta']['sha256'][:16]}…`)")
        else:
            st.warning(f"{len(changes)} difference(s):")
            for ch in changes:
                st.write(f"- {ch}")
