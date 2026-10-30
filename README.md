# novus-controller

A web dashboard, always-on monitor, and open-data API for **Novus** PID
kiln/furnace controllers over Bluetooth Low Energy — the same link Novus's
*QuickTune Mobile* app uses. Built and hardware-verified on the **N20K48**
(a PID process controller with a 20-program Ramp & Soak engine); it finds and
reads other Novus controllers too, and keeps them read-only until you say
otherwise.

> **Unofficial.** Not affiliated with Novus Automation. The protocol was
> reverse-engineered from QuickTune captures and cross-checked against Novus's
> protocol PDF — see [`docs/PROTOCOL.md`](docs/PROTOCOL.md). Writing the wrong
> register can ruin a firing, or worse on a furnace at 2000 °F+. Keep a hand
> on the physical controls.

---

## Install (one line)

On the Linux box that has the Bluetooth adapter (Ubuntu/Debian, Raspberry Pi
OS…):

```bash
curl -fsSL https://raw.githubusercontent.com/alibama/novus-controller/main/install.sh | bash
```

That clones the repo to `~/novus-controller`, makes a venv, installs
dependencies, generates a dashboard passphrase and an API write token, adds
you to the `bluetooth` group, installs and starts two systemd services
(`kiln-dashboard` on port 8501, `kiln-api` on port 8000), and prints the URLs
and the passphrase.

**Update** by running the same line again (or `bash ~/novus-controller/install.sh`).
It pulls, installs changed dependencies, byte-compiles and runs the test
suite, and only then restarts the services — a broken update never takes the
furnace watchdog down. Flags: `--no-services` (dev laptop), `--no-api`,
`--dir=PATH`.

Prefer Docker? `docker compose up -d` (Linux only; BlueZ over the host D-Bus —
see the notes at the top of [`docker-compose.yml`](docker-compose.yml)).

Then open `http://<that-machine>:8501`, enter the passphrase, and go to
**Devices → Scan**.

---

## What it does

**Find and name your controllers** — *Devices* page
- Scan over Bluetooth. Anything that looks like Novus (MAC prefix `00:26:A4`,
  manufacturer id 511, the Novus BLE service, or a model-like name) is listed
  first with the reason it matched; everything else can be shown too.
- **Identify** connects briefly and reads the device identification (vendor,
  product code, firmware) so you know exactly which model you have.
- Add it, name it, and pick its job: **furnace** (keep hot, watched,
  auto-recovered), **kiln** (runs programs), or **other** (monitor only). A
  one-line *Which one is the furnace?* picker covers the common case.
- Unverified models stay **read-only** until you flip *Allow writes*.
- **Diagnose** reads protection, unit, mode, limits, program and PID terms
  and says in plain words why a write won't stick (password, °C, manual mode,
  setpoint limits) or why it's running hot (output off but PV climbing →
  stuck SSR). A per-device controller password unlocks protected units.
- Changes apply to the running monitor immediately — no restart.

**Watch and control** — *Home* page
- Furnace temperature cards up top; kiln panels with PV/SP/output, the
  running program and its segments, RUN/STOP, and a sparkline.
- Always-on background monitor logs every controller to CSV and runs even with
  no browser open. Cooperative by default: it connects, reads, disconnects, so
  QuickTune on a phone is rarely blocked.
- **One Bluetooth control** on the Home page: *hand a controller to
  QuickTune for 15/30/60/120 minutes* — the monitor steps off that controller
  only, keeps watching the rest, and resumes by itself — plus the read
  interval. Same thing over the API (`pause` / `resume`), so a phone shortcut
  can ask for the radio before opening QuickTune.
- **Furnace watchdog**: if a furnace stays below your trigger temperature it
  does the same stop/start you'd do by hand, confirms the climb, and alerts
  you (Telegram / ntfy / Twilio SMS) — with guards so it never drives output
  on an implausible reading. An **over-temperature guard** alerts hard when
  a kiln sits far above setpoint with output off (a stuck SSR: cut power) and
  stops a controller that is still driving. Every threshold is editable on
  *Recovery & Logic*.

**Program it** — *Settings* page
- Write standard schedules (hold, anneal-down, full/tack fuse, slump, casting,
  fire polish) or build your own 9-segment program and write it to any slot.
- Guaranteed-soak tolerance editor, notification tester, raw register
  console, Bluetooth-sharing settings, and verified config snapshots with
  SHA-256 fingerprints and diffs.

**Understand it** — *Learn* page
- What a PID controller does (and a live read of your controller's PID
  terms), how Ramp & Soak programs work, why glass must be annealed, a
  thickness-based anneal-schedule calculator you can write straight to a slot,
  fusing/slumping/casting temperatures, heat-work, and what the watchdog does
  in plain language.

**Analyse it** — *Analytics* and *Notebook* pages
- PV/SP history, ramp-rate-vs-temperature, firing summaries with energy and
  cost.
- A firing notebook that compares what the program asked for to what the
  controller actually did, suggests adjustments, attaches notes and photos,
  compares two runs, and exports a self-contained HTML.

**Share it** — *Open Data & API* page and the `kiln-api` service
- Studio name, electricity rate, rated kW per controller (for cost estimates
  and attribution).
- Live API status, the public URL, the write token (show / regenerate), and
  the control-commands switch.
- One-click harvest: firings as CSV or a glassdatabase.org-ready XLSX, raw
  telemetry as a zip, and the exact `/summary` URL to hand to the Glass
  Database.

---

## The API: public read, authorized write

All reads are unauthenticated, CORS-open, CC-BY-4.0, and **never contain a
Bluetooth address or a secret**. Writes need `Authorization: Bearer <token>`
(the token lives in `api.env`; the installer generates it). Full reference in
[`docs/API.md`](docs/API.md).

| | Endpoint | Returns |
|---|---|---|
| GET | `/live` | latest PV / SP / output / program / watchdog state per controller |
| GET | `/summary` | totals and rollups — the glassdatabase.org contract |
| GET | `/firings`, `/firings.csv` | one row per firing: energy, cost, heat-work |
| GET | `/devices` | per-controller aggregates and model |
| GET | `/telemetry/{controller}` | raw logged samples (PV, SP, output, program) |
| GET | `/events`, `/notes`, `/commands` | audit trail, notes, queued commands |
| POST | `/notes` | attach a note to a controller or firing |
| POST | `/commands` | queue `run_program` / `stop_program` / `set_manual_setpoint` / `poll` / `diagnose` / `pause` / `resume` |

Commands are executed by the dashboard's monitor (the only process that owns
the Bluetooth radio) within a few seconds, through the same safety rules as
the on-screen buttons; read-only controllers reject them.

---

## Talk to one controller from Python

```python
import asyncio
from novus_client import NovusClient

async def main():
    c = NovusClient("kiln1", "00:26:A4:XX:XX:XX")   # your BLE address
    await c.connect()
    print(await c.read_device_id())                 # vendor / product / firmware
    await c.detect_scaling()                        # decimal places + °C/°F
    s = await c.read_state()
    print(f"PV={s.pv}{c.unit} SP={s.sp} output={s.output_pct}% running={s.running}")
    await c.disconnect()

asyncio.run(main())
```

Command-line helpers for probing a controller live in [`tools/`](tools/README.md).

## Verified vs unverified

**Verified end-to-end against real N20K48 hardware:** frame format and CRC,
all three function codes, the operation block (PV, SP, output, run/auto
flags), starting/stopping programs (247 / 213 / 214), reading and writing
full program tables, whole-degree °F scaling.

**Not fully verified — help wanted:** the password/session mechanism
(registers 51/53), power-loss resume behaviour (253?), "active segment"
semantics when programs link, and behaviour on sibling controllers (N1200,
N1040, N480D…). See the open questions in `docs/PROTOCOL.md`.

## Manual setup, hardening, troubleshooting

[`docs/SETUP.md`](docs/SETUP.md) covers what the installer does, running
without systemd, notifications, exposing the API to the internet, Bluetooth
permissions, laptop-as-server hardening, and updating/rolling back.

## Repository layout

```
novus-controller/
├── install.sh               # one-line install + update
├── run_dashboard.py         # service entry: starts the monitor, then Streamlit
├── kiln_dashboard.py        # Streamlit entry point (Home)
├── pages/                   # Devices, Analytics, Settings, Recovery & Logic,
│                            # Notebook, Learn, Open Data & API
├── app_core.py              # shared infra: async bridge, caches, live device reload
├── monitor.py               # always-on poller, CSV logs, watchdog, command queue, live.json
├── novus_client.py          # async BLE client (one per controller)
├── novus_protocol.py        # pure protocol: framing, CRC, parse/build (no I/O)
├── novus_models.py          # what we know about the Novus family; scan classification
├── devices.py               # device registry (roles, models, write permission)
├── commands.py              # API → monitor command queue (files)
├── paths.py                 # where data lives (KILN_DATA_DIR)
├── api.py                   # open-data API (FastAPI): public read, token write
├── programs_library.py      # standard firing schedules
├── glass_science.py         # annealing table/calculator, process and PID explainers
├── config_snapshot.py       # verified config capture: JSON + text + SHA-256
├── notebook.py / usage.py / runs.py   # firing analysis, energy/cost, run detection
├── notify.py                # Telegram / ntfy / Twilio
├── deploy/                  # systemd unit templates, install-services.sh, update.sh
├── docs/                    # PROTOCOL.md, SETUP.md, API.md
├── tools/                   # scanner, GATT dumper, register probes, exporters
└── tests/                   # 70+ unit tests, no hardware needed
```

## Architecture

```
Streamlit pages ──bridge.call(coro)──▶ AsyncBridge (one asyncio loop in a
                                       daemon thread, shared process-wide)
        Monitor (poller, CSV logs, ──────────┤  await client.read_state() / control()
        watchdog, command queue,             ▼
        live.json)                     NovusClient (one BLE connection per controller)
                                              │  await _request(frame, expect_fc)
                                              ▼
                                       novus_protocol  (pure: build/parse/CRC)
                                              │  bleak write_gatt_char / notify
                                              ▼
                                          BLE → Novus controller

kiln-api (FastAPI, separate service) ◀── reads logs/, state/live.json, notes/
                                     ──▶ writes commands/*.json  (picked up by Monitor)
```

## Contributing

PRs and issues welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md). Especially
valuable: confirming the register map, the password mechanism, the resume-mode
register, and reports from sibling Novus controllers (the *Identify* button
gives you the product code to quote).

## Acknowledgments

- **Novus Automation** for rugged, long-lived controllers
- [`bleak`](https://github.com/hbldh/bleak), the cross-platform async BLE library
- The glass community's firing-schedule knowledge (Bullseye and others) behind
  the standard programs and the Learn page

## License

MIT — see [`LICENSE`](LICENSE). Unofficial; not affiliated with or endorsed by
Novus Automation. "Novus" and "QuickTune" are referenced for interoperability
and identification only.
