"""
Boot-order test: the monitor must be creatable and running WITHOUT any
Streamlit page run (that's what run_dashboard.py relies on), and every later
boot() must return the same singletons. BLE + notify stubbed.

Run with:  pytest tests/test_boot.py
"""
import sys, pathlib, types, os, tempfile, importlib, json, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_b = types.ModuleType("bleak"); _b.BleakClient = type("B", (), {})
_b.BleakScanner = type("S", (), {}); _b.BleakError = Exception
sys.modules.setdefault("bleak", _b)
sys.modules.setdefault("notify", types.ModuleType("notify")).send = lambda *a, **k: None


def test_boot_starts_monitor_without_a_page_run():
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["KILN_DATA_DIR"] = str(tmp)
    (tmp / "devices.json").write_text(json.dumps([
        {"name": "k1", "address": "00:26:A4:00:00:01", "role": "kiln",
         "model": "N20K48", "password": "1111"}]))
    import paths; importlib.reload(paths)
    import devices; importlib.reload(devices)
    import monitor; importlib.reload(monitor)
    import app_core; importlib.reload(app_core)

    bridge, clients, mon, devs, runtime = app_core.boot()
    assert mon._started and "k1" in clients
    assert mon.passwords["k1"] == 1111 and mon.writable["k1"]
    assert mon.watchdogs["k1"].keep_hot is False          # a kiln
    # second boot (what a page does) returns the very same objects
    b2, c2, m2, d2, r2 = app_core.boot()
    assert m2 is mon and c2 is clients and b2 is bridge
    # ...even after Streamlit re-imports the module (file changed on disk):
    # the registry must survive a module reload or a second monitor appears.
    importlib.reload(app_core)
    b3, c3, m3, _, _ = app_core.boot()
    assert m3 is mon and c3 is clients and b3 is bridge
    # the launcher's flag parsing
    import run_dashboard as rd
    o = rd._flag_options(["--server.address", "127.0.0.1", "--server.port=8600"])
    assert o["server.address"] == "127.0.0.1" and o["server.port"] == 8600
    assert o["server.headless"] is True
