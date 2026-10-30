"""
Open Data & API
===============
The administrative side of sharing data: what this studio publishes, where
it is reachable, the key other apps need to write back, and the one-click
harvest for glassdatabase.org. Everything an outside consumer needs is on
this page; nothing here exposes a Bluetooth address.
"""
from __future__ import annotations

import json
import secrets
import urllib.request

import streamlit as st

import app_core as core
import usage as _usage
from devices import save_devices

st.set_page_config(page_title="Open Data & API", page_icon="🌐", layout="wide")

if not core.require_password():
    st.stop()

bridge, clients, monitor, devices, runtime = core.boot()

st.title("🌐 Open Data & API")
st.caption("This studio's kiln and furnace operating data is published as open "
           "data (CC-BY-4.0): live readings, one row per firing with estimated "
           "energy and cost, and the raw telemetry. Other apps read it without "
           "a key; writing back (notes, commands) needs the token below.")

hints = core.host_hints()
api_env = core.load_api_env()
public_url = (api_env.get("API_PUBLIC_URL") or "").rstrip("/")
local_api = f"http://{hints['ip'] or hints['hostname']}:8000"

# ===========================================================================
# 1. Studio identity & energy pricing (attribution + cost estimates)
# ===========================================================================
st.header("1 · Studio & energy settings")
_ss = core.load_studio_settings()
es1, es2, es3 = st.columns([2, 1, 1])
studio_name = es1.text_input("Studio name (shown as attribution and as the app title)",
                             value=_ss.get("studio", ""))
rate = es2.number_input("Electricity rate / kWh", min_value=0.0, max_value=2.0,
                        value=float(_ss.get("rate_per_kwh", 0.0)), step=0.01,
                        format="%.3f")
currency = es3.text_input("Currency", value=_ss.get("currency", "USD"))

st.markdown("**Rated power per controller (kW at 100% output)** — energy is "
            "estimated as output% × rated kW; calibrate with a clamp meter at "
            "100% output for the truest numbers.")
new_kw = {}
if devices:
    pk_cols = st.columns(max(1, min(4, len(devices))))
    for i, dev in enumerate(devices):
        new_kw[dev.name] = pk_cols[i % len(pk_cols)].number_input(
            dev.name, min_value=0.0, max_value=200.0,
            value=float(getattr(dev, "power_kw", 0.0) or 0.0), step=0.5,
            key=f"kw_{dev.address}")
if core.is_admin() and st.button("💾 Save studio & energy settings", type="primary"):
    core.save_studio_settings({"studio": studio_name.strip(),
                               "rate_per_kwh": float(rate),
                               "currency": (currency or "USD").strip()})
    for dev in devices:
        dev.power_kw = float(new_kw.get(dev.name, dev.power_kw or 0.0))
    save_devices(devices)
    core.reload_devices(bridge, clients, monitor)
    core.cached_usage_records.clear()
    st.success("Saved.")
    st.rerun()

st.divider()

# ===========================================================================
# 2. Where the data lives (the API)
# ===========================================================================
st.header("2 · The API — read without a key")
st.markdown(
    f"The open-data API runs as the `kiln-api` service on port **8000**. On "
    f"this network it is `{local_api}`. To let the internet (and "
    f"glassdatabase.org) read it, expose port 8000 over HTTPS — a Tailscale "
    f"Funnel, Cloudflare Tunnel, or your reverse proxy — and record that public "
    f"address below so it shows up here and in the index.")

# live health check of the local API
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as r:
        h = json.loads(r.read().decode())
    age = h.get("live_age_s")
    st.success(f"API is up locally · live readings "
               + (f"{age:g}s old" if age is not None else "not written yet")
               + (" · writes enabled" if h.get("writes_enabled") else
                  " · **writes disabled** (no token in api.env)"))
except Exception as e:
    st.warning(f"The API isn't answering on this machine ({e}). Start it with "
               "`sudo systemctl start kiln-api` or run "
               "`.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000`.")

pub = st.text_input("Public URL of the API (optional, https)", value=public_url,
                    placeholder="https://kilns.example.ts.net:10000")
if core.is_admin() and st.button("Save public URL"):
    core.save_api_env({"API_PUBLIC_URL": pub.strip().rstrip("/")})
    st.success("Saved to api.env. Restart the API (`sudo systemctl restart "
               "kiln-api`) for it to show in the index.")
    st.rerun()

base = public_url or local_api
st.markdown("**Endpoints** (all GET, no key, CORS-open, no device addresses):")
st.code("\n".join([
    f"{base}/live          latest PV / SP / output / program per controller",
    f"{base}/summary       totals + rollups (glassdatabase.org contract)",
    f"{base}/firings       one row per firing: energy, cost, heat-work",
    f"{base}/firings.csv   the same as CSV",
    f"{base}/devices       per-controller aggregates + model",
    f"{base}/events        watchdog / audit trail",
    f"{base}/notes         notes posted through the API",
    f"{base}/docs          interactive OpenAPI docs",
]), language="text")

with st.expander("Give glassdatabase.org this studio's data"):
    st.markdown(
        f"1. Expose the API publicly (above) and confirm `{base}/summary` opens "
        "in a browser.\n"
        f"2. In the Glass Database, set this studio's **STUDIO_DATA_URL** to "
        f"`{base}/summary`. It reads `firings`, `energy_kwh`, `cost_usd`, "
        "`since`, `studio`, `license` from the top level.\n"
        "3. For a one-off import instead, download the **XLSX** in section 4 "
        "and drop it in the importer's uploads folder:\n"
        "   ```\n   python -m central.ingest build --uploads /path/to/folder\n   ```")

st.divider()

# ===========================================================================
# 3. Writing back — the token
# ===========================================================================
st.header("3 · Writing back — needs the token")
st.markdown("Any app holding the **write token** can post a note against a "
            "controller or firing, or queue a command (run/stop a program, "
            "hold a setpoint, take a reading). Commands run through the same "
            "safety rules as the buttons here, and read-only controllers refuse "
            "them. Reads never need the token.")
if core.is_admin():
    tok = api_env.get("API_WRITE_TOKEN", "")
    allow_ctrl = (api_env.get("API_ALLOW_CONTROL", "1").lower() in ("1", "true", "yes", "on"))
    t1, t2, t3 = st.columns([3, 1, 1])
    if tok:
        show = st.session_state.get("show_token", False)
        t1.code(tok if show else tok[:4] + "•" * max(8, len(tok) - 8) + tok[-4:], language="text")
        if t2.button("👁 Show" if not show else "Hide", width="stretch"):
            st.session_state["show_token"] = not show
            st.rerun()
    else:
        t1.warning("No token in `api.env` — writes are disabled. Generate one:")
    if t3.button("🔁 New token", width="stretch",
                 help="Generates a fresh token; apps using the old one stop working."):
        core.save_api_env({"API_WRITE_TOKEN": secrets.token_hex(16)})
        st.session_state["show_token"] = True
        st.success("New token written to api.env. Restart the API to use it: "
                   "`sudo systemctl restart kiln-api`.")
        st.rerun()

    ctl = st.toggle("Allow control commands through the API (run/stop/setpoint)",
                    value=allow_ctrl)
    if ctl != allow_ctrl:
        core.save_api_env({"API_ALLOW_CONTROL": "1" if ctl else "0"})
        st.success("Saved. Restart the API to apply.")

    with st.expander("Examples (curl)"):
        st.code(f"""# post a note
    curl -X POST {base}/notes -H "Authorization: Bearer $TOKEN" \\
         -H "Content-Type: application/json" \\
         -d '{{"controller":"kiln1","text":"6mm clear, two layers, kiln wash fresh","author":"anson"}}'

    # run program 3 on kiln1 (queued; the dashboard executes it within seconds)
    curl -X POST {base}/commands -H "Authorization: Bearer $TOKEN" \\
         -H "Content-Type: application/json" \\
         -d '{{"controller":"kiln1","action":"run_program","program":3,"source":"scheduler"}}'

    # check what happened
    curl {base}/commands/<id>""", language="bash")

else:
    st.caption("The write token and its switches are only shown to admin sessions.")
st.divider()

# ===========================================================================
# 4. Harvest (downloads)
# ===========================================================================
st.header("4 · Harvest: firings as open data")
try:
    rows = core.cached_usage_records()
except Exception as e:
    rows = []
    st.caption(f"(usage build unavailable: {e})")

if rows:
    total_kwh = sum(r["energy_kwh"] or 0 for r in rows)
    total_cost = sum(r["cost"] or 0 for r in rows)
    cur = next((r["currency"] for r in rows if r.get("currency")), "")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Firings logged", len(rows))
    m2.metric("Energy (est.)", f"{total_kwh:,.0f} kWh")
    m3.metric("Cost (est.)", f"{total_cost:,.2f} {cur}" if total_cost else "—")
    m4.metric("Since", min((r["date_utc"] for r in rows if r["date_utc"]), default="—"))

    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇ Firings (CSV)", _usage.to_csv(rows),
                       file_name="kiln_firings.csv", mime="text/csv", width="stretch")
    d2.download_button("⬇ Firings (XLSX · glass-database ready)",
                       core.usage_xlsx_bytes(rows), file_name="kiln_firings.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch")
    d3.download_button("⬇ Raw telemetry (ZIP of CSVs)", core.telemetry_zip_bytes(),
                       file_name="kiln_telemetry.zip", mime="application/zip",
                       width="stretch")
    with st.expander("Preview the rows"):
        import pandas as pd
        st.dataframe(pd.DataFrame(rows, columns=_usage.COLUMNS), width="stretch",
                     hide_index=True)
else:
    st.info("No completed firings logged yet — run a program and the harvest "
            "fills in. Set the studio name, electricity rate, and each "
            "controller's rated kW in section 1 so rows carry cost and attribution.")

st.divider()

# ===========================================================================
# 5. What others wrote back
# ===========================================================================
st.header("5 · Notes and commands received")
notes = core.load_notes(limit=30)
cmds = []
try:
    import commands as cmdq
    cmds = cmdq.recent(core.COMMANDS_DIR, 30)
except Exception:
    pass
n1, n2 = st.columns(2)
with n1:
    st.markdown("**Notes** (via `POST /notes`)")
    if not notes:
        st.caption("none yet")
    for n in notes:
        st.markdown(f"- `{n.get('created_utc','')[:16]}` **{n.get('controller')}** "
                    f"— {n.get('text')}"
                    + (f" _({n.get('author')})_" if n.get("author") else ""))
with n2:
    st.markdown("**Commands** (via `POST /commands`)")
    if not cmds:
        st.caption("none yet")
    for c in cmds:
        icon = {"done": "✅", "failed": "❌", "rejected": "⛔", "pending": "⏳",
                "running": "▶️"}.get(c.get("status"), "•")
        st.markdown(f"- {icon} `{c.get('created_utc','')[:16]}` **{c.get('device')}** "
                    f"{c.get('action')} {c.get('args') or ''} → {c.get('status')}"
                    + (f" — {c.get('error')}" if c.get("error") else ""))
