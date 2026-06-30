"""
kiln_config.example.py
======================
Copy this to `kiln_config.py` and fill in your controllers' BLE addresses,
OR skip it entirely and add devices from the Settings page's scanner (which
writes devices.json). On first run, devices.py seeds its registry from this
list if devices.json doesn't exist yet.

Find a controller's BLE address with the scanner:
    python tools/kiln_bt_scanner.py
Look for the 00:26:A4 prefix (the Novus OUI) and manufacturer id 511.

`kiln_config.py` is gitignored so your real addresses never get committed.
"""

KILNS: list[tuple[str, str]] = [
    # ("name",   "BLE_ADDRESS"),
    ("kiln1",    "00:26:A4:XX:XX:XX"),
    ("kiln2",    "00:26:A4:XX:XX:XX"),
    # ("furnace", "00:26:A4:XX:XX:XX"),   # a controller you want temperature-watched
]
