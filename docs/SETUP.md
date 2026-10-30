# Setup, hardening and operations

The short version is on the README: run the one-liner on the Linux machine
with the Bluetooth adapter, open port 8501, scan. This page is everything
behind that — what the installer does, how to do it by hand, and how to keep
a shop box running unattended.

## 1. What `install.sh` does

```bash
curl -fsSL https://raw.githubusercontent.com/alibama/novus-controller/main/install.sh | bash
```

1. Installs `git python3 python3-venv python3-pip bluez` with apt if missing.
2. Clones into `~/novus-controller` (override with `NOVUS_DIR=… ` or
   `--dir=…`), or `git pull --ff-only` if it's already there.
3. Creates `.venv` and installs `requirements.txt` + `requirements-api.txt`
   (+ pytest, so updates can be test-gated).
4. Generates, once, and never overwrites:
   - `.streamlit/secrets.toml` → `dashboard_password = "…"` (printed at the end)
   - `api.env` → `API_WRITE_TOKEN=…` (shown on the Open Data page)
5. Adds you to the `bluetooth` group (BlueZ access for the service user).
6. Installs and enables `kiln-dashboard` (port 8501, LAN-bound, password-gated)
   and `kiln-api` (port 8000) as systemd services from the templates in
   `deploy/`, with this machine's real path and user filled in.
7. Prints the URLs.

Re-running it updates: pull → deps → `deploy/update.sh` (byte-compile + tests
→ restart → health check). Flags: `--no-services` (just the venv; run things
by hand), `--no-api`, `--dir=PATH`.

Your local files — `devices.json`, `notify.env`, `api.env`,
`.streamlit/secrets.toml`, `*_settings.json`, `logs/`, `config_snapshots/`,
`notebooks/`, `notes/`, `state/`, `commands/` — are gitignored, so updates
never touch them. Everything lives in the repo directory unless you set
`KILN_DATA_DIR`.

## 2. Running without systemd (laptop / development)

```bash
bash install.sh --no-services          # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-api.txt
.venv/bin/python run_dashboard.py --server.address 0.0.0.0 --server.port 8501
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000      # optional, in a second terminal
```

Both read `.streamlit/secrets.toml` / `api.env` from the repo directory. The
dashboard needs the Bluetooth adapter; the API never touches Bluetooth and
can run anywhere that can see the data directory.

`run_dashboard.py` starts the monitor (logging, watchdog, command queue)
*before* Streamlit, so the kilns are watched from the moment the service is
up, with nobody logged in. Plain `streamlit run kiln_dashboard.py` also works
for development, but then the monitor only starts on the first page load.

Docker is an alternative on Linux hosts (`docker compose up -d`); BlueZ is
reached over the host's D-Bus, which is why the compose file uses host
networking and mounts `/var/run/dbus`.

## 3. First contact with the controllers

1. Enable Bluetooth on each controller's front-panel menu.
2. Close QuickTune Mobile on every phone — a controller only serves one BLE
   client at a time and stops advertising while connected.
3. Dashboard → **Devices → Scan**. Novus-looking devices list first with the
   reason they matched. Press **Identify** to read the model and firmware,
   then **Add** and pick the job (furnace / kiln / other).
4. Pick the furnace in *Which one is the furnace?* if you have one.
5. On **Recovery & Logic**, check each controller's hold target and trigger
   temperature. Auto-recovery is on for verified, writable furnaces and
   kilns; the watchdog never touches a kiln running a program other than its
   program-1 hold.

From the command line instead:

```bash
.venv/bin/python tools/kiln_bt_scanner.py            # Streamlit scanner
.venv/bin/python novus_client.py 00:26:A4:XX:XX:XX   # identify + read one controller
```

## 4. Notifications

```bash
cp notify.env.example notify.env && chmod 600 notify.env
nano notify.env                      # Telegram / ntfy / Twilio — fill in what you use
sudo systemctl restart kiln-dashboard
```

The installed unit loads `notify.env` automatically. Test from
**Settings → Test notifications**.

## 5. Sharing data (the API)

See [`API.md`](API.md). In short: port 8000 is public-read / token-write.
To let glassdatabase.org (or anyone on the internet) read it:

```bash
tailscale funnel --bg --https=10000 localhost:8000     # or any reverse proxy with TLS
```

then paste the public base URL into **Open Data & API → Public URL**, and
give `<public>/summary` to the Glass Database. Don't expose port 8501 to the
internet; keep the dashboard on the LAN or tailnet.

## 5b. HTTPS for the dashboard — Tailscale, no certbot

If the box is on a tailnet, you already have a name like
`shop.<tailnet>.ts.net` and Tailscale can issue a real Let's Encrypt
certificate for it. **certbot can't** — Tailscale owns the `.ts.net` zone.
One-time: in the admin console → DNS, turn on *MagicDNS* and *HTTPS
Certificates*. Then on the shop box:

```bash
# dashboard → https://shop.<tailnet>.ts.net  (tailnet only; websockets work)
sudo tailscale serve --bg --https=443 http://127.0.0.1:8501

# API for people on the tailnet → https://shop.<tailnet>.ts.net:8443
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8000

# API for the open internet (glassdatabase.org) → :10000
sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8000

tailscale serve status
```

"Redirect HTTP to HTTPS" is simpler than a redirect: stop the plain port
from answering. Put `DASHBOARD_ADDRESS=127.0.0.1` in `api.env` and
`sudo systemctl restart kiln-dashboard`; from then on only the HTTPS name
works and `http://shop:8501` is refused. Leave the API on `0.0.0.0` if
LAN tools use it directly, or do the same with `--host` in the unit.

Never Funnel the dashboard itself: it is password-gated, but it can drive
kilns. Keep it tailnet-only (`serve`, not `funnel`).

## 5c. Accounts

Two passphrases live in `.streamlit/secrets.toml`:

```toml
dashboard_password = "…"   # admin: run/stop/program, pause for QuickTune, settings, token
viewer_password    = "…"   # read-only: sees every page, changes nothing
```

The installer generates both (and adds `viewer_password` to an existing
file that lacks it). Give the viewer passphrase to anyone who should watch
the furnace from their phone without being able to touch it. The sidebar
shows which kind of session you're in, with a sign-out link.

## 6. Bluetooth permissions and troubleshooting

- Service user must be in the `bluetooth` group (the installer does this; a
  system service picks it up on restart, no re-login needed). If a scan finds
  nothing: `rfkill unblock bluetooth`, `bluetoothctl show` → *Powered: yes*,
  `bluetoothctl scan on` should list devices.
- "Operation already in progress": a previous discovery is still clearing;
  wait a few seconds. If it persists, `sudo systemctl restart bluetooth`.
- **One client per controller.** While the monitor holds a connection,
  QuickTune can't connect. Cooperative mode (default) connects only for a
  second or two every poll, so this is rarely a problem; the Home page also
  has *Release Bluetooth for QuickTune* for a long phone session.
- A controller that *isn't reachable* is treated as "in use or off" — logged
  quietly, never escalated to recovery. A controller that *reads low* still
  triggers the watchdog.
- Logs: `journalctl -u kiln-dashboard -f`, `journalctl -u kiln-api -f`.
  Per-controller CSVs under `logs/<name>/<date>.csv`, events under
  `logs/<name>/events.log`.

## 7. Hardening a shop laptop as a server

- **Don't sleep:** `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
  and in `/etc/systemd/logind.conf` set `HandleLidSwitch=ignore`,
  `HandleLidSwitchExternalPower=ignore`, `HandleLidSwitchDocked=ignore`.
- **Power blip:** in BIOS, set *AC Recovery* / *After power loss* to **Power On**.
- **Remote access:** Tailscale (`tailscale up --ssh`), disable key expiry for
  the node in the admin console.
- **No automatic reboots:** if you use `unattended-upgrades`, keep
  `Unattended-Upgrade::Automatic-Reboot "false";`. Update between firings.
- **Verify cold boot:** `sudo reboot`, do *not* log in, wait two minutes:
  dashboard loads, `systemctl status kiln-dashboard` is active since boot.

## 8. Updating and rolling back

```bash
bash ~/novus-controller/install.sh          # same as the one-liner
# or, finer control:
cd ~/novus-controller && git pull && bash deploy/update.sh [--api] [--rollback-on-failure]
```

`deploy/update.sh` validates (compile + tests) before restarting, health-checks
each service, dumps the journal on failure, and logs to `deploy/logs/`. Roll
back by hand with `git checkout <previous-commit> && bash deploy/update.sh`.
Restarting the dashboard pauses the software watchdog for a few seconds; the
controllers keep running their programs. Avoid updating mid-recovery.

Service names can be pinned in `deploy/update.conf` (`KILN_SERVICES="kiln-dashboard kiln-api"`).
