"""
Open-data API tests: endpoint shapes, aggregates, and — most important — the
guarantee that NO Bluetooth address ever appears in any response. Skips if
fastapi isn't installed (the API is an optional add-on).

Run with:  pytest tests/test_api.py
"""
import sys, pathlib, os, json, tempfile, importlib
from datetime import datetime, timedelta

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _make_env():
    data = pathlib.Path(tempfile.mkdtemp())
    os.environ["KILN_DATA_DIR"] = str(data)
    import devices as dv
    dv.DEVICES_PATH = data / "devices.json"
    dv.DEVICES_PATH.write_text(json.dumps([
        {"name": "furnace", "address": "00:26:A4:AA:BB:CC", "role": "furnace",
         "can_be_furnace": False, "expected_setpoint": 2100.0, "power_kw": 12.0},
        {"name": "bubba", "address": "00:26:A4:11:22:33", "role": "kiln",
         "can_be_furnace": True, "expected_setpoint": 896.0, "power_kw": 2.4}]))
    (data / "studio_settings.json").write_text(json.dumps(
        {"studio": "Test Studio", "rate_per_kwh": 0.12, "currency": "USD"}))
    d = data / "logs" / "furnace"; d.mkdir(parents=True)
    rows = ["timestamp_utc,pv,sp,output_pct,active_program,active_segment,connected"]
    t = datetime.fromisoformat("2026-09-10T10:00:00+00:00")
    for i in range(24):
        rows.append(f"{t.isoformat()},{min(2100,200+i*110)},2100,"
                    f"{100 if i<18 else 20},1,1,1")
        t += timedelta(minutes=5)
    (d / "2026-09-10.csv").write_text("\n".join(rows) + "\n")
    import api; importlib.reload(api)
    api._cache["rows"] = None
    return TestClient(api.app)


def test_endpoints_and_aggregates():
    c = _make_env()
    assert c.get("/health").json()["ok"] is True
    idx = c.get("/").json()
    assert idx["license"] == "CC-BY-4.0" and idx["studio"] == "Test Studio"

    f = c.get("/firings").json()
    assert f["count"] == 1 and f["firings"][0]["cost"] > 0

    s = c.get("/summary").json()
    assert s["totals"]["firings"] == 1 and s["totals"]["energy_kwh"] > 0
    assert "2026-09" in s["by_month"]
    # Glass Database data contract: these keys must be at the TOP LEVEL.
    for k in ("firings", "count", "energy_kwh", "kwh", "cost_usd", "cost",
              "since", "start"):
        assert k in s, f"contract key '{k}' missing from /summary"
    assert s["firings"] == s["count"] == 1
    assert s["energy_kwh"] == s["kwh"] and s["cost_usd"] == s["cost"]
    assert s["since"] == s["start"] == "2026-09-10"

    devs = c.get("/devices").json()["devices"]
    assert {d["name"] for d in devs} == {"furnace", "bubba"}
    assert all("address" not in d for d in devs)

    assert c.get("/firings.csv").text.splitlines()[0].startswith("studio,controller")


def test_no_ble_address_ever_leaks():
    c = _make_env()
    blob = (c.get("/").text + c.get("/firings").text + c.get("/summary").text
            + c.get("/devices").text + c.get("/firings.csv").text)
    assert "00:26:A4" not in blob
    assert "AA:BB:CC" not in blob


def test_filters():
    c = _make_env()
    assert c.get("/firings?controller=bubba").json()["count"] == 0
    assert c.get("/firings?controller=furnace").json()["count"] == 1
    assert c.get("/firings?since=2027-01-01").json()["count"] == 0


def _make_env_two_months():
    import os, json, tempfile, importlib, pathlib
    from datetime import datetime, timedelta
    data = pathlib.Path(tempfile.mkdtemp())
    os.environ["KILN_DATA_DIR"] = str(data)
    import devices as dv
    dv.DEVICES_PATH = data / "devices.json"
    dv.DEVICES_PATH.write_text(json.dumps([
        {"name": "furnace", "address": "00:26:A4:AA:BB:CC", "role": "furnace",
         "can_be_furnace": False, "expected_setpoint": 2100.0, "power_kw": 12.0}]))
    (data / "studio_settings.json").write_text(json.dumps(
        {"studio": "RBG", "rate_per_kwh": 0.12, "currency": "USD"}))
    d = data / "logs" / "furnace"; d.mkdir(parents=True)
    for day in ("2026-08-15", "2026-09-10"):
        rows = ["timestamp_utc,pv,sp,output_pct,active_program,active_segment,connected"]
        t = datetime.fromisoformat(f"{day}T10:00:00+00:00")
        for i in range(12):
            rows.append(f"{t.isoformat()},{min(2100,200+i*150)},2100,100,1,1,1")
            t += timedelta(minutes=5)
        (d / f"{day}.csv").write_text("\n".join(rows) + "\n")
    import api; importlib.reload(api); api._cache["rows"] = None
    from fastapi.testclient import TestClient
    return TestClient(api.app)


def test_summary_time_range():
    c = _make_env_two_months()
    full = c.get("/summary").json()
    assert full["firings"] == 2 and full["since"] == "2026-08-15" and full["until"] == "2026-09-10"
    sep = c.get("/summary?since=2026-09-01").json()
    assert sep["firings"] == 1 and sep["since"] == "2026-09-10"
    aug = c.get("/summary?until=2026-08-31").json()
    assert aug["firings"] == 1 and aug["range"]["until"] == "2026-08-31"
    # a bare until date includes the whole day
    incl = c.get("/summary?until=2026-09-10").json()
    assert incl["firings"] == 2
    win = c.get("/summary?since=2026-08-01&until=2026-08-20").json()
    assert win["firings"] == 1
