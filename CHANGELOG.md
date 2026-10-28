# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/) loosely.

## [2.0.0] — 2026-10-27

A streamlining pass: one-line install, device search as a first-class page,
no assumptions about the shop, an API that other apps can write to, and a
Learn page.

### Added
- **`install.sh`** — one-line install *and* update
  (`curl … | bash`): apt deps, clone/pull, venv, generated dashboard
  passphrase and API token, bluetooth group, systemd services for the
  dashboard and the API. Re-running updates through the validated
  `deploy/update.sh` path. Optional `Dockerfile` + `docker-compose.yml`.
- **Devices page** (`pages/1_Devices.py`): Bluetooth scan that classifies any
  Novus-looking advertisement (MAC prefix, manufacturer id, service UUID, or a
  model-like name) and says why; **Identify** reads the device identification
  (vendor / product code / firmware) over Modbus MEI type 14; add by scan or
  by address; a one-line *Which one is the furnace?* picker; per-device model,
  role (furnace / kiln / **other** = monitor only), hold target, rated kW and
  an *Allow writes* switch. Changes apply to the running monitor **live** —
  no service restart.
- `novus_models.py`: what we know about the Novus family (N20K48 verified;
  N1200/N1040/N480D/… recognised, read-only by default) and the scan
  classifier. `NovusClient.read_device_id()`, `identify()`, `read_pid()`;
  the client now reads the °C/°F unit register.
- **Open-data API v2** (`api.py`): `GET /live` (latest reading per controller
  from `state/live.json`, written by the monitor after every poll),
  `GET /events`, `GET /notes`, `GET /commands`; **authorized writes** with a
  bearer token from `api.env`: `POST /notes` and `POST /commands`
  (`run_program` / `stop_program` / `set_manual_setpoint` / `poll`). Commands
  go through a file queue (`commands.py`) and are executed by the dashboard's
  monitor through the same `control()` path as the UI, so read-only
  controllers reject them and every action lands in `events.log`.
- **Open Data & API page** (`pages/7_Open_Data.py`): studio name / rate /
  rated kW, live API health, public URL, token show/regenerate, control
  switch, the glassdatabase.org hand-off, downloads, and the notes/commands
  other apps sent.
- **Learn page** (`pages/6_Learn.py`) + `glass_science.py`: PID and Ramp &
  Soak explained against the controller's registers (with a live PID read),
  annealing (why, the thickness table, a calculator that writes a schedule to
  a slot), fusing/slumping/casting temperatures, heat-work, and the watchdog
  in plain language.
- `paths.py`: one data directory (`KILN_DATA_DIR`) for everything the app
  writes. `.gitignore` (there was none — secrets and device lists could have
  been committed). `.gitattributes` for LF shell scripts.
- Tests for the scan classifier, device-id parsing, the registry, the command
  queue and monitor execution, the read-only guard, live.json, live
  add/rename/remove, API auth, and the glass-science calculator (76 tests).

### Changed
- Home page no longer hardcodes a studio name; the title comes from the
  studio setting. With no devices it walks you to the Devices page. The
  Open Data panel moved to its own page, and usage rows are cached (they were
  rebuilt from every log file on every 5-second refresh).
- Settings page keeps programming/tolerance/notifications/registers/BT
  sharing/snapshots; scanning and roles moved to Devices, energy settings to
  Open Data & API. Program writers hide read-only controllers.
- Watchdog defaults derive from role **and** model: `other` devices and
  unverified or write-disabled controllers are monitored but never driven.
- systemd units: dashboard binds `0.0.0.0` (password-gated; LAN access is
  the point), both units load `notify.env` / `api.env`, API hardened with
  `Restart=always`. Studio-specific wording removed from unit descriptions.
- Streamlit's deprecated `use_container_width` replaced by `width="stretch"`;
  `streamlit>=1.49` pinned. Verified against bleak 3.x.

### Fixed
- Analytics *Firing summary* tab crashed with `NameError: name` (used an
  undefined variable for the selected controller).
- A stray copy of the Analytics page at the repo root was removed.
- `NovusClient.commit()` referenced an unimported name (would `NameError`)
  and wrote the OPEN_SESSION register; removed.
- `reclaim_ble()` no longer force-reconnects every controller (cooperative
  mode reconnects lazily on the next poll).

## [Unreleased — earlier]

### Changed (Bluetooth sharing)
- The monitor now runs in **cooperative mode by default**: it always
  disconnects after each brief read (even when worried), so it never blocks a
  QuickTune user. A controller that can't be reached is treated as "in use or
  off" — logged (rate-limited), left on the normal slow cadence, and NOT
  escalated to fast polling or recovery. Real low-temperature readings still
  trigger the watchdog, so sharing the radio does not disable safety. New
  Settings → Bluetooth sharing controls (cooperative toggle + read interval),
  persisted to monitor_settings.json and applied live.

### Added
- Multipage Streamlit app: Home (furnace temps + kiln/furnace control panels),
  Analytics, and Settings.
- Always-on background monitor with intermittent BLE polling and per-controller
  CSV logging, independent of any open browser tab.
- Furnace watchdog with a user-settable trigger temperature, automatic
  stop/start recovery, post-recovery confirmation, and layered safety guards
  (implausible-reading guard, recover-floor, rate limiting, manual-override
  disarm). Recovers both clean dropouts and "output pinned but not holding"
  output-stage faults.
- Standard firing-program library (hold, anneal-down, full fuse, tack, slump,
  casting, fire-polish) and a Settings writer that programs controller slots.
- Notification fan-out: Telegram, ntfy, Twilio SMS.
- Settings tools: BLE scanner, device role assignment, notification tester,
  guaranteed-soak tolerance editor, and a raw register console.
- systemd unit templates and a hardened deployment runbook.

### Added (unreleased, later)
- Watchdog generalized to **all controllers**: program 1 is the "set and
  forget" hold everywhere. Kilns are only rescued while actively holding
  program 1 and are never disturbed during a deliberate firing on another
  program; furnaces (and any controller marked *keep hot*) are held from any
  state.
- **Recovery & Logic** page: live per-controller operational state, every
  decision threshold/timer editable and persisted, a manual temperature
  override, run-a-program control, and a recent-events viewer.
- Manual setpoint override (`set_manual_setpoint`) to push a controller to any
  target on demand.
- Plausibility floor now scales to each controller's hold temperature.

### Added (service install)
- `deploy/install-services.sh`: generates systemd units from the templates with
  this machine's real repo path and service user (owner of the repo dir, or
  `--user`/`--root`), avoiding the `status=217/USER` trap from copying the
  placeholder templates verbatim. Templates now carry a loud "do not copy
  verbatim" header.
- `deploy/update.sh` unit-existence check switched to `systemctl cat` (the
  previous `list-unit-files | grep` missed an installed, enabled unit), plus
  `deploy/update.conf` support to pin service names.

### Added (updates)
- `deploy/update.sh`: one-command update that installs changed deps,
  byte-compiles and runs the test suite as a safety gate, and only then
  restarts the systemd services it finds — logging everything to
  `deploy/logs/` and health-checking each service (with optional rollback). A
  broken pull never restarts a service. Fixed a latent ordering bug the gate
  caught (`from __future__` after a path shim in `tools/kiln_logger.py`).

### Added (open-data API)
- `api.py`: an optional read-only FastAPI service publishing per-firing energy
  and cost (`/firings`, `/firings.csv`), rollups (`/summary`), and per-device
  aggregates (`/devices`). CORS-open, CC-BY-4.0, and public-safe — it never
  serves Bluetooth addresses or secrets. `requirements-api.txt` +
  `deploy/kiln-api.service` + `docs/API.md`.

### Fixed
- Analytics no longer KeyErrors on a controller whose logged PV is all blank
  (e.g. a newly added device) — the ramp-rate columns are always created.

### Added (open data + energy)
- `usage.py` + `runs.py`: estimate per-firing energy (kWh) and cost from the
  output-power integral and each controller's rated kW, excluding log gaps.
- Home-page **Open Data Studio** panel: harvest per-firing usage as CSV or a
  glass-database-ready XLSX (sheet + data dictionary), plus raw telemetry as a
  zip; shown with studio attribution and CC-BY framing, with instructions to
  pipe into the glassdatabase.org importer.
- Settings **Energy & open data**: studio name, electricity rate/currency, and
  rated power (kW) per controller.
- `tools/export_usage.py` CLI for headless/cron export.
- Device gains a `power_kw` field (backward-compatible).

### Added (firing notebook)
- `notebook.py` + a Notebook page: reconstruct a firing from the telemetry log,
  align it to the intended program segment-by-segment, and report where the
  actual curve diverged (target not reached, power-limited/slow ramp, short
  effective soak, overshoot, BLE dropouts) with conservative suggested program
  adjustments. Heat-work proxy (°F·hours, excluding log gaps) and a two-run
  comparison explain why nominally identical programs yield different glass.
  Notes + photos attach to an entry; each saves as a self-contained shareable
  HTML (inline SVG chart, base64 images — no external dependencies).

### Added (config audit)
- `config_snapshot.py` + a Settings-page section + `tools/snapshot_config.py`
  CLI: capture a controller's full configuration (all 20 program tables and the
  key config registers) with multi-read verification, store it as canonical
  JSON with a SHA-256 fingerprint and a human-readable text dump, verify
  integrity, and diff two snapshots to confirm exactly what changed.
- Page filenames no longer contain emoji (icons kept as in-page `page_icon`),
  so pages are easier to move and reference.

### Fixed
- BLE request/response correlation: `_request` now discards stale or misaligned
  frames (wrong CRC, wrong function code, or wrong register count) and waits
  for the correct response, with a resend retry. Fixes writes failing with
  "expected FC 0x47, got 0x46" and reads returning a previous request's data.

### Notes
- Protocol decode cross-checked against the official Novus communication PDF;
  whole-degree °F scaling and the corrected program-select register (247) are
  hardware-verified. See `docs/PROTOCOL.md`.
