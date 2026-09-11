# novus-n20k48-ble

An unofficial Python client and web app for **Novus N20K48** process
controllers over their Bluetooth Low Energy interface — the same interface
used by Novus's *QuickTune Mobile* app.

The protocol was reverse-engineered from packet captures of QuickTune Mobile
sessions against running N20K48 controllers and then cross-checked against
Novus's official communication-protocol PDF; see
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the full decode. **This work is
unofficial and not affiliated with Novus Automation.** It exists because the
controllers are deployed in a small glass studio that wanted one web dashboard
covering several kilns and a furnace at once, with logging, automatic
furnace-recovery, and phone notifications — things the official app doesn't do.

> [!IMPORTANT]
> Use at your own risk. Writing the wrong register on a kiln can ruin a
> firing — or worse, on a furnace running at 2000 °F+. Read
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md) before doing anything beyond reading
> PV/SP, and keep a hand on the physical controls.

---

## What it does

- Talks to **multiple N20K48 controllers concurrently** over BLE
- Reads process variable, setpoint, output power, and program-execution state
- Starts and stops **Ramp & Soak programs (1–20)** and writes full program tables
- A multipage **Streamlit** app:
  - **Home** — furnace temperatures up top, kiln control panels below
  - **Analytics** — PV/SP history, ramp-rate-vs-temperature, firing summaries
  - **Settings** — BLE scanner, device roles, a standard-program writer, a
    notification tester, and a raw register console
- **Always-on background monitor** that logs every controller to CSV and runs
  independent of any open browser tab
- A **furnace watchdog**: if a furnace drops below a temperature you set, it
  automatically does the same stop/start recovery you'd do by hand, confirms
  the temperature climbs back, and alerts you — with layered safety guards so
  it never drives output on an implausible reading
- **Notifications** via Telegram, [ntfy](https://ntfy.sh), and/or Twilio SMS
- **Config snapshot & audit**: capture every program table and config register with multi-read verification, stored as canonical JSON with a SHA-256 fingerprint plus a human-readable dump, and diff two snapshots to prove what changed
- **Open Data Studio**: the home page harvests per-firing usage as CC-BY open data — energy (kWh) and cost per run, downloadable as CSV or a glass-database-ready XLSX, plus raw telemetry — piping into [glassdatabase.org](https://glassdatabase.org)'s importer
- **Firing notebook**: record a run's actual curve, compare it segment-by-segment against the program the controller reports it ran, get concrete findings (didn't reach target / power-limited ramp / short soak / dropouts) and suggested adjustments, attach notes + photos, compare two runs to see why the glass differs, and export a self-contained shareable HTML

## Verified vs unverified

**Verified end-to-end against real hardware:**
- Frame format, framing CRC (Modbus CRC-16), all three function codes
- Reading the operation block (registers 200–214): PV, SP, output, run/auto flags
- Starting/stopping programs (program-select 247, auto 213, run 214)
- Reading and writing full program tables (start setpoint, 9 segments, tolerance)
- Multi-register writes; whole-degree °F scaling on the test units

**Not fully verified — help wanted:**
- The password/session mechanism on protected controllers (register 51/53)
- Power-loss resume behavior (register 253 is suspected but unconfirmed)
- "Active segment" semantics when programs link or loop
- Behavior on related Novus controllers (N1200, N480D, etc.)

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the complete open-questions list.

---

## Quick start

**Requirements:** a Linux host with a working Bluetooth adapter (developed on
Ubuntu 24), Python 3.10+, and an N20K48 with the BLE module.

```bash
git clone https://github.com/<your-user>/novus-n20k48-ble.git
cd novus-n20k48-ble
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Talk to one controller from Python
```python
import asyncio
from novus_client import NovusClient

async def main():
    c = NovusClient("kiln1", "00:26:A4:XX:XX:XX")   # your BLE address
    await c.connect()
    s = await c.read_state()
    print(f"PV={s.pv}  SP={s.sp}  output={s.output_pct}%  running={s.running}")
    if s.active_program:
        print(f"program {s.active_program}, segment {s.active_segment}")
    await c.disconnect()

asyncio.run(main())
```

### Find your controllers
```bash
python tools/kiln_bt_scanner.py        # look for the 00:26:A4 OUI / mfg id 511
python tools/explore_registers.py 00:26:A4:XX:XX:XX   # read-only probe
```

### Run the app
```bash
streamlit run kiln_dashboard.py
```
On first launch, open **Settings → Find controllers**, scan, add your devices,
and set each one's role (furnace or kiln). Or copy `kiln_config.example.py` to
`kiln_config.py` and list them there. Then open the URL Streamlit prints
(default `http://localhost:8501`).

> While the app holds a BLE connection, QuickTune Mobile cannot connect to that
> same controller — the bridge allows only one client at a time. The app polls
> intermittently and has a "Release BLE for QuickTune" button.

For a hardened always-on deployment (systemd units, notifications, remote
access), see [`docs/SETUP.md`](docs/SETUP.md).

---

## Repository layout

```
novus-n20k48-ble/
├── kiln_dashboard.py        # Streamlit entry point (Home page)
├── pages/                   # Analytics and Settings pages
├── app_core.py              # shared infra: async bridge, caches, auth, rendering
├── monitor.py               # always-on poller, CSV logging, furnace watchdog
├── novus_client.py          # async BLE client (one connection per controller)
├── novus_protocol.py        # pure protocol: framing, CRC, parse/build (no I/O)
├── devices.py               # device registry (roles), JSON persistence
├── programs_library.py      # standard firing schedules (hold, anneal, fuse, …)
├── config_snapshot.py       # verified config capture: JSON + human-readable + SHA-256
├── notebook.py              # firing lab notebook: intended-vs-actual analysis + HTML export
├── usage.py                 # per-firing energy (kWh) + cost estimation
├── runs.py                  # pure log readers / firing-run detection
├── notify.py                # Telegram / ntfy / Twilio notifications
├── kiln_analysis.py         # standalone read-only analytics (optional service)
├── kiln_calibrate.py        # ramp-rate characterization tool
├── kiln_config.example.py   # device-list template (copy to kiln_config.py)
├── notify.env.example       # notification config template
├── deploy/                  # systemd unit templates
├── docs/                    # PROTOCOL.md (the decode) and SETUP.md (runbook)
├── tools/                   # BLE scanner, GATT dumper, register probes (RE aids)
└── tests/                   # protocol + watchdog unit tests (stubbed, no hardware)
```

## Architecture

```
Streamlit pages ──bridge.call(coro)──▶ AsyncBridge (one asyncio loop in a
                                       daemon thread, shared process-wide)
                                              │
        Monitor (background poller, ──────────┤  await client.read_state() / control()
        CSV logging, furnace watchdog)        ▼
                                       NovusClient (one BLE connection per
                                       controller, held across reruns)
                                              │  await _request(frame, expect_fc)
                                              ▼
                                       novus_protocol  (pure: build/parse/CRC)
                                              │  bleak write_gatt_char / notify
                                              ▼
                                          BLE → N20K48
```

---

## Contributing

PRs and issues welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md). Especially
valuable: confirming/correcting the register map, the password mechanism, the
resume-mode register, and support for sibling Novus controllers. If you're on
the Novus team, the open questions are laid out in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) — happy to talk.

## Acknowledgments

- **Novus Automation** for rugged, long-lived controllers
- The maintainers of [`bleak`](https://github.com/hbldh/bleak), the
  cross-platform async BLE library this is built on
- The glass community's firing-schedule knowledge (Bullseye and others) that
  informed the standard-program templates

## License

MIT — see [`LICENSE`](LICENSE). Unofficial; not affiliated with or endorsed by
Novus Automation. "Novus" and "QuickTune" are referenced for interoperability
and identification only.
