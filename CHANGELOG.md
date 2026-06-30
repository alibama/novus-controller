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

### Notes
- Protocol decode cross-checked against the official Novus communication PDF;
  whole-degree °F scaling and the corrected program-select register (247) are
  hardware-verified. See `docs/PROTOCOL.md`.
