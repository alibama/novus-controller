"""
Settings
========
Controller programming and housekeeping:
  1. Write a standard program (hold, anneal, fuse …) into a slot.
  2. Build or edit a custom program.
  3. Guaranteed-soak tolerance on an existing slot.
  4. Notifications test.
  5. Register console (advanced).
  6. Bluetooth sharing (cooperative mode / read interval).
  7. Controller config snapshot & audit.

Finding and naming controllers lives on the Devices page; studio name,
energy pricing and the API live on Open Data & API.
"""
from __future__ import annotations

import streamlit as st

import app_core as core
from programs_library import LIBRARY, total_minutes
from novus_client import ProgramSegment

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("⚙️ Settings")
st.caption("Looking for the scanner or device roles? They moved to **Devices**. "
           "Studio name, energy rates and the API token are on **Open Data & API**.")

names = [d.name for d in devices]
writable = [d.name for d in devices if d.writes_allowed]
if names and not writable:
    st.warning("All your controllers are read-only (unverified model, or writes "
               "switched off on the Devices page), so the program writers below "
               "are disabled.")


def _rate_rows(start, segs):
    """Rows for a segment table with a human 'ramp/hold' column."""
    rows = [{"seg": "start", "target °F": f"{start:g}", "minutes": "—", "ramp/hold": "—"}]
    prev = start
    for i, (mins, tgt) in enumerate(segs, start=1):
        if tgt == prev:
            kind = "hold"
        elif mins <= 1:
            kind = "AFAP cool" if tgt < prev else "AFAP heat"
        else:
            rate = abs(tgt - prev) / (mins / 60.0)
            kind = f"{'cool' if tgt < prev else 'heat'} {rate:.0f}°/hr"
        rows.append({"seg": str(i), "target °F": f"{tgt:g}", "minutes": mins,
                     "ramp/hold": kind})
        prev = tgt
    return rows


# ===========================================================================
# 1. WRITE A STANDARD PROGRAM
# ===========================================================================
st.header("1 · Write a standard program to a controller")
st.warning("These schedules are **starting templates** (Bullseye / COE-90, "
           "~6mm). Verify and adjust for your glass, thickness, and molds "
           "before trusting a firing. The **Learn** page explains each one.")

if not writable:
    st.info("Add a writable controller first (Devices page).")
else:
    p1, p2, p3 = st.columns([2, 3, 1])
    target = p1.selectbox("Controller", writable)
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
    st.dataframe(pd.DataFrame(_rate_rows(sp.start_setpoint,
                                         [(s.minutes, s.setpoint) for s in sp.segments])),
                 width="stretch", hide_index=True)

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
                bridge.call(
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
# 2. BUILD OR EDIT A CUSTOM PROGRAM
# ===========================================================================
st.header("2 · Build or edit a custom program")
st.caption("Type any ramp/soak program and write it to any slot. Each row is a "
           "segment: how many minutes to reach a target and hold there. Use "
           "0 (or 1) minutes for an as-fast-as-possible jump. Load a template "
           "or the controller's current slot as a starting point, then edit. "
           "Max 9 segments per program (the controller's limit).")

if not writable:
    st.info("Add a writable controller first (Devices page).")
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
    pe_ctrl = e1.selectbox("Controller", writable, key="pe_ctrl")
    pe_slot = e2.number_input("Slot", min_value=1, max_value=20, value=1, key="pe_slot")

    # prefill options
    l1, l2, l3 = st.columns([3, 1, 1])
    pe_tmpl = l1.selectbox("Template to load", list(LIBRARY.keys()),
                           format_func=lambda k: LIBRARY[k].name, key="pe_tmpl")
    if l2.button("Load template", width="stretch"):
        sp = LIBRARY[pe_tmpl]
        ss.pe_start = float(sp.start_setpoint)
        ss.pe_tol = float(sp.tolerance)
        ss.pe_link = 0
        ss.pe_df = pd.DataFrame([{"minutes": s.minutes, "target °F": s.setpoint,
                                  "event": s.event} for s in sp.segments])
        ss.pe_nonce += 1
        st.rerun()
    if l3.button("Load current", width="stretch",
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
        ss.pe_df, num_rows="dynamic", width="stretch",
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
    pairs = []
    for _, r in edited.iterrows():
        mins = r.get("minutes")
        tgt = r.get("target °F")
        if pd.isna(mins) or pd.isna(tgt):
            continue
        mins = int(mins); tgt = float(tgt); ev = int(r.get("event") or 0)
        segs.append(ProgramSegment(setpoint=tgt, duration_minutes=mins, event=ev))
        pairs.append((mins, tgt))

    total_h = sum(s.duration_minutes for s in segs) / 60.0
    if pairs:
        st.caption(f"{len(segs)} segment(s) · ~{total_h:.1f}h of ramps "
                   "(holds/soaks add more) · start "
                   f"{pe_start:g}°F, tolerance ±{pe_tol:g}°")
        st.dataframe(pd.DataFrame(_rate_rows(pe_start, pairs)[1:]),
                     width="stretch", hide_index=True)

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
                st.caption("Tip: to run it, use the Home page's RUN button or the "
                           "Recovery & Logic page's “Run a program” control; set it "
                           "as program 1 to make it this controller's "
                           "set-and-forget hold.")
            except Exception as ex:
                st.error(f"write failed: {ex}")

st.divider()

# ===========================================================================
# 3. GUARANTEED-SOAK TOLERANCE
# ===========================================================================
st.header("3 · Guaranteed-soak tolerance (fix programs in place)")
st.caption("The tolerance band makes a segment's timer pause whenever PV drifts "
           "more than ±band from the setpoint — so a hold actually happens AT "
           "temperature instead of on the wall clock. 0 = runs on the clock "
           "(soaks may complete while still under temperature). Set ±5–9°F on "
           "programs whose soaks matter. This edits the slot in place.")

if not writable:
    st.info("Add a writable controller first (Devices page).")
else:
    t1, t2 = st.columns([2, 1])
    tctrl = t1.selectbox("Controller", writable, key="tol_ctrl")
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
# 4. TEST NOTIFICATIONS
# ===========================================================================
st.header("4 · Test notifications")
import notify
configured = notify.channels_configured()
if configured:
    st.caption("Configured channels: " + ", ".join(configured))
else:
    st.warning("No notification channels configured. Copy `notify.env.example` "
               "to `notify.env`, fill in Telegram / ntfy / Twilio, and restart "
               "the dashboard (`sudo systemctl restart kiln-dashboard`). The "
               "installed service loads `notify.env` automatically.")

tcol1, tcol2 = st.columns([3, 1])
test_msg = tcol1.text_input("Test message",
                            value="Test from the kiln dashboard 🔥",
                            label_visibility="collapsed")
if tcol2.button("📨 Send test", width="stretch", disabled=not configured):
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
# 5. REGISTER CONSOLE (advanced)
# ===========================================================================
st.header("5 · Register console (advanced)")
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
    st.info("Add a device first (Devices page).")
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
    if read_col.button("📖 Read register", width="stretch"):
        with st.spinner("Reading…"):
            try:
                vals = bridge.call(
                    monitor.read_registers_now(rc_ctrl, int(addr), 1), timeout=30)
                st.success(f"`{rc_ctrl}` register **{addr}** = **{vals[0]}**"
                           if vals else "no value returned")
            except Exception as e:
                st.error(f"read failed: {e}")

    if rc_ctrl in writable:
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
    else:
        st.caption(f"{rc_ctrl} is read-only; enable writes on the Devices page to "
                   "write registers.")

# ---------------------------------------------------------------------------
# 6. Bluetooth sharing (cooperative mode)
# ---------------------------------------------------------------------------
st.divider()
st.header("6 · Bluetooth sharing")
st.caption("Tip: to use QuickTune on one controller, use the **📶 Bluetooth** "
           "button on the Home page — it hands that controller over for a set "
           "time and resumes by itself. The settings below are the defaults.")
st.caption("A controller can only talk to one device at a time. In "
           "**cooperative mode** the monitor is a good neighbour: it connects "
           "just long enough to read temperature and output, then disconnects, "
           "so anyone on QuickTune is rarely blocked. If a controller can't be "
           "reached (someone's on QuickTune, or it's powered off) it's left "
           "alone — no alarms, no fast polling. Turn cooperative mode OFF only "
           "if you want the monitor to hold connections for fastest reaction.")

_ms = core.load_monitor_settings()
bc1, bc2 = st.columns(2)
coop = bc1.toggle("Cooperative mode (share the radio)",
                  value=bool(_ms.get("cooperative", True)),
                  help="ON: always release BLE between brief reads; never block "
                       "QuickTune. Strongly recommended.")
poll_min = bc2.slider("Read every (minutes)", 1, 15,
                      int(round(_ms.get("poll_interval_s", 300) / 60)),
                      help="How often to take a quick reading. Longer = less "
                           "chance of bumping a QuickTune user, coarser energy "
                           "data. 3–5 min is a good balance.")
st.caption("Even in cooperative mode, a real low-temperature reading still "
           "triggers the watchdog — sharing the radio doesn't disable safety.")

if st.button("💾 Save Bluetooth settings", type="primary"):
    new = {"cooperative": bool(coop), "poll_interval_s": float(poll_min * 60)}
    core.save_monitor_settings(new)
    # apply live so it takes effect without a restart
    monitor.cooperative = bool(coop)
    monitor.poll_interval_s = float(poll_min * 60)
    st.success(f"Saved. Reading every {poll_min} min, cooperative "
               f"{'on' if coop else 'off'} — applied now.")

# ---------------------------------------------------------------------------
# 7. Controller config snapshot & audit
# ---------------------------------------------------------------------------
st.divider()
st.header("7 · Controller config snapshot & audit")
st.caption("Capture a controller's full configuration (all 20 program tables "
           "plus the key config registers) with multi-read verification, and "
           "store it two ways: a canonical JSON with a SHA-256 fingerprint, and "
           "a human-readable text dump. Use it to confirm nothing gets changed "
           "along the way — re-snapshot any time and diff against a previous one.")

if not names:
    st.info("Add a device first (Devices page).")
else:
    snap_ctrl = st.selectbox("Controller", names, key="snap_ctrl")

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
