# tools/

Diagnostic and reverse-engineering helpers. Most are standalone scripts that
take a BLE address. They're how the protocol in `docs/PROTOCOL.md` was decoded,
and they're handy for poking at a new controller safely (reading first!).

| script | what it does |
|---|---|
| `kiln_bt_scanner.py` | Scan for nearby Novus controllers (OUI `00:26:A4`, mfg id 511). |
| `gatt_enumerate.py`  | Dump GATT services/characteristics of one device. |
| `novus_probe.py`     | Send framed read requests and print decoded responses. |
| `novus_n20k48_read.py` | Read a block of registers from one controller. |
| `explore_registers.py` | Read-only sweep of the documented registers via `NovusClient`. |
| `kiln_logger.py`     | Legacy headless CSV logger (superseded by `monitor.py`). |
| `snapshot_config.py` | Capture a full, verified config snapshot (JSON + human-readable) for backup/audit. |
| `export_usage.py`    | Export per-firing energy/cost as CSV + glass-database-ready XLSX (cron-friendly). |

Usage example:

```bash
python tools/explore_registers.py 00:26:A4:XX:XX:XX
```

`explore_registers.py` and `kiln_logger.py` import modules from the project
root and add it to `sys.path` automatically, so they run from anywhere in the
repo. `kiln_logger.py` also expects a `kiln_config.py` (copy the example).
