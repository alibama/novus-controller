# novus-controller
novus n20k48 streamlit controller software


# novus-n20k48-ble

An unofficial Python client and web dashboard for **Novus N20K48** process
controllers over their Bluetooth Low Energy interface — the same interface
used by Novus's *QuickTune Mobile* app.

The protocol was reverse-engineered from packet captures of a single
QuickTune Mobile session against a running N20K48; see [`PROTOCOL.md`](PROTOCOL.md)
for the full decode. **This work is unofficial and not affiliated with
Novus Automation.** It exists because the controllers are deployed in
a small glass studio where the operator wanted a single web dashboard
covering several kilns at once — a use case the official mobile app
doesn't cover.

> [!IMPORTANT]
> Use at your own risk. Writing to the wrong register on a kiln can
> ruin a firing or worse. Read [`PROTOCOL.md`](PROTOCOL.md) before
> doing anything beyond reading PV/SP.

---

## What you can do with it

- Connect to multiple N20K48 controllers concurrently over BLE
- Read process variable, setpoint, output power, and program-execution state
- Start and stop Ramp & Soak programs (1–20)
- Watch all of it in a live Streamlit dashboard with per-kiln PV/SP charts

## What's verified vs unverified

**Verified end-to-end against real hardware:**
- Frame format, framing CRC, all function codes
- Read of registers 200–215 (operation cycle, program execution state)
- Writing register 214 to start and stop programs
- Multi-register writes for config saves

**Not yet verified:**
- Full program-table register layout (segments parse correctly, but
  metadata fields in the 2600+ range are not labeled)
- Password protection on protected controllers — one of the test kilns
  rejects writes until a password is supplied; the auth mechanism is
  unknown
- Decimal-point auto-detection (currently a constant on the client)
- "Active segment" semantics when programs loop or link to others

See [`PROTOCOL.md`](PROTOCOL.md) for the complete list of open questions.

---

## Quick start

### Requirements
- Linux host with a working Bluetooth adapter (tested on Ubuntu 24)
- Python 3.10+
- An N20K48 controller with the BLE module

### Install
```bash
git clone https://github.com/<your-user>/novus-n20k48-ble.git
cd novus-n20k48-ble
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Scan for nearby kilns
```bash
python kiln_bt_scanner.py
```
Look for devices with MAC prefix `00:26:A4` (Novus's OUI) and manufacturer
ID `511` (0x01FF).

### Probe a single controller
Read PV, SP, output, and program state without writing anything:
```bash
python explore_registers.py <BT_ADDRESS>
```

### Talk to one kiln from Python
```python
import asyncio
from novus_client import NovusClient

async def main():
    c = NovusClient("bubba", "00:26:A4:00:5F:4E")
    await c.connect()

    state = await c.read_state()
    print(f"PV={state.pv}  SP={state.sp}  output={state.output_pct}%")
    if state.active_program:
        print(f"Running program {state.active_program}, segment {state.active_segment}")

    # Start program 1
    await c.run_program(1)
    await asyncio.sleep(2)

    # Stop it
    await c.stop_program()

    await c.disconnect()

asyncio.run(main())
```

### Launch the dashboard
```bash
# Edit kiln_dashboard.py and replace the KILNS list with your controllers
streamlit run kiln_dashboard.py
```
Then open the URL Streamlit prints (default `http://localhost:8501`).

Three columns, one per controller. Each shows:
- PV / SP / Output metrics, big and live
- Run / Stop controls with a program selector
- A live PV/SP chart from the last ~20 minutes
- A reconnect button if the controller drops

**While the dashboard is running, QuickTune Mobile cannot connect to the
same controller** — the BLE bridge appears to allow only one client at a time.

### Optional: expose over Tailscale
```bash
sudo tailscale serve --bg --https=443 http://localhost:8501
```

---

## Repository layout

```
novus-n20k48-ble/
├── README.md                  ← you are here
├── PROTOCOL.md                ← detailed protocol decode (for Novus engineers)
├── LICENSE                    ← MIT
├── requirements.txt
├── novus_protocol.py          ← pure protocol: framing, CRC, parse/build
├── novus_client.py            ← async BLE client (one connection per controller)
├── kiln_dashboard.py          ← Streamlit dashboard
├── kiln_bt_scanner.py         ← BLE scanner for nearby Novus controllers
├── gatt_enumerate.py          ← GATT service/characteristic dumper
└── explore_registers.py       ← read-only diagnostic probe
```

---

## Architecture

```
┌──────────────────────────┐
│  Streamlit dashboard     │   sync UI calls
│  (kiln_dashboard.py)     │
└──────────────┬───────────┘
               │  bridge.call(coroutine)
               ▼
┌──────────────────────────┐   one daemon thread runs an asyncio
│  AsyncBridge             │   event loop forever; sync code submits
│  (in dashboard)          │   coroutines via run_coroutine_threadsafe
└──────────────┬───────────┘
               │  await client.read_state()
               ▼
┌──────────────────────────┐   one persistent BLE connection per kiln,
│  NovusClient             │   held open across Streamlit reruns
│  (novus_client.py)       │
└──────────────┬───────────┘
               │  await self._request(frame, expect_fc=...)
               ▼
┌──────────────────────────┐   pure protocol module — no I/O —
│  novus_protocol.py       │   builds, parses, CRCs the wire format
└──────────────┬───────────┘
               │  bleak.BleakClient.write_gatt_char / start_notify
               ▼
        ┌──────────────┐
        │  BLE → N20K48 │
        └──────────────┘
```

---

## Contributing & contact

This was built in one weekend by one person. PRs and issues welcome,
especially:
- Confirmation or correction of the register map in `PROTOCOL.md`
- The password-authentication mechanism
- Layout of the program table (programs 1–20, with 9 segments each)
- Support for related Novus controllers (N1200, N480D, etc.)

If you're on the Novus team and would like to engage, the open questions
are laid out in [`PROTOCOL.md`](PROTOCOL.md). Happy to talk.

## Acknowledgments

- **Novus Automation** for building rugged, long-lived controllers
- The IoThrifty Modbus wiki and other community resources that hinted at
  the register layout
- The maintainers of [`bleak`](https://github.com/hbldh/bleak) — without
  a cross-platform async BLE library this would have been miserable

## License

MIT — see [`LICENSE`](LICENSE).
