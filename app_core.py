"""
app_core.py
===========
Shared infrastructure for the multipage app. Every page imports from here so
they all share ONE asyncio loop, ONE set of BLE connections, and ONE
background monitor (st.cache_resource is process-wide, so the home, analytics,
and settings pages reuse the same instances).

Holds: the async bridge, cached factories (bridge/clients/monitor/runtime),
live device add/remove (no restart needed), the password gate, PV/SP history
helpers, the open-data helpers, and the per-kiln panel renderer.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import streamlit as st

import paths
from devices import (load_devices, save_devices, Device, ROLE_FURNACE,
                     ROLE_KILN, ROLE_OTHER)
from monitor import Monitor, WatchdogConfig
from novus_client import NovusClient, ControllerState

REFRESH_INTERVAL_S = 5
HISTORY_LEN = 240
POLL_INTERVAL_S = 300.0   # intermittent polling: connect, read, free the radio

DATA_DIR = paths.DATA_DIR
LOG_DIR = paths.LOG_DIR
WATCHDOG_PATH = paths.WATCHDOG_PATH
SNAPSHOT_DIR = paths.SNAPSHOT_DIR
NOTEBOOK_DIR = paths.NOTEBOOK_DIR
STUDIO_PATH = paths.STUDIO_PATH
MONITOR_PATH = paths.MONITOR_PATH
API_ENV_PATH = paths.API_ENV_PATH
NOTES_DIR = paths.NOTES_DIR
COMMANDS_DIR = paths.COMMANDS_DIR

APP_NAME = "Kiln dashboard"


# ---------------------------------------------------------------------------
# Small JSON settings files
# ---------------------------------------------------------------------------
def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            print(f"[app_core] failed to read {path}: {e}")
    return default


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[app_core] failed to write {path}: {e}")


def load_monitor_settings() -> dict:
    d = {"cooperative": True, "poll_interval_s": POLL_INTERVAL_S}
    d.update(_read_json(MONITOR_PATH, {}))
    return d


def save_monitor_settings(d: dict) -> None:
    _write_json(MONITOR_PATH, d)


def _monitor_setting(key, default):
    return load_monitor_settings().get(key, default)


def load_studio_settings() -> dict:
    d = {"studio": "", "rate_per_kwh": 0.0, "currency": "USD"}
    d.update(_read_json(STUDIO_PATH, {}))
    return d


def save_studio_settings(d: dict) -> None:
    _write_json(STUDIO_PATH, d)


def studio_title() -> str:
    """Page/header title: the studio's own name if set, else a neutral one."""
    name = (load_studio_settings().get("studio") or "").strip()
    return name or APP_NAME


# ---------------------------------------------------------------------------
# API settings (api.env) — read-only here; the installer writes the token.
# ---------------------------------------------------------------------------
def load_api_env() -> dict:
    """Parse KEY=VALUE lines from api.env (comments and blanks ignored)."""
    out = {}
    if API_ENV_PATH.exists():
        for line in API_ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_api_env(values: dict) -> None:
    """Rewrite api.env keeping comments for keys we don't touch."""
    lines = API_ENV_PATH.read_text().splitlines() if API_ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in values:
                new_lines.append(f"{k}={values[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in values.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    API_ENV_PATH.write_text("\n".join(new_lines) + "\n")
    try:
        API_ENV_PATH.chmod(0o600)
    except Exception:
        pass


def host_hints() -> dict:
    """Best-effort hostname / LAN IP for showing URLs in the UI."""
    host = socket.gethostname()
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return {"hostname": host, "ip": ip}


# ---------------------------------------------------------------------------
# Open data helpers (usage rows, exports)
# ---------------------------------------------------------------------------
def build_usage_records(controllers=None, max_runs_each: int = 30) -> list[dict]:
    """Turn every detected run across the given controllers into open-data usage
    rows (energy + cost estimated per device rating and the studio rate)."""
    import notebook as _nb
    import usage as _usage
    devs = {d.name: d for d in get_devices_cached()}
    ss = load_studio_settings()
    names = controllers or list(devs.keys())
    rows = []
    for name in names:
        dev = devs.get(name)
        if not dev:
            continue
        for run in detect_recent_runs(name, max_runs=max_runs_each):
            raw = load_telemetry_window(name, run["start"], run["end"])
            samples = _nb.parse_rows(raw)
            if not samples:
                continue
            rows.append(_usage.summarize_run(
                name, dev.role, run["program"], samples,
                power_kw=getattr(dev, "power_kw", 0.0) or 0.0,
                rate=(ss.get("rate_per_kwh") or None),
                currency=ss.get("currency", "USD"),
                studio=ss.get("studio", "")))
    rows.sort(key=lambda r: r.get("start_utc", ""), reverse=True)
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def cached_usage_records() -> list[dict]:
    """Usage rows, recomputed at most once a minute (parsing every log on
    every 5-second page refresh was the single biggest CPU cost)."""
    return build_usage_records()


def usage_xlsx_bytes(rows: list[dict]) -> bytes:
    """A glass-database-ready workbook: sheet 'kiln_firings' (the data) plus a
    'data_dictionary' sheet. Returns xlsx bytes."""
    import io
    import pandas as pd
    import usage as _usage
    buf = io.BytesIO()
    df = pd.DataFrame(rows, columns=_usage.COLUMNS)
    dd = pd.DataFrame([{"column": k, "description": v}
                       for k, v in _usage.DATA_DICTIONARY.items()])
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="kiln_firings", index=False)
            dd.to_excel(xw, sheet_name="data_dictionary", index=False)
    except Exception:
        # openpyxl missing — fall back to CSV bytes
        buf = io.BytesIO(_usage.to_csv(rows).encode("utf-8"))
    return buf.getvalue()


def telemetry_zip_bytes(controllers=None) -> bytes:
    """Bundle raw per-day telemetry CSVs into a zip for open-data harvest."""
    import io
    import zipfile
    names = controllers or [d.name for d in get_devices_cached()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            d = LOG_DIR / name
            if not d.exists():
                continue
            for csv_path in sorted(d.glob("*.csv")):
                z.write(csv_path, arcname=f"{name}/{csv_path.name}")
    return buf.getvalue()


def load_telemetry_window(controller: str, start_iso: str, end_iso: str) -> list[dict]:
    """Raw CSV rows for `controller` within [start_iso, end_iso] (see runs.py)."""
    import runs
    return runs.load_telemetry_window(LOG_DIR, controller, start_iso, end_iso)


def detect_recent_runs(controller: str, max_runs: int = 6, gap_min: float = 20.0):
    """Newest-first contiguous program runs from the logs (see runs.py)."""
    import runs
    return runs.detect_recent_runs(LOG_DIR, controller, max_runs, gap_min)


def save_notebook(controller: str, nb_doc: dict) -> dict:
    """Persist a notebook as JSON (with embedded telemetry + images) and also
    write a shareable self-contained HTML. Returns {'json','html'}."""
    import notebook as _nbmod
    created = nb_doc["meta"].get("created_utc", "")
    slug = "".join(c if c.isalnum() else "-"
                   for c in nb_doc["meta"].get("title", "firing"))[:40].strip("-")
    stamp = created.replace(":", "").replace("-", "") or "entry"
    d = NOTEBOOK_DIR / controller
    d.mkdir(parents=True, exist_ok=True)
    base = d / f"{stamp}__{slug or 'firing'}"
    jpath = base.with_suffix(".json")
    hpath = base.with_suffix(".html")
    jpath.write_text(json.dumps(nb_doc, indent=2))
    hpath.write_text(_nbmod.to_html(nb_doc))
    return {"json": jpath, "html": hpath}


def list_notebooks(controller: str) -> list[Path]:
    d = NOTEBOOK_DIR / controller
    return sorted(d.glob("*.json"), reverse=True) if d.exists() else []


def load_notebook(path) -> dict:
    return json.loads(Path(path).read_text())


def save_snapshot(device_name: str, snapshot: dict) -> dict:
    """Write a config snapshot to disk in both forms (canonical JSON + human
    readable text), named by capture time. Returns {'json','txt','sha256'}."""
    import config_snapshot
    ts = snapshot["meta"]["captured_utc"].replace(":", "").replace("-", "")
    d = SNAPSHOT_DIR / device_name
    d.mkdir(parents=True, exist_ok=True)
    jpath = d / f"{ts}.json"
    tpath = d / f"{ts}.txt"
    jpath.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    tpath.write_text(config_snapshot.to_human(snapshot))
    return {"json": jpath, "txt": tpath, "sha256": snapshot["meta"]["sha256"]}


def list_snapshots(device_name: str) -> list[Path]:
    """Newest-first list of stored JSON snapshots for a controller."""
    d = SNAPSHOT_DIR / device_name
    if not d.exists():
        return []
    return sorted(d.glob("*.json"), reverse=True)


def load_snapshot(path) -> dict:
    return json.loads(Path(path).read_text())


def load_notes(device: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Notes posted through the API (notes/notes.jsonl), newest first."""
    p = NOTES_DIR / "notes.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            n = json.loads(line)
        except Exception:
            continue
        if device and n.get("device") != device:
            continue
        out.append(n)
    return list(reversed(out))[:limit]


# ---------------------------------------------------------------------------
# Watchdog settings persistence
# ---------------------------------------------------------------------------
# Watchdog fields that are editable from the UI and persisted across restarts.
WATCHDOG_FIELDS = [
    "enabled", "auto_recover", "expected_setpoint", "low_margin", "hold_program",
    "keep_hot", "dropout_confirm_s", "cant_hold_confirm_s", "sustain_s",
    "recover_floor", "confirm_after_s", "confirm_rise", "max_recoveries",
    "recover_on_cant_hold",
]


def load_watchdog_settings() -> dict:
    return _read_json(WATCHDOG_PATH, {})


def save_watchdog_settings(monitor) -> None:
    """Persist per-furnace trigger temps AND the full per-device watchdog config
    so the operational state survives restarts and is the source of truth."""
    from dataclasses import asdict
    data = {
        "recover_threshold": {k: v for k, v in monitor.recover_threshold.items()
                              if v is not None},
        "config": {},
    }
    for name, cfg in monitor.watchdogs.items():
        d = asdict(cfg)
        data["config"][name] = {k: d[k] for k in WATCHDOG_FIELDS if k in d}
    _write_json(WATCHDOG_PATH, data)


def apply_watchdog_settings(monitor) -> None:
    data = load_watchdog_settings()
    for name, thr in (data.get("recover_threshold") or {}).items():
        if name in monitor.clients:
            monitor.recover_threshold[name] = float(thr)
    for name, saved in (data.get("config") or {}).items():
        cfg = monitor.watchdogs.get(name)
        if not cfg:
            continue
        for k in WATCHDOG_FIELDS:
            if k in saved:
                setattr(cfg, k, saved[k])


def watchdog_for(d: Device) -> WatchdogConfig:
    """Default watchdog policy for a device, by role. Program 1 is the 'set
    and forget' hold everywhere. Furnaces are kept hot from any state; kilns
    are only rescued while actively holding program 1 (so real firings on
    other programs are never disturbed). 'other' devices and unverified
    controller models are monitored but never driven."""
    is_furnace = (d.role == ROLE_FURNACE)
    drive_ok = d.writes_allowed and d.verified and d.role != ROLE_OTHER
    hold = float(d.expected_setpoint or 0.0)
    return WatchdogConfig(
        enabled=drive_ok and hold > 0,
        expected_setpoint=hold if hold > 0 else 896.0,
        low_margin=100.0,                        # trigger this far below hold
        auto_recover=drive_ok,
        hold_program=1,                          # program 1 = the hold
        keep_hot=is_furnace,
        # Plausibility floor scales to the hold: ~half the target. Below it,
        # a reading is too low/uncertain to auto-drive (TC-fault guard).
        recover_floor=max(200.0, round((hold if hold > 0 else 896.0) * 0.5)),
    )


def device_meta(d: Device) -> dict:
    return {"role": d.role, "model": d.model, "verified": d.verified,
            "writes_allowed": d.writes_allowed}


# ---------------------------------------------------------------------------
# Async bridge + process-wide singletons
# ---------------------------------------------------------------------------
class AsyncBridge:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="kiln-async-loop")
        self.thread.start()

    def call(self, coro, timeout: float = 10.0):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


@st.cache_resource
def get_bridge() -> AsyncBridge:
    return AsyncBridge()


@st.cache_resource
def get_devices_cached() -> list[Device]:
    # Cached so all pages agree on the device set within a server lifetime.
    # reload_devices() clears it after the Devices page saves.
    return load_devices()


@st.cache_resource
def get_runtime_state() -> dict:
    return {"ble_released": False}


@st.cache_resource
def get_clients(_bridge: AsyncBridge) -> dict[str, NovusClient]:
    """One NovusClient per device. The Monitor owns connections. The dict is
    shared with the monitor and mutated in place by reload_devices()."""
    return {d.name: NovusClient(d.name, d.address) for d in get_devices_cached()}


@st.cache_resource
def get_monitor(_bridge: AsyncBridge, _clients: dict) -> Monitor:
    """Start the always-on monitor once."""
    devs = get_devices_cached()
    watchdogs = {d.name: watchdog_for(d) for d in devs}
    m = Monitor(_clients, watchdogs,
                poll_interval_s=_monitor_setting("poll_interval_s", POLL_INTERVAL_S),
                hold_connection=False,
                cooperative=_monitor_setting("cooperative", True))
    for d in devs:
        m.writable[d.name] = bool(d.writes_allowed)
        m.meta[d.name] = device_meta(d)
    m.start(_bridge.loop)
    return m


def boot():
    """Call at the top of every page after the password gate. Returns
    (bridge, clients, monitor, devices, runtime)."""
    bridge = get_bridge()
    clients = get_clients(bridge)
    monitor = get_monitor(bridge, clients)
    apply_watchdog_settings(monitor)
    devices = get_devices_cached()
    runtime = get_runtime_state()
    return bridge, clients, monitor, devices, runtime


def reload_devices(bridge, clients, monitor) -> list[Device]:
    """Re-read devices.json and apply the difference to the LIVE monitor —
    add new controllers, drop removed ones, swap a changed address, update
    role/model/permissions — without restarting the service. Returns the new
    device list (and refreshes the cached one every page uses)."""
    new = load_devices()
    by_name = {d.name: d for d in new}
    # removed
    for name in list(clients.keys()):
        if name not in by_name:
            try:
                bridge.call(monitor.remove_device(name), timeout=15)
            except Exception as e:
                print(f"[app_core] remove {name} failed: {e}")
            clients.pop(name, None)
    # added or changed
    for d in new:
        c = clients.get(d.name)
        if c is not None and c.address.upper() != d.address.upper():
            try:
                bridge.call(monitor.remove_device(d.name), timeout=15)
            except Exception as e:
                print(f"[app_core] replace {d.name} failed: {e}")
            clients.pop(d.name, None)
            c = None
        if c is None:
            c = NovusClient(d.name, d.address)
            clients[d.name] = c
            monitor.add_device(c, watchdog_for(d), writable=d.writes_allowed,
                               meta=device_meta(d))
        else:
            monitor.writable[d.name] = bool(d.writes_allowed)
            monitor.meta[d.name] = device_meta(d)
            cfg = monitor.watchdogs.get(d.name)
            fresh = watchdog_for(d)
            if cfg is None:
                monitor.watchdogs[d.name] = fresh
            else:
                # Keep the operator's tuned timers; update the role-derived bits.
                cfg.expected_setpoint = fresh.expected_setpoint
                cfg.keep_hot = fresh.keep_hot
                if not fresh.enabled:          # became read-only / "other"
                    cfg.enabled = False
                    cfg.auto_recover = False
    apply_watchdog_settings(monitor)
    get_devices_cached.clear()
    monitor._write_live()
    return get_devices_cached()


# --- BLE release / reclaim -------------------------------------------------
def release_ble(bridge, clients, monitor):
    monitor.paused = True
    time.sleep(1.2)
    for name, c in list(clients.items()):
        try:
            bridge.call(c.disconnect(), timeout=8)
        except Exception as e:
            print(f"[{name}] release failed: {e}")
    get_runtime_state()["ble_released"] = True


def reclaim_ble(bridge, clients, monitor):
    # Connections are re-established lazily by the next poll (cooperative
    # mode never holds them anyway), so just resume the monitor.
    monitor.paused = False
    get_runtime_state()["ble_released"] = False


# --- password gate ---------------------------------------------------------
def require_password() -> bool:
    if st.session_state.get("auth_ok"):
        return True
    try:
        expected = st.secrets.get("dashboard_password", None)
    except Exception:
        expected = None
    if not expected:
        st.error("No `dashboard_password` set in `.streamlit/secrets.toml` — "
                 "refusing to start unauthenticated. The installer "
                 "(`install.sh`) creates one for you; or write the file by hand:\n\n"
                 "```toml\ndashboard_password = \"choose-a-passphrase\"\n```")
        return False
    st.title(f"🔥 {studio_title()}")
    pw = st.text_input("Passphrase", type="password")
    if pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect passphrase")
    return False


# --- history (per-session, for the sparklines) -----------------------------
def init_history(devices: list[Device]):
    if "history" not in st.session_state:
        st.session_state.history = {}
    for d in devices:
        st.session_state.history.setdefault(d.name, deque(maxlen=HISTORY_LEN))
    if "started_at" not in st.session_state:
        st.session_state.started_at = time.time()


def push_history(name: str, state: ControllerState):
    h = st.session_state.history.get(name)
    if h is None:
        return
    if state.connected and state.pv is not None and state.sp is not None:
        h.append({"t": time.time() - st.session_state.started_at,
                  "PV": state.pv, "SP": state.sp})


def latest_state(monitor, dev: Device) -> ControllerState:
    return monitor.latest.get(dev.name) or ControllerState(
        name=dev.name, address=dev.address, connected=False, pv=None, sp=None)


def unit_of(clients: dict, name: str) -> str:
    c = clients.get(name)
    return getattr(c, "unit", "°") or "°"


# --- segment table renderer (shared) ---------------------------------------
def render_segments(name: str, program_num: int, monitor, active_segment=None):
    prog = monitor.programs.get(name, {}).get(program_num)
    if prog is None:
        st.caption(f"Program {program_num}: not loaded yet — use “Load all programs”.")
        return
    if not prog.segments:
        st.caption(f"Program {program_num}: empty.")
        return
    import pandas as pd
    rows = []
    start_sp = getattr(prog, "start_setpoint", None)
    if start_sp is not None:
        rows.append({"seg": "start", "target °": f"{start_sp:g}",
                     "time (min)": "—", "event": "—"})
    for i, seg in enumerate(prog.segments, start=1):
        rows.append({
            "seg": ("▶ " if active_segment == i else "") + str(i),
            "target °": f"{seg.setpoint:g}",
            "time (min)": seg.duration_minutes,
            "event": seg.event or "",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    tol = getattr(prog, "tolerance", 0)
    st.caption(f"tolerance ±{tol:g}°"
               + (f" · links to program {prog.link_to}" if getattr(prog, "link_to", 0) else ""))


# --- per-kiln / furnace control panel (shared by the home page) ------------
def render_kiln_panel(col, dev: Device, state: ControllerState, bridge, monitor,
                      is_furnace: bool = False, clients: Optional[dict] = None):
    name = dev.name
    uid = dev.address            # unique per controller; use in widget keys so
                                 # two devices with the same name can't collide
    unit = unit_of(clients or {}, name)
    can_write = bool(dev.writes_allowed) and dev.role != ROLE_OTHER
    with col:
        if not state.connected:
            st.subheader(f"⚫ {name}")
            st.caption("not connected on last poll (normal between poll cycles)")
        elif state.active_program:
            st.subheader(f"🔥 {name}")
        else:
            st.subheader(f"⚪ {name}")
        st.caption((dev.model_info.key if dev.model else "model unknown")
                   + (" · PID" if dev.model_info.pid else "")
                   + ("" if dev.verified else " · unverified")
                   + ("" if can_write else " · read-only"))

        m1, m2, m3 = st.columns(3)
        m1.metric("PV", f"{state.pv:g}{unit}" if state.pv is not None else "—")
        m2.metric("SP", f"{state.sp:g}{unit}" if state.sp is not None else "—")
        m3.metric("Output", f"{state.output_pct:g}%" if state.output_pct is not None else "—")

        if is_furnace:
            _render_furnace_status(name, monitor, uid, unit)

        if state.active_program:
            seg = state.active_segment if state.active_segment is not None else "?"
            st.success(f"**Running program {state.active_program}** · segment {seg}")
            with st.expander("Segments — what's coming", expanded=not is_furnace):
                render_segments(name, state.active_program, monitor,
                                active_segment=state.active_segment)
            if can_write and st.button("⏹ STOP", key=f"stop_{uid}", width="stretch"):
                with st.spinner("Stopping… (grabbing Bluetooth)"):
                    try:
                        bridge.call(monitor.control(name, "stop_program"), timeout=30)
                        if is_furnace:
                            # Don't let the watchdog instantly restart what the
                            # operator just shut off.
                            monitor.recovery_armed[name] = False
                        st.rerun()
                    except Exception as e:
                        st.error(f"stop failed: {e}")
        else:
            st.info("idle" if state.connected else "no reading yet")
            if can_write:
                c1, c2 = st.columns([2, 1])
                with c1:
                    program = st.selectbox("Program", range(1, 21), key=f"prog_{uid}",
                                           label_visibility="collapsed")
                with c2:
                    if st.button("▶ RUN", key=f"start_{uid}", width="stretch"):
                        with st.spinner("Starting… (grabbing Bluetooth)"):
                            try:
                                bridge.call(monitor.control(name, "run_program", program),
                                            timeout=30)
                                if is_furnace:
                                    monitor.recovery_armed[name] = True
                                st.rerun()
                            except Exception as e:
                                st.error(f"start failed: {e}")
                with st.expander(f"Preview program {program} segments"):
                    render_segments(name, program, monitor)
            else:
                st.caption("Read-only: this controller is monitored and logged "
                           "but never driven. Change that on the Devices page.")

        history = list(st.session_state.history.get(name, []))
        if len(history) >= 2:
            import pandas as pd
            df = pd.DataFrame(history).set_index("t")
            st.line_chart(df, height=140)


def _render_furnace_status(name: str, monitor, uid: str = "", unit: str = "°"):
    """Watchdog state + auto-recovery arm/disarm for a furnace panel."""
    armed = monitor.recovery_armed.get(name, True)
    ws = getattr(monitor, "_watch", {}).get(name)
    worried = name in getattr(monitor, "worried", set())
    needs_human = bool(getattr(ws, "needs_human", False)) if ws else False
    cfg = monitor.watchdogs.get(name)

    if cfg and not cfg.enabled:
        st.caption("Watchdog off for this controller (enable it on Recovery & Logic).")
        return
    if needs_human:
        st.error("⚠️ Watchdog halted — needs a human. Auto-recovery stopped. "
                 "Check the furnace, then re-arm below.")
    elif worried:
        st.warning("👀 Watchdog is worried — temperature is low, watching closely.")

    cc1, cc2 = st.columns([3, 2])
    with cc1:
        st.caption(f"Auto-recovery: {'🟢 **armed**' if armed else '🔴 **disarmed**'}")
    with cc2:
        if armed:
            if st.button("Disarm", key=f"disarm_{uid}", width="stretch",
                         help="Stop the watchdog from auto-restarting this furnace."):
                monitor.recovery_armed[name] = False
                if ws:
                    ws.last_alerted_case = None
                st.rerun()
        else:
            if st.button("Re-arm", key=f"rearm_{uid}", type="primary",
                         width="stretch",
                         help="Let the watchdog auto-restart this furnace again."):
                monitor.recovery_armed[name] = True
                if ws:
                    ws.needs_human = False
                    ws.last_alerted_case = None
                st.rerun()

    # --- auto-restart trigger temperature (UI-set, persisted) ---
    expected = cfg.expected_setpoint if cfg else 2100.0
    floor = cfg.recover_floor if cfg else 1000.0
    cur = monitor.recover_threshold.get(name)
    cur_line = cur if cur is not None else (expected - (cfg.low_margin if cfg else 100.0))
    with st.expander(f"🛠 Auto-restart trigger — currently below {cur_line:g}{unit}"):
        new = st.number_input(
            f"Restart if temperature stays below ({unit})",
            min_value=int(floor) + 1, max_value=int(expected) - 1,
            value=int(cur_line), step=10, key=f"thr_{uid}")
        st.caption(
            f"**What this does:** if **{name}** stays below **{int(new)}{unit}**, the "
            f"watchdog will automatically **STOP it and RUN the same program again** "
            f"— the exact off/on you do by hand — then confirm the temperature "
            f"climbs back. Holding target is {expected:g}{unit}; it won't auto-drive "
            f"from below {floor:g}{unit} (that needs a human).")
        if st.button("Save trigger", key=f"savethr_{uid}", type="primary"):
            monitor.recover_threshold[name] = float(new)
            save_watchdog_settings(monitor)
            if ws:
                ws.last_alerted_case = None
            st.success(f"Saved — auto-restart {name} if it drops below {int(new)}{unit}.")
            st.rerun()
