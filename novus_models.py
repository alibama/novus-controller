"""
novus_models.py
===============
What we know about the Novus controller family, so the app can tell the
operator *which* controller it found, whether this software has been verified
against it, and how it should be treated.

The app is written and hardware-verified for the **N20K48** — a PID process
controller with a 20-program Ramp & Soak engine and Novus's Bluetooth module.
Other Novus controllers advertise the same BLE service and most share the
Modbus register map in broad strokes, so the app can *find* and *read* them,
but anything beyond reading PV/SP on an unverified model is at your own risk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Bluetooth fingerprints common to Novus's BLE module.
NOVUS_OUI = "00:26:A4"                                     # Novus Automation
NOVUS_MFG_ID = 511                                         # 0x01FF
NOVUS_SERVICE_UUID = "0783b03e-8535-b5a0-7140-a304d2495cb7"
NOVUS_DATA_CHAR = "0783b03e-8535-b5a0-7140-a304d2495cba"


@dataclass(frozen=True)
class ModelInfo:
    key: str            # canonical short name, e.g. "N20K48"
    family: str         # human description
    pid: bool           # PID process controller?
    ramp_soak: bool     # has a Ramp & Soak program engine?
    verified: bool      # this app's register map verified against hardware
    notes: str = ""


MODELS: dict[str, ModelInfo] = {
    "N20K48": ModelInfo("N20K48", "Modular PID process controller, 48×48, "
                        "20 Ramp & Soak programs × 9 segments",
                        pid=True, ramp_soak=True, verified=True,
                        notes="The controller this app was built and tested on."),
    "N1200":  ModelInfo("N1200", "PID process controller, 1/4 DIN, "
                        "Ramp & Soak", pid=True, ramp_soak=True, verified=False,
                        notes="Same Modbus register family; map not verified."),
    "N1040":  ModelInfo("N1040", "PID temperature controller, 48×48",
                        pid=True, ramp_soak=False, verified=False,
                        notes="Reads PV/SP; no 20-slot program table."),
    "N1030":  ModelInfo("N1030", "PID temperature controller, 48×48",
                        pid=True, ramp_soak=False, verified=False),
    "N1020":  ModelInfo("N1020", "PID temperature controller, 48×48",
                        pid=True, ramp_soak=False, verified=False),
    "N1050":  ModelInfo("N1050", "PID temperature controller, 48×48",
                        pid=True, ramp_soak=False, verified=False),
    "N1100":  ModelInfo("N1100", "PID process controller, 1/8 DIN, Ramp & Soak",
                        pid=True, ramp_soak=True, verified=False),
    "N2000":  ModelInfo("N2000", "PID process controller, 1/4 DIN, Ramp & Soak",
                        pid=True, ramp_soak=True, verified=False),
    "N480D":  ModelInfo("N480D", "PID controller, 48×48",
                        pid=True, ramp_soak=False, verified=False),
    "N3000":  ModelInfo("N3000", "Process indicator / controller, 1/8 DIN",
                        pid=True, ramp_soak=False, verified=False),
    "N3204":  ModelInfo("N3204", "Process indicator, 48×96",
                        pid=False, ramp_soak=False, verified=False,
                        notes="Indicator, not a controller — read-only."),
    "N1500":  ModelInfo("N1500", "Universal process indicator",
                        pid=False, ramp_soak=False, verified=False,
                        notes="Indicator, not a controller — read-only."),
    "N322":   ModelInfo("N322", "Thermostat / ON-OFF controller",
                        pid=False, ramp_soak=False, verified=False),
}

UNKNOWN = ModelInfo("unknown", "Unknown or unidentified Novus device",
                    pid=False, ramp_soak=False, verified=False,
                    notes="Identify it (read its device ID) before trusting "
                          "anything beyond PV/SP reads.")

# Substrings seen in BLE advertised names / product codes, longest first so
# "N1200" doesn't match before "N1200" variants etc.
_NAME_PATTERNS = sorted(MODELS.keys(), key=len, reverse=True)


def model_from_text(text: str | None) -> str:
    """Best-effort model key from an advertised name or a product-code string,
    e.g. "0200 - N20K48 - USB - 1 AI / 1 DO / 1 RL" → "N20K48". Returns ""
    when nothing matches."""
    if not text:
        return ""
    t = text.upper().replace(" ", "")
    for key in _NAME_PATTERNS:
        if key in t:
            return key
    # generic Nxxxx pattern (a model we haven't listed yet)
    m = re.search(r"\bN\d{3,4}[A-Z]{0,2}\d{0,2}\b", text.upper())
    return m.group(0) if m else ""


def info(model_key: str | None) -> ModelInfo:
    return MODELS.get((model_key or "").upper(), UNKNOWN)


def describe(model_key: str | None) -> str:
    """One-line label for the UI: 'N20K48 · PID, Ramp & Soak · verified'."""
    m = info(model_key)
    if m is UNKNOWN:
        return "unknown model · identify it to learn more"
    kind = ("PID" if m.pid else "indicator/thermostat") + \
           (", Ramp & Soak" if m.ramp_soak else "")
    return f"{m.key} · {kind} · {'verified' if m.verified else 'NOT verified'}"


def classify_advertisement(address: str, name: str | None, mfg_data: dict | None,
                           service_uuids=None) -> dict:
    """Decide whether a BLE advertisement looks like a Novus controller and
    say *why*. Returns {novus: bool, reasons: [..], model_guess: str}."""
    reasons = []
    addr = (address or "").upper()
    if addr.startswith(NOVUS_OUI):
        reasons.append("Novus MAC prefix 00:26:A4")
    if mfg_data and NOVUS_MFG_ID in mfg_data:
        reasons.append("Novus manufacturer id 511")
    for u in (service_uuids or []):
        if str(u).lower() == NOVUS_SERVICE_UUID:
            reasons.append("Novus BLE service UUID")
            break
    guess = model_from_text(name)
    nm = (name or "").lower()
    if guess:
        reasons.append(f"name looks like a {guess}")
    elif "novus" in nm or "quicktune" in nm:
        reasons.append("name mentions Novus/QuickTune")
    return {"novus": bool(reasons), "reasons": reasons, "model_guess": guess}
