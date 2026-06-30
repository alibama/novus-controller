# SETUP.md — Shop laptop deployment & hardening

Target machine: `youruser-Latitude-5285` (Ubuntu 24). Goal: all four
controllers live in the dashboard, continuous logging, and the box comes
back on its own after a reboot or power blip — **with no login required.**

Work top to bottom. Commands assume the project lives at
`~/kiln-bt-scanner` and your username is `youruser`. If either differs, adjust
paths in the two `.service` files before installing them.

---

## 0. Get a terminal on the machine

Either sit at it, or from another tailnet device:
```bash
ssh youruser@your-kiln-host
```

---

## 1. Put the latest files in place

Copy these into `~/kiln-bt-scanner/`, overwriting the old versions:

```
novus_protocol.py      novus_client.py        kiln_dashboard.py
kiln_logger.py         kiln_analysis.py       kiln_calibrate.py
explore_registers.py   kiln_config.py         requirements.txt
kiln-dashboard.service kiln-analysis.service  SETUP.md
```

If you're using the git repo, just `git pull`. Otherwise Taildrop them from
the Mac, or `scp` them in. Quick sanity check that the imports resolve:

```bash
cd ~/kiln-bt-scanner
source .venv/bin/activate
pip install -r requirements.txt          # picks up pandas, autorefresh, etc.
python -c "import novus_client, kiln_config; print('imports OK')"
```

---

## 2. Find the new furnace controller's BLE address

Make sure nothing is holding BLE connections yet (the service isn't
installed, and QuickTune Mobile is closed). Then scan:

```bash
python - <<'PY'
import asyncio
from bleak import BleakScanner

async def main():
    print("scanning 10s...")
    devs = await BleakScanner.discover(timeout=10, return_adv=True)
    found = []
    for d, adv in devs.values():
        mfg = adv.manufacturer_data or {}
        is_novus = d.address.upper().startswith("00:26:A4") or (511 in mfg)
        if is_novus:
            found.append((d.address, d.name))
    if not found:
        print("no Novus controllers seen — power-cycle the furnace controller and retry")
    for addr, name in sorted(found):
        print(f"  {addr}   {name}")

asyncio.run(main())
PY
```

You should see four addresses. Three you already know (bubba/freezy/
calliope); the fourth is the furnace. Copy that address.

---

## 3. Add the furnace to the config

Edit `kiln_config.py` and fill in the furnace line:

```bash
nano kiln_config.py
```
```python
KILNS = [
    ("bubba",    "00:26:A4:XX:XX:XX"),
    ("freezy",   "00:26:A4:XX:XX:XX"),
    ("calliope", "00:26:A4:XX:XX:XX"),
    ("furnace",  "00:26:A4:00:XX:XX"),   # <-- the address from step 2
]
```

This one file is the only place the kiln list lives now.

---

## 4. Functional test before installing the service

Run the dashboard by hand once to confirm all four connect:

```bash
streamlit run kiln_dashboard.py --server.address 127.0.0.1 --server.port 8501
```

From a tailnet device, open the Funnel/Serve URL (or
`http://localhost:8501` if you're at the machine). First load takes ~20–25s
while it connects to four controllers in turn. Confirm all four columns show
live PV/SP. Then `Ctrl-C` to stop. CSV logs are now accruing under
`~/kiln-bt-scanner/logs/<name>/<date>.csv`.

---

## 5. Bluetooth permissions for a background service

A system service running as `youruser` needs D-Bus access to BlueZ. Add
yourself to the `bluetooth` group:

```bash
sudo usermod -aG bluetooth youruser
```

(No need to log out — a system service picks up the new group when it
starts. Group membership in your interactive shell updates after a
re-login, but that doesn't matter here.)

---

## 6. Install the dashboard as a boot service

```bash
sudo cp ~/kiln-bt-scanner/kiln-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiln-dashboard
sudo systemctl start kiln-dashboard
sudo systemctl status kiln-dashboard        # should be active (running)
journalctl -u kiln-dashboard -f             # watch live logs; Ctrl-C to exit
```

`enable` makes it start at boot. `Restart=always` + `StartLimitIntervalSec=0`
in the unit means it relaunches forever even if it crashes or a kiln is off.

(Optional) the read-only analysis page on port 8502, which can run alongside
because it never touches Bluetooth:
```bash
sudo cp ~/kiln-bt-scanner/kiln-analysis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kiln-analysis
```

---

## 7. Stop the laptop from ever sleeping

This is the big one for a laptop acting as a server. Block all sleep states
and tell it to ignore the lid:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Then edit logind:
```bash
sudo nano /etc/systemd/logind.conf
```
Set (uncomment and change) these lines:
```
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
```
Leave `HandlePowerKey` alone so you can still force-off by holding power.
This takes effect on the reboot in step 10 (or `sudo systemctl restart
systemd-logind`, but that can end your GUI session — easier to just reboot).

---

## 8. Survive a power blip (BIOS)

So the box powers itself back on after a shop power loss:

1. Reboot and tap **F2** at the Dell logo to enter BIOS setup.
2. **Power Management → AC Recovery** (sometimes "AC Power Recovery").
3. Set it to **Power On** (not "Last State", not "Power Off").
4. Save & exit.

While you're in there, confirm the clock/date is right (helps with log
timestamps and TLS).

---

## 9. Make sure Tailscale comes back on its own

```bash
sudo systemctl enable --now tailscaled
tailscale up --ssh                 # re-assert SSH access over the tailnet
tailscale serve status             # confirm the :443 -> localhost:8501 proxy
tailscale funnel status            # confirm public Funnel is still configured
```

In the Tailscale **admin console**, on this machine: confirm **"Disable key
expiry"** is on, so the node never needs a manual re-auth.

The Serve/Funnel config persists across reboots; step 10 verifies it.

---

## 10. Reboot and verify the whole thing comes back cold

```bash
sudo reboot
```

**Do not log in.** Wait ~1–2 minutes, then from another device:

- Hit the Funnel URL → dashboard loads, all four kilns connect.
- `ssh youruser@your-kiln-host` still works.
- `sudo systemctl status kiln-dashboard` → active (running), and note the
  uptime started at boot, proving it came up without a login.

If all three are true, the box is hardened: power blip → BIOS powers it on →
tailscaled + the dashboard service start at boot → Funnel re-exposes it →
logging resumes. No keyboard, no login.

---

## Operating notes & gotchas

- **One BLE client at a time per controller.** While the service holds
  connections, QuickTune Mobile can't connect to those kilns, and neither
  can a second copy of the dashboard. To use QuickTune or run
  `kiln_calibrate.py`, stop the service first:
  `sudo systemctl stop kiln-dashboard`, do your thing, then `start` it again.

- **Calibration** (`kiln_calibrate.py`) and **the dashboard** both drive
  BLE, so they can't run together. Stop the service first.

- **Don't enable automatic OS reboots.** A surprise reboot mid-firing is
  worse than a delayed update. If you set up `unattended-upgrades`, leave
  `Unattended-Upgrade::Automatic-Reboot "false";`. Update manually between
  firings.

- **Logs growth.** Journald keeps service stdout. If it ever gets large:
  `sudo journalctl --vacuum-time=30d`. CSV logs are tiny; leave them.

- **Updating code later.** Drop new files in, then
  `sudo systemctl restart kiln-dashboard`. Adding a kiln = edit
  `kiln_config.py`, then restart.

- **Quick service controls.**
  - status: `systemctl status kiln-dashboard`
  - live logs: `journalctl -u kiln-dashboard -f`
  - restart: `sudo systemctl restart kiln-dashboard`
  - stop (to free BLE): `sudo systemctl stop kiln-dashboard`
