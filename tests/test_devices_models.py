"""
Device registry + model identification tests. No hardware.

Run with:  pytest tests/test_devices_models.py
"""
import sys, pathlib, json, os, tempfile, importlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import novus_models as nm
import novus_protocol as P


def test_classify_by_each_fingerprint():
    assert nm.classify_advertisement("00:26:A4:11:22:33", None, {}, [])["novus"]
    assert nm.classify_advertisement("AA:BB:CC:00:00:01", None, {511: b"\x60"}, [])["novus"]
    assert nm.classify_advertisement("AA:BB:CC:00:00:02", "N1200", {}, [])["novus"]
    assert nm.classify_advertisement("AA:BB:CC:00:00:03", "", {},
                                     [nm.NOVUS_SERVICE_UUID])["novus"]
    assert not nm.classify_advertisement("AA:BB:CC:00:00:04", "Phone", {76: b"x"}, [])["novus"]


def test_model_guess_from_product_code():
    assert nm.model_from_text("0200 - N20K48 - USB - 1 AI / 1 DO / 1 RL") == "N20K48"
    assert nm.model_from_text("Novus N1200 bridge") == "N1200"
    assert nm.model_from_text("N 480D") == "N480D"
    assert nm.model_from_text("") == ""
    assert nm.info("N20K48").verified and nm.info("N20K48").pid
    assert not nm.info("N1200").verified
    assert nm.info("whatever") is nm.UNKNOWN


def test_device_id_roundtrip():
    payload = P.build_device_id_response(
        {0: "Novus Automation", 1: "0200 - N20K48 - USB - 1 AI / 1 DO / 1 RL", 2: "1.02"})
    d = P.parse_device_id_response(payload)
    assert d["vendor"] == "Novus Automation" and d["revision"] == "1.02"
    assert nm.model_from_text(d["product"]) == "N20K48"
    # a read response must be rejected
    try:
        P.parse_device_id_response(bytes([1, 0x46, 2, 0, 0]))
        assert False, "should have raised"
    except ValueError:
        pass


def _devices_module(tmp):
    os.environ["KILN_DATA_DIR"] = str(tmp)
    import paths; importlib.reload(paths)
    import devices; importlib.reload(devices)
    return devices


def test_new_device_defaults_and_readonly_for_unverified():
    dv = _devices_module(pathlib.Path(tempfile.mkdtemp()))
    k = dv.new_device("kiln1", "00:26:a4:11:22:33", dv.ROLE_KILN)
    assert k.address == "00:26:A4:11:22:33" and k.model == "N20K48"
    assert k.writes_allowed and k.verified and k.expected_setpoint == 896.0
    f = dv.new_device("furnace", "00:26:A4:00:00:01", dv.ROLE_FURNACE,
                      product="0200 - N1200 - RS485")
    assert f.model == "N1200" and not f.writes_allowed and f.expected_setpoint == 2100.0


def test_registry_roundtrip_tolerates_old_and_unknown_keys():
    tmp = pathlib.Path(tempfile.mkdtemp())
    dv = _devices_module(tmp)
    (tmp / "devices.json").write_text(json.dumps([
        {"name": "old", "address": "00:26:a4:aa:bb:cc", "role": "kiln",
         "can_be_furnace": True, "expected_setpoint": 896.0},       # pre-model file
        {"name": "x", "address": "00:26:A4:00:00:09", "role": "bogus",
         "future_field": 1, "model": ""}]))
    devs = dv.load_devices()
    assert devs[0].model == "N20K48" and devs[0].address == "00:26:A4:AA:BB:CC"
    assert devs[1].role == dv.ROLE_KILN and devs[1].model == "N20K48"
    dv.save_devices(devs)
    assert dv.load_devices()[0].name == "old"


def test_validate_flags_duplicates_and_bad_macs():
    dv = _devices_module(pathlib.Path(tempfile.mkdtemp()))
    devs = [dv.Device("a", "00:26:A4:00:00:01"), dv.Device("a", "00:26:A4:00:00:01"),
            dv.Device("b", "nope")]
    probs = dv.validate(devs)
    assert any("duplicate name" in p for p in probs)
    assert any("duplicate address" in p for p in probs)
    assert any("invalid BLE address" in p for p in probs)
    assert dv.validate([dv.Device("ok", "00:26:A4:00:00:02")]) == []
