# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/) loosely.

## [Unreleased]

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
